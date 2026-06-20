"""Runtime system prompt assembly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROMPT_SECTIONS = {
    "identity": (
        "你是一个有用的个人AI助手。你可以使用工具来帮助用户完成任务。"
        "请用中文回复，除非用户明确要求使用其他语言。"
    ),
    "tool_preamble": (
        "在调用任何工具之前，你必须先用简短的文字告诉用户你打算做什么，"
        "例如：'我来帮你查一下北京的天气'、'让我计算一下这个表达式'。"
    ),
    "todo": (
        "当任务包含多个步骤、需要修改代码、排查问题、比较方案或持续跟进进度时，"
        "请先调用 todo_write 写出简短任务清单。"
        "执行过程中每完成一个阶段或切换当前重点时，应再次调用 todo_write 更新状态。"
        "todo 状态只能使用 pending、in_progress、completed。"
        "简单问答或一次性工具调用不需要使用 todo_write。"
    ),
    "subagent": (
        "遇到复杂但相对独立的子问题时，可以调用 task(description) 启动子 Agent；"
        "子 Agent 会使用独立上下文完成任务并只返回结论。"
    ),
    "memory": (
        "当用户明确要求记住长期偏好、约束、项目事实或参考线索时，"
        "调用 remember 保存到持久记忆。"
    ),
    "skills": (
        "可用技能目录如下，只包含名称和简要说明；需要使用某个技能时，"
        "先调用 load_skill(name) 获取完整 SKILL.md 内容，不要假设你已经知道完整规则。"
    ),
}

_LAST_CONTEXT_KEY: str | None = None
_LAST_PROMPT: str | None = None


def _tool_names(tools: list[Any]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if name:
            names.append(str(name))
    return sorted(names)


def build_prompt_context(
    *,
    tools: list[Any],
    skill_catalog: str,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Build deterministic prompt context from current runtime state."""
    return {
        "tool_names": _tool_names(tools),
        "skill_catalog": skill_catalog.strip(),
        "workspace": str(workspace) if workspace else "",
    }


def assemble_system_prompt(context: dict[str, Any]) -> str:
    """Assemble the system prompt from independent sections."""
    tool_names = set(context.get("tool_names", []))
    sections = [
        PROMPT_SECTIONS["identity"],
        PROMPT_SECTIONS["tool_preamble"],
    ]

    if "todo_write" in tool_names:
        sections.append(PROMPT_SECTIONS["todo"])
    if "task" in tool_names:
        sections.append(PROMPT_SECTIONS["subagent"])
    if "remember" in tool_names:
        sections.append(PROMPT_SECTIONS["memory"])

    skill_catalog = str(context.get("skill_catalog", "")).strip()
    if "load_skill" in tool_names and skill_catalog:
        sections.append(f"{PROMPT_SECTIONS['skills']}\n{skill_catalog}")

    workspace = str(context.get("workspace", "")).strip()
    if workspace:
        sections.append(f"当前工作目录：{workspace}")

    return "\n".join(sections)


def get_system_prompt(context: dict[str, Any]) -> str:
    """Return cached assembled prompt when runtime context is unchanged."""
    global _LAST_CONTEXT_KEY, _LAST_PROMPT

    context_key = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
    if context_key == _LAST_CONTEXT_KEY and _LAST_PROMPT is not None:
        return _LAST_PROMPT

    _LAST_CONTEXT_KEY = context_key
    _LAST_PROMPT = assemble_system_prompt(context)
    return _LAST_PROMPT
