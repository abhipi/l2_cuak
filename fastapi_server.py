from fastapi import FastAPI, Request, Response
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


@app.post("/start")
def start_and_stream(payload: dict):
    """
    Starts `browsing_agent.py` with the provided payload
    and streams its output for up to 60 seconds.

    Returns a Server-Sent Events (SSE) stream:
      - Each line of script output is sent as 'data: ...\n\n'
      - If 60s elapse, we terminate the script and end the stream.
      - Also returns a unique session_id and a link to a VNC page.
    """
    session_id = str(uuid.uuid4())
    payload["session_id"] = session_id
    payload_json = json.dumps(payload)

    # Start the script
    process = subprocess.Popen(
        ["python3", "browsing_agent.py", payload_json],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=os.setsid,  # Makes it a process group for clean termination
    )

    # Store session info
    SESSIONS[session_id] = {
        "process": process,
        "start_time": time.time(),
        "payload": payload,
    }

    vnc_url = f"http://{os.getenv('PUBLIC_DNS','localhost')}:6081/vnc.html"

    async def event_stream():
        start_time = time.time()
        timeout_seconds = 60
        yield f"data: Session started: {session_id}\n\n"
        yield f"data: Access VNC at: {vnc_url}\n\n"

        while True:
            # Read both stdout and stderr
            stdout_line = process.stdout.readline()
            stderr_line = process.stderr.readline()
            elapsed = time.time() - start_time

            # Send stdout
            if stdout_line:
                yield f"data: {stdout_line.rstrip()}\n\n"
            # Send stderr
            if stderr_line:
                yield f"data: ERROR: {stderr_line.rstrip()}\n\n"

            # If process finishes, stop
            if process.poll() is not None:
                yield "data: Task completed.\n\n"
                break

            # Check timeout (if > 60s, kill process)
            if elapsed > timeout_seconds:
                yield "data: Task timed out after 60 seconds. Killing process...\n\n"
                os.killpg(
                    os.getpgid(process.pid), signal.SIGTERM
                )  # Kill entire process group
                break

            await asyncio.sleep(0.1)

        yield "event: close\ndata: end\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/vnc/{session_id}")
def get_vnc(session_id: str):
    """
    Return an HTML page linking to the noVNC session.
    Because of ALB sticky sessions, the user
    must land on the same instance that started their job.
    """
    if session_id not in SESSIONS:
        return {"error": "Session not found or expired"}

    # Example noVNC link
    vnc_url = f"http://{os.getenv('PUBLIC_DNS','localhost')}:6081/vnc.html"

    html_content = f"""
    <html>
        <head><title>NoVNC Session {session_id}</title></head>
        <body>
            <h3>NoVNC Connection for Session {session_id}</h3>
            <p><a href="{vnc_url}" target="_blank">Open VNC</a></p>
        </body>
    </html>
    """
    return Response(content=html_content, media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
