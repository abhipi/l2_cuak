###########################
# main.py (or app.py)
###########################
import os
import time
import json
import uuid
import asyncio
import requests
import subprocess

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

import docker  # Make sure you have installed: pip install docker

###########################
# Create our FastAPI app
###########################
app = FastAPI()

# Docker client (default from_env)
docker_client = docker.from_env()

# In-memory dict to store session data (container info, start time, etc.)
SESSIONS = {}

# Global session timeout in seconds
TIMEOUT = 80

# Enable CORS (adjust for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


###########################
# Healthcheck Endpoint
###########################
@app.get("/health")
def health_check():
    """Health Check for ALB (or general monitoring)."""
    return {"status": "ok"}


###########################
# Helper: Get Instance Public Hostname
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
# Start & Stream Agent
###########################
@app.post("/start")
def start_and_stream(payload: dict):
    """
    1) Launches a dedicated Docker container running Chrome + noVNC.
    2) Finds the ephemeral ports assigned for CDP and noVNC.
    3) Passes the cdp_url to browsing_agent.py.
    4) Streams the browsing_agent.py output in real time.
    5) Cleans up on completion or timeout.
    """

    # Generate a new session ID
    session_id = str(uuid.uuid4())
    container_name = f"chrome_instance_{session_id}"

    # 1) Launch the Docker container
    #    We'll map ports dynamically (Docker will pick ephemeral host ports).
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

    # Give the container a moment to spin up (optional, depends on image startup time)
    time.sleep(2)

    # 2) Retrieve ephemeral ports from container
    container.reload()  # Refresh container.attrs
    ports_info = container.attrs["NetworkSettings"]["Ports"]

    # We assume each entry looks like "9333/tcp": [{"HostIp": "0.0.0.0", "HostPort": "49158"}]
    cdp_port_mapping = ports_info["9333/tcp"][0]["HostPort"]
    no_vnc_port_mapping = ports_info["6080/tcp"][0]["HostPort"]
    vnc_port_mapping = ports_info["5900/tcp"][0]["HostPort"]

    # 3) Construct the CDP URL; often "ws://HOST:CDP_PORT/devtools/browser"
    #    But you may need to adjust the path if your container expects a certain path.
    host_for_cdp = os.getenv("PUBLIC_DNS", get_instance_public_hostname())
    cdp_url = f"ws://{host_for_cdp}:{cdp_port_mapping}"

    # Insert the cdp_url into the payload so browsing_agent.py can use it
    payload["cdp_url"] = cdp_url

    # 4) Store session data in SESSIONS
    SESSIONS[session_id] = {
        "start_time": time.time(),
        "container_id": container.id,
        "cdp_port": cdp_port_mapping,
        "no_vnc_port": no_vnc_port_mapping,
        "vnc_port": vnc_port_mapping,
    }

    # Convert payload to JSON string for Popen
    payload_str = json.dumps(payload, separators=(",", ":"))

    # 5) Start browsing_agent.py as a subprocess
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

    # 6) Define the streaming generator
    async def stream_generator():
        yield f"data: Session started with ID: {session_id}. Container: {container_name}\n\n"
        yield f"data: cdp_url: {cdp_url}\n\n"

        start_time = time.time()

        try:
            while True:
                elapsed = time.time() - start_time

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
                    line = task.result().strip()
                    if line:
                        yield f"data: {line}\n\n"

                # Cancel pending tasks
                for task in pending:
                    task.cancel()

                # Check if process timed out
                if elapsed > TIMEOUT:
                    yield "data: Task timed out, killing process & container...\n\n"
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()

                    # Cleanup Docker container
                    container.stop()
                    container.remove(force=True)

                    yield "event: close\ndata: end\n\n"
                    return

                # Check if process has finished
                if process.poll() is not None:
                    yield "data: Task completed. Stopping container...\n\n"
                    container.stop()
                    container.remove(force=True)
                    yield "event: close\ndata: end\n\n"
                    return

                # Avoid busy loop
                await asyncio.sleep(0.05)

        finally:
            # Remove session from memory
            if session_id in SESSIONS:
                del SESSIONS[session_id]
                print(f"Session {session_id} removed from SESSIONS.")

    # 7) Return a StreamingResponse
    return StreamingResponse(stream_generator(), media_type="text/event-stream")


###########################
# VNC Endpoint
###########################
@app.get("/vnc/{session_id}")
def get_vnc(session_id: str):
    """
    Returns an HTML page embedding a noVNC viewer using the ephemeral port
    assigned to the container for this session.
    """

    if session_id not in SESSIONS:
        return {"error": "Session not found or expired"}

    # Retrieve container & ephemeral port info
    session_data = SESSIONS[session_id]
    container_id = session_data["container_id"]

    # Reload container to ensure we have the latest port mapping
    container = docker_client.containers.get(container_id)
    container.reload()
    ports_info = container.attrs["NetworkSettings"]["Ports"]

    # Get the ephemeral port for noVNC (6080 -> e.g. 49160)
    no_vnc_port_mapping = ports_info["6080/tcp"][0]["HostPort"]

    # Build the final host.
    # If you're on AWS, we attempt to get the public DNS, otherwise fallback to localhost.
    vnc_host = os.getenv("PUBLIC_DNS", get_instance_public_hostname())

    # Use the same password the container expects (may be from env)
    vnc_password = os.getenv("VNC_PASSWORD", "12345678")

    # Construct an HTML page that references the correct noVNC websocket
    # We use the ephemeral port in place of 6081 or 6080
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
    <!-- Import noVNC's RFB module from a stable CDN (master branch) -->
    <script type="module">
        import RFB from 'https://cdn.jsdelivr.net/gh/novnc/noVNC@master/core/rfb.js';

        function status(text) {{
            document.getElementById('status').textContent = text;
        }}

        // Ctrl+Alt+Del button
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
        const path = "websockify";  // Adjust if your container uses a different path

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
</head>
<body>
    <div id="top_bar">
        <div id="status">Loading</div>
        <div id="sendCtrlAltDelButton">Send CtrlAltDel</div>
    </div>
    <div id="screen"></div>
</body>
</html>
"""
    return Response(content=html_content, media_type="text/html")


###########################
# Local Dev Entry Point
###########################
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
