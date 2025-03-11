import os
import sys
import asyncio
import json

# Make sure these imports match your actual project structure
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
    Reads the first argument from sys.argv and tries to parse it as JSON.
    Fallback: If it's not valid JSON, treat it as a string containing the 'task'.

    Example call:
      python browsing_agent.py '{"task": "Go to https://google.com and scroll down"}'
    or
      python browsing_agent.py "Go to https://google.com and scroll down"
    """
    if len(sys.argv) < 2:
        # No payload passed in, use a default
        return {
            "task": "Navigate to 'https://en.wikipedia.org/wiki/Internet' and scroll to a specific string."
        }

    payload_str = sys.argv[1]
    try:
        payload = json.loads(payload_str)
        # Expecting something like: {"task": "...", "model": "..."}
    except json.JSONDecodeError:
        # Not valid JSON, assume the entire string is the task
        payload = {"task": payload_str}

    return payload


# ------------------------------------------------------------------------------
# Main Logic
# ------------------------------------------------------------------------------
async def main():
    # 1. Parse payload
    payload = parse_payload_from_argv()
    task_text = payload.get("task", "")
    model_name = payload.get("model", "gpt-4o")  # default to your GPT-4 alias

    # 2. Initialize LLM and Agent
    llm = ChatOpenAI(model=model_name)
    agent = Agent(
        task=task_text,
        llm=llm,
        browser=Browser(
            config=BrowserConfig(
                headless=False,  # or True, if you want a headless browser
                chrome_instance_path="/usr/bin/google-chrome",  # Adjust for your environment
            )
        ),
    )

    # 3. Run the agent
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
