"""Synchronous subagent support for isolated subtasks."""

from __future__ import annotations

import os
from typing import Any

from langchain.agents import create_agent

from agent_context import AgentContextCompactMiddleware
from agent_permissions import AgentPermissionMiddleware
from skills import skill_catalog_for_prompt
from tools import edit_file, glob_files, load_skill, read_file, run_shell_command, write_file

SUBAGENT_TOOLS = [
    load_skill,
    read_file,
    write_file,
    edit_file,
    glob_files,
    run_shell_command,
]


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _llm_model_spec() -> str:
    adapter = os.getenv("LLM_ADAPTER_TYPE", "anthropic").strip()
    model = os.getenv("LLM_MODEL", "glm-5.1").strip()
    return f"{adapter}:{model}"


def _extract_final_text(result: Any) -> str:
    messages = result.get("messages", []) if isinstance(result, dict) else getattr(result, "messages", [])
    if not messages:
        return "Subagent finished without returning messages."

    final = messages[-1]
    content = getattr(final, "content", "")
    if isinstance(content, str):
        return content.strip() or "Subagent finished with an empty response."
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        text = "\n".join(parts).strip()
        if text:
            return text

    return str(content).strip() or "Subagent finished with an empty response."


def _subagent_system_prompt(cwd: str = "") -> str:
    cwd_note = f"\nWorking directory hint: {cwd}" if cwd else ""
    return (
        "你是主 Agent 启动的同步子 Agent，负责独立完成一个明确子任务。"
        "你有全新的上下文；中间探索过程不会返回给主 Agent。"
        "可以使用工具读取文件、搜索、运行命令或修改文件，但不要再委派其他 Agent。"
        "完成后只返回简洁、具体的结论，包含关键发现、做过的修改、验证结果和建议的下一步。"
        "如果需要使用技能，先调用 load_skill(name) 读取完整说明。"
        f"{cwd_note}\n"
        "可用技能目录如下：\n"
        f"{skill_catalog_for_prompt()}"
    )


def spawn_subagent(description: str, cwd: str = "", max_steps: int | None = None) -> str:
    """Run an isolated synchronous subagent and return only its final conclusion."""
    task_description = str(description or "").strip()
    if not task_description:
        return "Error: subagent task description must not be empty."

    steps = max_steps if max_steps is not None else _int_env("AGENT_SUBAGENT_MAX_STEPS", 30)
    steps = max(1, min(int(steps), _int_env("AGENT_SUBAGENT_MAX_STEPS_LIMIT", 60)))
    recursion_limit = max(4, steps * 2 + 2)

    graph = create_agent(
        model=_llm_model_spec(),
        tools=SUBAGENT_TOOLS,
        middleware=[AgentContextCompactMiddleware(), AgentPermissionMiddleware()],
        system_prompt=_subagent_system_prompt(cwd),
    )

    user_content = task_description
    if cwd:
        user_content = f"{task_description}\n\nUse cwd for file and shell tools when relevant: {cwd}"

    result = graph.invoke(
        {"messages": [{"role": "user", "content": user_content}]},
        config={"recursion_limit": recursion_limit},
    )
    return _extract_final_text(result)
