import os
import time
import json
import uuid
import asyncio
import requests
import subprocess
import signal
import httpx

from fastapi import FastAPI, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import docker  # pip install docker

###########################
# Create our FastAPI app
###########################
app = FastAPI()

# Docker client (default from_env)
docker_client = docker.from_env()

# Session info by session_id (in-memory for quick lookups)
# Each entry: {
#   "start_time": float,
#   "last_active": float,
#   "container_id": str,
#   "cdp_port": str,
#   "no_vnc_port": str,
#   "vnc_port": str,
#   "user_ip": str,
#   "host_public_ip": str,  # <-- We'll store this now
#   "process": subprocess.Popen or None
# }
SESSIONS = {}

# Map user_ip -> session_id (in-memory)
IP_SESSIONS = {}

# Timeout for container inactivity (5 minutes)
TIMEOUT = 300

# Timeout for agent subprocess (2 minutes)
SUBPROCESS_TIMEOUT = 120

###########################
# REDIS for cross-VM session sharing
###########################
import redis

REDIS_HOST = "cuak-v2-uv1vbr.serverless.use1.cache.amazonaws.com"
REDIS_PORT = 6379  # or whatever port your Redis uses

redis_client = redis.StrictRedis(
    host=REDIS_HOST, port=REDIS_PORT, decode_responses=True
)


def set_session_data(session_id: str, data: dict):
    """
    Store in local memory and mirror to Redis.
    """
    SESSIONS[session_id] = data
    redis_client.set(f"session_data:{session_id}", json.dumps(data))


def get_session_data(session_id: str) -> dict:
    """
    Retrieve from local memory if present, otherwise from Redis.
    """
    if session_id in SESSIONS:
        return SESSIONS[session_id]
    r_data = redis_client.get(f"session_data:{session_id}")
    if r_data:
        loaded = json.loads(r_data)
        SESSIONS[session_id] = loaded
        return loaded
    return None


def delete_session_data(session_id: str):
    SESSIONS.pop(session_id, None)
    redis_client.delete(f"session_data:{session_id}")


def set_ip_session(user_ip: str, session_id: str):
    IP_SESSIONS[user_ip] = session_id
    redis_client.set(f"ip_session:{user_ip}", session_id)


def get_ip_session(user_ip: str) -> str:
    """
    Retrieve IP -> session_id from local memory or Redis.
    """
    if user_ip in IP_SESSIONS:
        return IP_SESSIONS[user_ip]
    sid = redis_client.get(f"ip_session:{user_ip}")
    if sid:
        IP_SESSIONS[user_ip] = sid
        return sid
    return None


def delete_ip_session(user_ip: str):
    IP_SESSIONS.pop(user_ip, None)
    redis_client.delete(f"ip_session:{user_ip}")


###########################
# Enable CORS
###########################
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


###########################
# Healthcheck
###########################
@app.get("/health")
def health_check():
    """Simple health check endpoint."""
    return {"status": "ok"}


###########################
# Helper: get public hostname
###########################
def get_instance_public_ip():
    """
    Attempt to fetch the AWS EC2 public IP via metadata.
    Fallback to localhost if not available or not on AWS.
    """
    try:
        resp = requests.get(
            "http://169.254.169.254/latest/meta-data/public-ipv4", timeout=2
        )
        if resp.status_code == 200:
            return resp.text.strip()
    except Exception as e:
        print("Error retrieving instance public IP:", e)
    return "localhost"


###########################
# Background Cleanup
###########################
@app.on_event("startup")
async def start_cleanup_task():
    # Start an async task in the background
    asyncio.create_task(cleanup_inactive_sessions())


async def cleanup_inactive_sessions():
    """
    Every 60s, check all sessions. If a session has been inactive
    (no calls to /start or /vnc) for more than TIMEOUT seconds,
    remove the container and delete the session from memory & Redis.
    """
    while True:
        now = time.time()
        # We must iterate over local memory sessions. If needed,
        # you could also iterate over redis to find stale sessions.
        for session_id, session_data in list(SESSIONS.items()):
            last_active = session_data["last_active"]
            if (now - last_active) > TIMEOUT:
                print(
                    f"[CLEANUP] Session {session_id} inactive > {TIMEOUT}s. Removing."
                )
                container_id = session_data["container_id"]
                try:
                    container = docker_client.containers.get(container_id)
                    container.stop()
                    container.remove(force=True)
                except Exception as e:
                    print(f"[CLEANUP] Error removing container {container_id}: {e}")

                delete_session_data(session_id)
                user_ip = session_data["user_ip"]
                existing = get_ip_session(user_ip)
                if existing == session_id:
                    delete_ip_session(user_ip)

        await asyncio.sleep(60)


###########################
# Start & Stream Agent
###########################
@app.post("/start")
async def start_and_stream(payload: dict, request: Request):
    user_ip = request.headers.get("x-forwarded-for", request.client.host)
    print(f"Incoming /start from IP: {user_ip}")

    existing_session_id = get_ip_session(user_ip)
    session_id = None
    container = None
    reuse = False

    if existing_session_id:
        existing_data = get_session_data(existing_session_id)
        if existing_data:
            elapsed = time.time() - existing_data["last_active"]
            if elapsed < TIMEOUT:
                reuse = True
                container_id = existing_data["container_id"]
                container = docker_client.containers.get(container_id)
                session_id = existing_session_id
                existing_data["last_active"] = time.time()
                set_session_data(session_id, existing_data)
                print(f"Reusing container {container.name} for IP {user_ip}")
            else:
                # stale; remove from memory and kill container
                try:
                    container_id = existing_data["container_id"]
                    container = docker_client.containers.get(container_id)
                    container.stop()
                    container.remove(force=True)
                except:
                    pass
                delete_session_data(existing_session_id)
                delete_ip_session(user_ip)

    if not reuse:
        # Spin up a new container
        session_id = str(uuid.uuid4())
        container_name = f"chrome_instance_{session_id}"
        print(f"Starting container: {container_name}")

        container = docker_client.containers.run(
            "abhipi04/custom-chrome-novnc",
            detach=True,
            name=container_name,
            ports={
                "5900/tcp": None,
                "9333/tcp": None,
                "6080/tcp": None,
            },
            shm_size="2g",
        )
        # Wait for container to spin up
        time.sleep(2)
        container.reload()

        ports_info = container.attrs["NetworkSettings"]["Ports"]
        cdp_port_mapping = ports_info["9333/tcp"][0]["HostPort"]
        no_vnc_port_mapping = ports_info["6080/tcp"][0]["HostPort"]
        vnc_port_mapping = ports_info["5900/tcp"][0]["HostPort"]

        # Store the public IP so any VM can generate the correct noVNC URL
        public_ip_for_container = get_instance_public_ip()

        session_data = {
            "start_time": time.time(),
            "last_active": time.time(),
            "container_id": container.id,
            "cdp_port": cdp_port_mapping,
            "no_vnc_port": no_vnc_port_mapping,
            "vnc_port": vnc_port_mapping,
            "user_ip": user_ip,
            "host_public_ip": public_ip_for_container,  # <--- HERE we store the original VM IP
            "process": None,
        }
        set_session_data(session_id, session_data)
        set_ip_session(user_ip, session_id)
    else:
        container.reload()
        ports_info = container.attrs["NetworkSettings"]["Ports"]
        cdp_port_mapping = ports_info["9333/tcp"][0]["HostPort"]
        no_vnc_port_mapping = ports_info["6080/tcp"][0]["HostPort"]
        vnc_port_mapping = ports_info["5900/tcp"][0]["HostPort"]

        updated_data = get_session_data(session_id)
        updated_data.update(
            {
                "last_active": time.time(),
                "cdp_port": cdp_port_mapping,
                "no_vnc_port": no_vnc_port_mapping,
                "vnc_port": vnc_port_mapping,
            }
        )
        set_session_data(session_id, updated_data)

    # Construct CDP URL
    session_data = get_session_data(session_id)
    cdp_url = f"http://{session_data['host_public_ip']}:{session_data['cdp_port']}"
    payload["cdp_url"] = cdp_url

    # Kill any old running process for this session
    old_proc = session_data.get("process")
    if old_proc and old_proc.poll() is None:
        print(f"Killing old agent process for session {session_id}")
        try:
            os.killpg(os.getpgid(old_proc.pid), signal.SIGKILL)
        except Exception as e:
            print(f"Error killing old process group: {e}")

    # Start new subprocess in its own process group
    payload_str = json.dumps(payload, separators=(",", ":"))
    process = subprocess.Popen(
        ["bash", "-c", f"pipenv run python -u browsing_agent.py '{payload_str}'"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
        preexec_fn=os.setsid,
        cwd="/home/ubuntu/l2_cuak",
    )
    session_data["process"] = process
    set_session_data(session_id, session_data)

    async def stream_generator():
        yield f"data: Session started with ID: {session_id}. Container: {container.name}\n\n"
        yield f"data: cdp_url: {cdp_url}\n\n"
        start_time = time.time()

        try:
            while True:
                elapsed = time.time() - start_time
                current_data = get_session_data(session_id)
                if current_data:
                    current_data["last_active"] = time.time()
                    set_session_data(session_id, current_data)

                # If the client disconnects
                if await request.is_disconnected():
                    yield "data: Client disconnected. Killing subprocess.\n\n"
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except Exception as e:
                        yield f"data: Error killing subprocess: {e}\n\n"
                    yield "event: close\ndata: end\n\n"
                    return

                # Read from stdout/stderr concurrently
                stdout_task = asyncio.create_task(
                    asyncio.to_thread(process.stdout.readline)
                )
                stderr_task = asyncio.create_task(
                    asyncio.to_thread(process.stderr.readline)
                )

                done, pending = await asyncio.wait(
                    {stdout_task, stderr_task},
                    timeout=0.1,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    line = task.result().rstrip("\n")
                    if line:
                        yield f"data: {line}\n\n"

                # Cancel pending tasks
                for task in pending:
                    task.cancel()

                # Subprocess time limit
                if elapsed > SUBPROCESS_TIMEOUT:
                    yield f"data: Subprocess timed out after {SUBPROCESS_TIMEOUT}s. Killing!\n\n"
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except Exception as e:
                        yield f"data: Error forcibly killing agent: {e}\n\n"
                    yield "event: close\ndata: end\n\n"
                    return

                # If process finished on its own
                if process.poll() is not None:
                    yield "data: Subprocess finished.\n\n"
                    yield "event: close\ndata: end\n\n"
                    return

                await asyncio.sleep(0.05)

        finally:
            sd = get_session_data(session_id)
            if sd and "process" in sd:
                sd["process"] = None
                set_session_data(session_id, sd)
            print(f"Agent process ended for session {session_id}")

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


###########################
# VNC Endpoint
###########################
@app.get("/vnc/{session_id}")
def get_vnc(session_id: str, request: Request):
    """
    Returns a noVNC HTML client that connects directly to the container's
    original host VM (via the 'host_public_ip' we stored) and noVNC port.
    """
    user_ip = request.headers.get("x-forwarded-for", request.client.host)
    session_data = get_session_data(session_id)
    if not session_data:
        return Response(content="Session not found or expired.", media_type="text/html")

    # Optional: Enforce IP ownership
    # if session_data["user_ip"] != user_ip:
    #     return Response("Session does not belong to your IP.", media_type="text/html")

    # Update last_active
    session_data["last_active"] = time.time()
    set_session_data(session_id, session_data)

    # Instead of local Docker calls, we rely on Redis for the container's actual host & port
    no_vnc_port_mapping = session_data["no_vnc_port"]
    vnc_host = session_data["host_public_ip"]  # The actual VM that runs the container
    vnc_password = os.getenv("VNC_PASSWORD", "12345678")

    # Construct the HTML that points noVNC at the original VM
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8"/>
    <title>NoVNC Session {session_id}</title>
    <style>
        body {{
            margin: 0;
            background-color: dimgrey;
            height: 100%;
            display: flex;
            flex-direction: column;
        }}
        html {{
            height: 100%;
        }}
        #top_bar {{
            background-color: #6e84a3;
            color: white;
            font: bold 12px Helvetica;
            padding: 6px 5px 4px 5px;
            border-bottom: 1px outset;
        }}
        #status {{
            text-align: center;
        }}
        #sendCtrlAltDelButton {{
            position: fixed;
            top: 0px;
            right: 0px;
            border: 1px outset;
            padding: 5px;
            cursor: pointer;
        }}
        #screen {{
            flex: 1;
            overflow: hidden;
        }}
    </style>
</head>
<body>
<div id="top_bar">
  <div id="status">Loading</div>
  <div id="sendCtrlAltDelButton">Send CtrlAltDel</div>
</div>
<div id="screen"></div>

<script type="module">
import RFB from 'https://cdn.jsdelivr.net/gh/novnc/noVNC@master/core/rfb.js';

function status(text) {{
    document.getElementById('status').textContent = text;
}}

// Ctrl+Alt+Del
document.addEventListener("DOMContentLoaded", function() {{
    document.getElementById('sendCtrlAltDelButton').onclick = function() {{
        if (rfb) {{
            rfb.sendCtrlAltDel();
        }}
    }};
}});

const host = "{vnc_host}";
const port = "{no_vnc_port_mapping}";
const password = "{vnc_password}";
const path = "websockify";

let url;
if (window.location.protocol === "https:") {{
    url = 'wss://';
}} else {{
    url = 'ws://';
}}
url += host + ":" + port + "/" + path;

let rfb;
document.addEventListener("DOMContentLoaded", () => {{
    status("Connecting");
    try {{
        rfb = new RFB(document.getElementById('screen'), url, {{
            credentials: {{ password: password }}
        }});
        rfb.addEventListener("connect", () => status("Connected to VNC"));
        rfb.addEventListener("disconnect", () => status("Disconnected"));
        rfb.viewOnly = false;
        rfb.scaleViewport = true;
    }} catch (err) {{
        console.error("VNC Connection Error:", err);
        status("Error: " + err);
    }}
}});
</script>
</body>
</html>
"""

    return Response(content=html_content, media_type="text/html")


###########################
# Local Dev
###########################
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
