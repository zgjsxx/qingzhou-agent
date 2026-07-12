"""Synchronous subagent support for isolated subtasks."""

from __future__ import annotations

import os
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langchain.agents import create_agent

from agent.context import AgentContextCompactMiddleware
from agent.context_references import AgentContextReferenceMiddleware
from agent.logging import AgentLoggingMiddleware, is_agent_logging_enabled
from agent.permissions import AgentPermissionMiddleware
from agent.skills import skill_catalog_for_prompt
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


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    user_content = task_description
    if cwd:
        user_content = f"{task_description}\n\nUse cwd for file and shell tools when relevant: {cwd}"

    result = _invoke_subagent_in_isolated_thread(user_content, cwd, recursion_limit)
    return _extract_final_text(result)


def _invoke_subagent_in_isolated_thread(user_content: str, cwd: str, recursion_limit: int) -> Any:
    # 子 Agent 必须谨慎处理 LangGraph 的 runnable context。
    #
    # LangChain/LangGraph 会把当前 run 的 config 放进 ContextVar
    # var_child_runnable_config。工具函数是在主 Agent 的 runnable context 里
    # 执行的，因此如果直接在同一 context 中调用另一个 graph.invoke()，子 graph
    # 的 ensure_config() 会隐式继承父 config。这个父 config 里包含
    # CONFIG_KEY_RUNTIME / stream_writer / CONFIG_KEY_STREAM，结果就是子 Agent
    # 内部的 read_file、run_shell_command 等工具事件会被写入父 Agent 的 SSE
    # stream，前端会临时看到子 Agent 的内部过程。
    #
    # 默认路径使用普通 ThreadPoolExecutor 线程执行 invoke()。普通新线程不会自动
    # 继承当前 ContextVar，所以子 Agent 只能看到我们显式传入 graph.invoke()
    # 的 config，不会继承父 stream。这样可以保持 UI 只展示 run_subagent 这一
    # 个工具调用和最终结果。
    #
    # 调试时可设置 AGENT_SUBAGENT_STREAM_TO_PARENT=true。此时会显式
    # copy_context().run(invoke)，把父 runnable context 带入子线程，用来观察
    # 子 Agent 内部工具调用是否进入父 stream。该模式主要用于定位问题，默认关闭。
    def invoke() -> Any:
        middleware = [
            AgentContextReferenceMiddleware(),
            AgentContextCompactMiddleware(),
            AgentPermissionMiddleware(interactive=False),
        ]
        if is_agent_logging_enabled():
            middleware.append(AgentLoggingMiddleware(agent_name="subagent"))

        graph = create_agent(
            model=_llm_model_spec(),
            tools=SUBAGENT_TOOLS,
            middleware=middleware,
            system_prompt=_subagent_system_prompt(cwd),
        )
        return graph.invoke(
            {"messages": [{"role": "user", "content": user_content}]},
            config={
                "recursion_limit": recursion_limit,
                "callbacks": [],
                "tags": ["subagent"],
            },
        )

    stream_to_parent = _bool_env("AGENT_SUBAGENT_STREAM_TO_PARENT", False)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="subagent") as executor:
        if stream_to_parent:
            ctx = copy_context()
            return executor.submit(ctx.run, invoke).result()
        return executor.submit(invoke).result()
