"""Function-calling agent built with LangChain."""

import sys
import os
from pathlib import Path

# Make backend/src importable when LangGraph loads agent.py by file path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain.agents import create_agent

from agent_logging import AgentLoggingMiddleware, is_agent_logging_enabled
from agent_permissions import AgentPermissionMiddleware
from skills import skill_catalog_for_prompt
from tools import ALL_TOOLS

LLM_ADAPTER_TYPE = os.getenv("LLM_ADAPTER_TYPE", "anthropic").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "glm-5.1").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()


def configure_llm_provider_env() -> None:
    """Map project-level LLM_* settings to provider-specific LangChain env vars."""
    provider = LLM_ADAPTER_TYPE.lower()

    if provider == "anthropic":
        if LLM_API_KEY:
            os.environ["ANTHROPIC_API_KEY"] = LLM_API_KEY
        if LLM_BASE_URL:
            os.environ["ANTHROPIC_API_URL"] = LLM_BASE_URL
    elif provider == "openai":
        if LLM_API_KEY:
            os.environ["OPENAI_API_KEY"] = LLM_API_KEY
        if LLM_BASE_URL:
            os.environ["OPENAI_BASE_URL"] = LLM_BASE_URL


configure_llm_provider_env()

SKILL_CATALOG = skill_catalog_for_prompt()

middleware = [AgentPermissionMiddleware()]

# Keep interaction logging opt-in so normal chat requests do not create JSONL files.
if is_agent_logging_enabled():
    middleware.append(AgentLoggingMiddleware())

graph = create_agent(
    model=f"{LLM_ADAPTER_TYPE}:{LLM_MODEL}",
    tools=ALL_TOOLS,
    middleware=middleware,
    system_prompt=(
        "你是一个有用的个人AI助手。你可以使用工具来帮助用户完成任务。请用中文回复，除非用户明确要求使用其他语言。\n"
        "在调用任何工具之前，你必须先用简短的文字告诉用户你打算做什么，例如：'我来帮你查一下北京的天气'、'让我计算一下这个表达式'。\n"
        "当任务包含多个步骤、需要修改代码、排查问题、比较方案或持续跟进进度时，请先调用 todo_write 写出简短任务清单。"
        "执行过程中每完成一个阶段或切换当前重点时，应再次调用 todo_write 更新状态。"
        "todo 状态只能使用 pending、in_progress、completed。简单问答或一次性工具调用不需要使用 todo_write。\n"
        "可用技能目录如下，只包含名称和简要说明；需要使用某个技能时，先调用 load_skill(name) 获取完整 SKILL.md 内容，不要假设你已经知道完整规则。\n"
        f"{SKILL_CATALOG}"
    ),
)
