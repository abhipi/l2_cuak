# Update (03/20/2025- CDP URL is now passed as a parameter in the JSON payload for containerized chrome)
import os
import sys
import asyncio
import json

# Add parent directory to the Python path if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_openai import ChatOpenAI
from browser_use import Agent
from dotenv import load_dotenv
from browser_use.browser.browser import Browser, BrowserConfig

# ------------------------------------------------------------------------------
# Load environment variables
# ------------------------------------------------------------------------------
load_dotenv()
if not os.getenv("OPENAI_API_KEY"):
    raise ValueError("OPENAI_API_KEY is not set")


# ------------------------------------------------------------------------------
# Function to parse payload from sys.argv
# ------------------------------------------------------------------------------
def parse_payload_from_argv():
    """
    Reads the first argument from sys.argv and parses as JSON.
    Supports:
      python browsing_agent.py '{"task": "...", "cdp_url": "..."}'
    """
    if len(sys.argv) < 2:
        return {
            "task": "Navigate to 'https://en.wikipedia.org/wiki/Internet' and scroll to a specific string.",
            "cdp_url": os.getenv("CDP_URL", "http://localhost:9333"),
        }

    payload_str = sys.argv[1]
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        payload = {
            "task": payload_str,
            "cdp_url": os.getenv("CDP_URL", "http://localhost:9333"),
        }

    if "cdp_url" not in payload:
        payload["cdp_url"] = os.getenv("CDP_URL", "http://localhost:9333")

    return payload


# ------------------------------------------------------------------------------
# Main Logic
# ------------------------------------------------------------------------------
async def main():
    # Parse payload from argument
    payload = parse_payload_from_argv()
    task_text = payload.get("task", "")
    model_name = payload.get("model", "gpt-4o")  # default fallback
    cdp_url = payload.get("cdp_url", "http://localhost:9333")

    # Initialize LLM and Agent
    llm = ChatOpenAI(model=model_name)
    agent = Agent(
        task=task_text,
        llm=llm,
        browser=Browser(
            config=BrowserConfig(
                headless=False,
                wss_url=cdp_url,  # Pulled from JSON payload
            )
        ),
    )

    # Run the agent
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
