"""Function-calling agent built with LangChain."""

import sys
from pathlib import Path

# Make backend/src importable when LangGraph loads agent.py by file path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain.agents import create_agent

from tools import ALL_TOOLS

# create_agent with "provider:model" string uses init_chat_model internally,
# which reads ANTHROPIC_API_KEY and ANTHROPIC_API_URL from env automatically.
# Temperature defaults to 0; adjust via ANTHROPIC_TEMPERATURE env var if needed.
graph = create_agent(
    model="anthropic:glm-5.1",
    tools=ALL_TOOLS,
    system_prompt=(
        "你是一个有用的个人AI助手。你可以使用工具来帮助用户完成任务。请用中文回复，除非用户明确要求使用其他语言。\n"
        "在调用任何工具之前，你必须先用简短的文字告诉用户你打算做什么，例如：'我来帮你查一下北京的天气'、'让我计算一下这个表达式'。"
    ),
)
