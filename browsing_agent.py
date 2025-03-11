import os
import sys
import asyncio
import json

# Ensure this matches your project structure
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_openai import ChatOpenAI
from browser_use import Agent
from dotenv import load_dotenv
from browser_use.browser.browser import Browser, BrowserConfig

# ------------------------------------------------------------------------
# Load environment variables
# ------------------------------------------------------------------------
load_dotenv()
if not os.getenv("OPENAI_API_KEY"):
    raise ValueError("OPENAI_API_KEY is not set")


# ------------------------------------------------------------------------
# Parse Payload from sys.argv
# ------------------------------------------------------------------------
def parse_payload_from_argv():
    """Read the first command-line argument and parse it as JSON."""
    if len(sys.argv) < 2:
        print("⚠️ No payload received! Using default task.")
        return {
            "task": "Navigate to 'https://en.wikipedia.org/wiki/Internet' and scroll."
        }

    payload_str = sys.argv[1]
    print(f"📜 Received input: {payload_str}")

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        print("⚠️ Invalid JSON input! Treating as raw task string.")
        payload = {"task": payload_str}

    return payload


# ------------------------------------------------------------------------
# Main Logic
# ------------------------------------------------------------------------
async def main():
    """Main async function to run the agent"""
    print("🚀 Starting browsing agent...")

    # 1. Parse payload
    payload = parse_payload_from_argv()
    task_text = payload.get("task", "")
    model_name = payload.get("model", "gpt-4o")  # Default model

    print(f"📝 Task: {task_text}")
    print(f"🧠 Model: {model_name}")

    if not task_text:
        print("⚠️ No valid task found. Exiting.")
        return

    # 2. Initialize LLM and Agent
    try:
        llm = ChatOpenAI(model=model_name)
        agent = Agent(
            task=task_text,
            llm=llm,
            browser=Browser(
                config=BrowserConfig(
                    headless=False,  # Change to True if needed
                    chrome_instance_path="/usr/bin/google-chrome",
                )
            ),
        )

        # 3. Run the agent
        print("🖥️ Launching agent...")
        await agent.run()
        print("✅ Task completed successfully.")
    except Exception as e:
        print(f"❌ Error during execution: {e}")


if __name__ == "__main__":
    asyncio.run(main())  # ✅ UNCOMMENTED: Now the script actually runs!
