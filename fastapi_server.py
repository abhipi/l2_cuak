import os
import time
import json
import uuid
import asyncio
import requests
import subprocess

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

# Session info by session_id
# Each entry: {
#   "start_time": float,
#   "last_active": float,
#   "container_id": str,
#   "cdp_port": str,
#   "no_vnc_port": str,
#   "vnc_port": str,
#   "user_ip": str,
#   "process": subprocess.Popen or None
# }
SESSIONS = {}

# Map user_ip -> session_id
IP_SESSIONS = {}

# Timeout for container inactivity (5 minutes)
TIMEOUT = 300


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
def get_instance_public_hostname():
    """
    Attempt to fetch the AWS EC2 public hostname via metadata.
    Fallback to localhost if not available or not on AWS.
    """
    try:
        response = requests.get(
            "http://169.254.169.254/latest/meta-data/public-hostname", timeout=2
        )
        if response.status_code == 200:
            return response.text.strip()
    except Exception as e:
        print("Error retrieving instance public hostname:", e)
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
    remove the container and delete the session from memory.
    """
    while True:
        now = time.time()
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

                # Remove from memory
                SESSIONS.pop(session_id, None)
                user_ip = session_data["user_ip"]
                if IP_SESSIONS.get(user_ip) == session_id:
                    IP_SESSIONS.pop(user_ip, None)
        await asyncio.sleep(60)


###########################
# Start & Stream Agent
###########################
@app.post("/start")
async def start_and_stream(payload: dict, request: Request):
    """
    1) Check if there's an existing container for this IP still within TIMEOUT.
       - If yes, REUSE that container.
       - Otherwise, spin up a new one.
    2) Start browsing_agent.py as a subprocess referencing the container's CDP URL.
    3) Stream browsing_agent.py output in real-time (SSE).
    4) Do NOT remove container on finish. Instead, rely on the 5-min inactivity cleanup.
    """

    user_ip = request.headers.get("x-forwarded-for", request.client.host)
    print(f"Incoming /start from IP: {user_ip}")

    # 1) Check for existing active session
    existing_session_id = IP_SESSIONS.get(user_ip)
    session_id = None
    container = None
    reuse = False

    if existing_session_id:
        # See if it's still in SESSIONS
        existing_data = SESSIONS.get(existing_session_id)
        if existing_data:
            elapsed = time.time() - existing_data["last_active"]
            if elapsed < TIMEOUT:
                # Great, reuse this container
                reuse = True
                container = docker_client.containers.get(existing_data["container_id"])
                session_id = existing_session_id
                # Update last_active
                existing_data["last_active"] = time.time()
                print(f"Reusing container {container.name} for IP {user_ip}")
            else:
                # It's stale; remove from memory and kill container
                try:
                    container = docker_client.containers.get(
                        existing_data["container_id"]
                    )
                    container.stop()
                    container.remove(force=True)
                except:
                    pass
                # Remove from session tracking
                SESSIONS.pop(existing_session_id, None)
                IP_SESSIONS.pop(user_ip, None)

    # If no reuse, spin up a new container
    if not reuse:
        session_id = str(uuid.uuid4())
        container_name = f"chrome_instance_{session_id}"

        print(f"Starting container: {container_name}")
        container = docker_client.containers.run(
            "abhipi04/custom-chrome-novnc",
            detach=True,
            name=container_name,
            ports={
                "5900/tcp": None,  # VNC
                "9333/tcp": None,  # CDP
                "6080/tcp": None,  # noVNC
            },
            shm_size="2g",
        )
        # Give the container a moment to spin up
        time.sleep(2)
        container.reload()

        # Retrieve ephemeral ports from container
        ports_info = container.attrs["NetworkSettings"]["Ports"]
        cdp_port_mapping = ports_info["9333/tcp"][0]["HostPort"]
        no_vnc_port_mapping = ports_info["6080/tcp"][0]["HostPort"]
        vnc_port_mapping = ports_info["5900/tcp"][0]["HostPort"]

        SESSIONS[session_id] = {
            "start_time": time.time(),
            "last_active": time.time(),
            "container_id": container.id,
            "cdp_port": cdp_port_mapping,
            "no_vnc_port": no_vnc_port_mapping,
            "vnc_port": vnc_port_mapping,
            "user_ip": user_ip,
            "process": None,  # We'll store the subprocess handle here
        }
        IP_SESSIONS[user_ip] = session_id
    else:
        # We have a session_id and container from reuse
        # We'll re-load ephemeral ports from container
        container.reload()
        ports_info = container.attrs["NetworkSettings"]["Ports"]
        cdp_port_mapping = ports_info["9333/tcp"][0]["HostPort"]
        no_vnc_port_mapping = ports_info["6080/tcp"][0]["HostPort"]
        vnc_port_mapping = ports_info["5900/tcp"][0]["HostPort"]

        # Update session dictionary, just in case
        SESSIONS[session_id].update(
            {
                "last_active": time.time(),
                "cdp_port": cdp_port_mapping,
                "no_vnc_port": no_vnc_port_mapping,
                "vnc_port": vnc_port_mapping,
            }
        )

    # 2) Construct the CDP URL & pass to the agent
    host_for_cdp = "localhost"
    cdp_url = f"http://{host_for_cdp}:{cdp_port_mapping}"
    payload["cdp_url"] = cdp_url

    # If there's an existing process, we can either kill it or let it keep running.
    # For simplicity, let's kill any old process if it's still around, then start a new one.
    old_proc = SESSIONS[session_id].get("process")
    if old_proc and old_proc.poll() is None:
        # process is still running; let's kill it
        print(f"Killing old agent process for session {session_id}")
        old_proc.terminate()
        try:
            old_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            old_proc.kill()

    # 3) Start browsing_agent.py as subprocess
    payload_str = json.dumps(payload, separators=(",", ":"))
    process = subprocess.Popen(
        [
            "pipenv",
            "run",
            "python",
            "-u",
            "browsing_agent.py",
            payload_str,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    # Store the new process in session
    SESSIONS[session_id]["process"] = process

    # 4) The streaming generator
    async def stream_generator():
        yield f"data: Session started with ID: {session_id}. Container: {container.name}\n\n"
        yield f"data: cdp_url: {cdp_url}\n\n"

        start_time = time.time()

        try:
            while True:
                elapsed = time.time() - start_time

                # Update last_active so the session doesn't get cleaned up
                SESSIONS[session_id]["last_active"] = time.time()

                # Read from stdout / stderr concurrently
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

                # Handle any completed tasks
                for task in done:
                    line = task.result().rstrip("\n")
                    if line:
                        yield f"data: {line}\n\n"

                # Cancel any pending tasks
                for task in pending:
                    task.cancel()

                # Check if we've exceeded the subprocess time limit
                if elapsed > TIMEOUT:
                    yield f"data: Subprocess timed out after {TIMEOUT}s.\n\n"
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    yield "event: close\ndata: end\n\n"
                    return

                # Check if process finished
                if process.poll() is not None:
                    yield "data: Subprocess finished.\n\n"
                    yield "event: close\ndata: end\n\n"
                    return

                # Avoid busy loop
                await asyncio.sleep(0.05)

        finally:
            # Mark the process reference as None
            if SESSIONS.get(session_id):
                SESSIONS[session_id]["process"] = None
            print(f"Agent process ended for session {session_id}")

    # 5) Return the SSE streaming response
    # Custom cookie returned to use by the ALB
    # headers = {
    #     "Set-Cookie": f"SessionStickiness={session_id}; Path=/; Secure; SameSite=None; HttpOnly"
    # }
    # Headers removed (TEST)
    return StreamingResponse(stream_generator(), media_type="text/event-stream")


###########################
# VNC Endpoint
###########################
@app.get("/vnc/{session_id}")
def get_vnc(session_id: str, request: Request):
    """
    Returns a noVNC HTML client for the ephemeral port assigned to the container.
    """
    user_ip = request.headers.get("x-forwarded-for", request.client.host)
    if session_id not in SESSIONS:
        return Response(content="Session not found or expired.", media_type="text/html")

    # Optional: Enforce the IP match if you want strict per-IP usage
    session_data = SESSIONS[session_id]
    if session_data["user_ip"] != user_ip:
        return Response(
            content="Session does not belong to your IP.", media_type="text/html"
        )

    # Update last_active
    session_data["last_active"] = time.time()

    # Reload container to confirm ports
    container_id = session_data["container_id"]
    try:
        container = docker_client.containers.get(container_id)
        container.reload()
    except:
        return Response(content="Container no longer running.", media_type="text/html")

    ports_info = container.attrs["NetworkSettings"]["Ports"]
    no_vnc_port_mapping = ports_info["6080/tcp"][0]["HostPort"]
    vnc_host = os.getenv("PUBLIC_DNS", get_instance_public_hostname())
    vnc_password = os.getenv("VNC_PASSWORD", "12345678")

    # Construct the HTML
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
