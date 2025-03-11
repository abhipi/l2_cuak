from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import subprocess
import uuid
import time
import asyncio
import json
import os
import signal

app = FastAPI()

# In-memory dict to store session data (process, etc.)
SESSIONS = {}

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict this
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    """Health Check for ALB (or general monitoring)."""
    return {"status": "ok"}


###########################
# Endpoint to Stream the Agent Payload
###########################
@app.post("/start")
def start_and_stream(payload: dict):
    """Starts `browsing_agent.py` with the provided payload and streams its output."""

    session_id = str(uuid.uuid4())

    session_id = str(uuid.uuid4())  # Generate session ID
    SESSIONS[session_id] = {"start_time": time.time()}  # Store session info

    # Ensure JSON is properly formatted
    payload_str = json.dumps(
        payload, separators=(",", ":")
    )  # Ensures valid JSON without unnecessary whitespace

    # Create a subprocess that we can read line-by-line
    process = subprocess.Popen(
        [
            "pipenv",
            "run",
            "python",
            "-u",
            "browsing_agent.py",
            payload_str,
        ],  # Add `-u` flag
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line buffering
        universal_newlines=True,
    )

    async def stream_generator():
        """Streams the output of `browsing_agent.py` in real-time and force-closes at 60s."""
        yield f"data: Session started with ID: {session_id}.\n\n"

        start_time = time.time()
        timeout_seconds = 80  # Timeout after 80s

        try:
            while True:
                elapsed = time.time() - start_time

                # Create tasks for reading stdout and stderr
                stdout_task = asyncio.create_task(
                    asyncio.to_thread(process.stdout.readline)
                )
                stderr_task = asyncio.create_task(
                    asyncio.to_thread(process.stderr.readline)
                )

                # Wait for either stdout or stderr to be ready, or timeout after 0.1s
                done, pending = await asyncio.wait(
                    {stdout_task, stderr_task},
                    timeout=0.1,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Process completed tasks
                for task in done:
                    line = task.result().strip()
                    if line:
                        yield f"data: {line}\n\n"

                # Cancel any pending tasks to avoid resource leaks
                for task in pending:
                    task.cancel()

                # Check if timeout has been reached
                if elapsed > timeout_seconds:
                    yield "data: Task timed out after 60s, killing process...\n\n"
                    process.terminate()  # Attempt graceful shutdown
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()  # Force kill if still running
                    yield "event: close\ndata: end\n\n"
                    return  # Exit the generator immediately

                # Check if the process finished naturally
                if process.poll() is not None:
                    yield "data: Task completed.\n\n"
                    yield "event: close\ndata: end\n\n"
                    return  # Exit the generator immediately

                # Brief sleep to avoid high CPU usage
                await asyncio.sleep(0.05)
        finally:
            if session_id in SESSIONS:
                del SESSIONS[session_id]
                print(f"Session {session_id} removed from SESSIONS.")

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


@app.get("/vnc/{session_id}")
def get_vnc(session_id: str):
    """
    Return an HTML page embedding a noVNC viewer that auto-injects a VNC password.
    The page uses the environment variable VNC_PASSWORD.
    """
    if session_id not in SESSIONS:
        return {"error": "Session not found or expired"}

    # Retrieve host information and VNC password from environment variables
    vnc_host = os.getenv("PUBLIC_DNS", "52.86.72.216")
    vnc_port = os.getenv(
        "VNC_PORT", "6081"
    )  # You can also set this in your environment
    vnc_password = os.getenv("VNC_PASSWORD", "12345678")

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
    <!-- Import noVNC's RFB module from a stable CDN -->
    <script type="module">
        import RFB from 'https://cdn.jsdelivr.net/gh/novnc/noVNC@master/core/rfb.js';

        // Function to update status in the top bar
        function status(text) {{
            document.getElementById('status').textContent = text;
        }}

        // Event listener for the Ctrl+Alt+Del button
        document.addEventListener("DOMContentLoaded", function() {{
            document.getElementById('sendCtrlAltDelButton').onclick = function() {{
                if (rfb) {{
                    rfb.sendCtrlAltDel();
                }}
            }};
        }});

        // Set connection parameters using environment-injected defaults
        const host = "{vnc_host}";
        const port = "{vnc_port}";
        const password = "{vnc_password}";
        const path = "websockify";  // Adjust if your noVNC proxy uses a different path

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
