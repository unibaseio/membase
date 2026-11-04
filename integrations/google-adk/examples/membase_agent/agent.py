"""A minimal ADK agent with Membase memory.

pip install membase-google-adk
export MEMBASE_API_KEY=mbk_...      # Connect > Developer keys in the Membase app
export GOOGLE_API_KEY=...           # or another model ADK supports
adk run examples/membase_agent
"""

from google.adk.agents import Agent

from membase_google_adk import membase_tools

root_agent = Agent(
    name="membase_agent",
    model="gemini-3.5-flash",
    instruction=(
        "You are a helpful assistant with long-term memory. Search Membase before answering anything "
        "that may depend on what the user told you before, and cite the Memory in brackets. Save to "
        "Membase only when the user asks you to remember something."
    ),
    tools=membase_tools(),
)
