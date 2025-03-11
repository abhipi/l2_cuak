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
    session_id = str(uuid.uuid4())
    payload_str = json.dumps(payload)

    # Create a subprocess that we can read line-by-line
    process = subprocess.Popen(
        ["pipenv", "run", "python", "browsing_agent.py", payload_str],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line buffered
        universal_newlines=True,
    )

    start_time = time.time()
    timeout_seconds = 60

    async def stream_generator():
        # Stream until process ends or 60s pass
        while True:
            elapsed = time.time() - start_time

            # Read one line (if any) from stdout
            line = await asyncio.to_thread(process.stdout.readline)
            # Read any errors
            err_line = await asyncio.to_thread(process.stderr.readline)

            # If we got a normal line, yield it immediately
            if line:
                yield f"data: {line.rstrip()}\n\n"

            # If we got an error line, yield that too
            if err_line:
                yield f"data: ERROR: {err_line.rstrip()}\n\n"

            # If the process ended, break
            if process.poll() is not None:
                yield "data: Task completed.\n\n"
                break

            # If we've timed out, kill the process
            if elapsed > timeout_seconds:
                yield "data: Task timed out after 60s, killing process.\n\n"
                process.kill()
                break

            # Pause slightly to avoid spinning the CPU
            await asyncio.sleep(0.05)

        # End SSE
        yield "event: close\ndata: end\n\n"

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


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
