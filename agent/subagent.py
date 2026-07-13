"""同步子 Agent 支持：用于在隔离上下文中执行独立子任务。"""

from __future__ import annotations

import os
import json
import time
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langgraph.errors import GraphRecursionError

from agent.config import config_str
from agent.context import AgentContextCompactMiddleware
from agent.context_references import AgentContextReferenceMiddleware
from agent.llm_config import configure_provider_env, provider_model_kwargs
from agent.logging import AgentLoggingMiddleware, is_agent_logging_enabled
from agent.permissions import AgentPermissionMiddleware
from agent.skills import skill_catalog_for_prompt
from tools import edit_file, glob_files, load_skill, read_file, run_shell_command, search_files, web_extract, web_search

# ── 子 Agent 工具集 ──────────────────────────────────────────────────
# 子 Agent 不能再次委派其他 Agent（不含 delegate_task / run_subagent），
# 这与 hermes-agent 的 DELEGATE_BLOCKED_TOOLS 设计一致，
# 防止无限嵌套和失控的委派树。
#
# readonly:      只读模式——读文件、搜索、glob、网页、shell 诊断命令。
# workspace_write: 同上 + edit_file，允许任务范围内的文件修改。
# 两个集合都不包含 delegate_task 或 run_subagent。
READONLY_SUBAGENT_TOOLS = [
    load_skill,
    read_file,
    glob_files,
    search_files,
    web_search,
    web_extract,
    run_shell_command,
]
WORKSPACE_WRITE_SUBAGENT_TOOLS = [*READONLY_SUBAGENT_TOOLS, edit_file]
SUBAGENT_TOOLS = WORKSPACE_WRITE_SUBAGENT_TOOLS
DELEGATE_MODES = {"readonly", "workspace_write"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        parsed = float(os.getenv(name, default))
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _llm_model_spec() -> str:
    adapter = os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip()
    model = os.getenv("LLM_MODEL", config_str("llm", "model", "glm-5.1")).strip()
    return f"{adapter}:{model}"


def _llm_model() -> Any:
    adapter = os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip()
    model = os.getenv("LLM_MODEL", config_str("llm", "model", "glm-5.1")).strip()
    api_key = os.getenv("LLM_API_KEY", config_str("llm", "apiKey", "")).strip()
    auth_token = os.getenv("LLM_AUTH_TOKEN", os.getenv("ANTHROPIC_AUTH_TOKEN", "")).strip()
    base_url = os.getenv("LLM_BASE_URL", config_str("llm", "baseUrl", "")).strip()
    configure_provider_env(adapter=adapter, api_key=api_key, auth_token=auth_token, base_url=base_url)
    model_spec = f"{adapter}:{model}"
    if auth_token and adapter.lower() == "anthropic":
        return init_chat_model(
            model_spec,
            **provider_model_kwargs(adapter=adapter, auth_token=auth_token, base_url=base_url),
        )
    return model_spec


def _direct_llm_model() -> Any:
    """不带 LangGraph runnable context 的独立 LLM 调用，仅供 fallback 总结使用。"""
    adapter = os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip()
    model = os.getenv("LLM_MODEL", config_str("llm", "model", "glm-5.1")).strip()
    api_key = os.getenv("LLM_API_KEY", config_str("llm", "apiKey", "")).strip()
    auth_token = os.getenv("LLM_AUTH_TOKEN", os.getenv("ANTHROPIC_AUTH_TOKEN", "")).strip()
    base_url = os.getenv("LLM_BASE_URL", config_str("llm", "baseUrl", "")).strip()
    configure_provider_env(adapter=adapter, api_key=api_key, auth_token=auth_token, base_url=base_url)
    return init_chat_model(
        f"{adapter}:{model}",
        **provider_model_kwargs(adapter=adapter, auth_token=auth_token, base_url=base_url),
    )


def _extract_final_text(result: Any) -> str:
    """从子 Agent 结果中提取最后一条消息的文本。"""
    messages = result.get("messages", []) if isinstance(result, dict) else getattr(result, "messages", [])
    if not messages:
        return "子 Agent 结束时未返回任何消息。"

    final = messages[-1]
    content = getattr(final, "content", "")
    if isinstance(content, str):
        return content.strip() or "子 Agent 结束时返回了空响应。"
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

    return str(content).strip() or "子 Agent 结束时返回了空响应。"


def _message_content_text(content: Any) -> str:
    """将消息 content（字符串或 content-block 列表）统一转为纯文本。"""
    if isinstance(content, str):
        return content
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
        return "\n".join(parts)
    return str(content)


def _messages_transcript(messages: list[Any], max_chars: int | None = None) -> str:
    """将消息列表转为结构化 transcript 文本，供 fallback 总结使用。

    超过 max_chars 时截取尾部（保留最新对话）。
    """
    limit = max_chars if max_chars is not None else _int_env("AGENT_SUBAGENT_FALLBACK_TRANSCRIPT_CHARS", 24000)
    lines: list[str] = []
    for message in messages:
        role = getattr(message, "type", None) or getattr(message, "role", None) or message.__class__.__name__
        content = getattr(message, "content", "")
        text = _message_content_text(content).strip()
        if not text:
            continue
        lines.append(f"[{role}]\n{text}")
    transcript = "\n\n".join(lines).strip()
    if limit > 0 and len(transcript) > limit:
        return transcript[-limit:]
    return transcript


def _fallback_summarize_recursion_limit(last_state: Any, exc: BaseException) -> dict[str, Any]:
    """当子 Agent 达到 LangGraph 递归上限时，用独立 LLM 从已有 transcript 生成最终总结。

    这是 hermes-agent 没有的功能——它只截断或放弃；
    我们用一次无工具的 LLM 调用从已收集的证据中尽力产出回答，
    即使不完整也比直接报错更有价值。
    """
    messages = last_state.get("messages", []) if isinstance(last_state, dict) else getattr(last_state, "messages", [])
    transcript = _messages_transcript(list(messages or []))
    if not transcript:
        raise exc

    prompt = (
        "子 Agent 在写出最终回答前达到了 LangGraph 递归/工具调用上限。\n"
        "不要调用工具。你现在没有任何工具。仅根据下方 transcript 写出尽可能好的最终回答。\n"
        "如果证据不完整，请明确说明。保持回答具体、简洁。\n\n"
        f"限制错误:\n{exc}\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )
    response = _direct_llm_model().invoke(
        [
            {
                "role": "system",
                "content": (
                    "你负责总结被中断的子 Agent 工作。不得请求工具，不得假设 transcript 中不存在的事实。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
    )
    return {
        "messages": [response],
        "_subagent_status": "partial",
        "_subagent_warning": (
            "子 Agent 达到 GRAPH_RECURSION_LIMIT；最终回答由已有消息无工具生成。"
        ),
        "_subagent_error": str(exc),
    }


def _subagent_system_prompt(cwd: str = "") -> str:
    """run_subagent 使用的默认 system prompt（中文）。"""
    cwd_note = f"\n工作目录提示: {cwd}" if cwd else ""
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


def _delegate_system_prompt(goal: str, context: str = "", cwd: str = "", mode: str = "readonly") -> str:
    """delegate_task 使用的英文 system prompt，包含结构化输出格式要求。"""
    cwd_note = cwd.strip() or "(not specified)"
    context_note = context.strip() or "(none)"
    mode_note = (
        "Readonly mode: do not modify files. You may inspect files, search, read web pages, and run safe diagnostic commands."
        if mode == "readonly"
        else "Workspace-write mode: you may edit existing project files when necessary, but keep changes minimal and task-scoped."
    )
    return (
        "You are a focused subagent working on one delegated task from a parent agent.\n\n"
        f"YOUR TASK:\n{goal.strip()}\n\n"
        f"CONTEXT:\n{context_note}\n\n"
        f"WORKING DIRECTORY HINT:\n{cwd_note}\n\n"
        f"MODE:\n{mode_note}\n\n"
        "Important rules:\n"
        "- You do not have the parent conversation history. Rely only on YOUR TASK, CONTEXT, and tool results.\n"
        "- Prefer search_files, glob, and read_file before broad shell commands.\n"
        "- Do not delegate to other agents.\n"
        "- If you make or claim any side-effect, include concrete verification such as file paths, command output, or errors.\n"
        "- If the user or parent context uses Chinese, return Chinese unless the task explicitly asks otherwise.\n\n"
        "Final response format:\n"
        "Summary:\n"
        "Findings:\n"
        "Files inspected:\n"
        "Files changed:\n"
        "Commands/tests run:\n"
        "Risks:\n"
        "Next steps:\n\n"
        "Available skills catalog:\n"
        f"{skill_catalog_for_prompt()}"
    )


def _normalize_delegate_mode(mode: str | None) -> str:
    """将 mode 参数标准化为合法值，非法值降级为 readonly。"""
    normalized = str(mode or "readonly").strip().lower()
    if normalized not in DELEGATE_MODES:
        return "readonly"
    return normalized


def _subagent_tools_for_mode(mode: str) -> list[Any]:
    """根据委派模式返回对应的工具集。"""
    if mode == "workspace_write":
        return WORKSPACE_WRITE_SUBAGENT_TOOLS
    return READONLY_SUBAGENT_TOOLS


def _normalize_delegate_tasks(
    goal: str = "",
    context: str = "",
    tasks: list[dict[str, Any]] | None = None,
    mode: str = "readonly",
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """将 goal/tasks 参数标准化为统一的任务列表，并校验合法性。

    返回 (normalized_tasks, error)——error 非 None 时表示校验失败。
    """
    if tasks:
        if not isinstance(tasks, list):
            return None, "tasks 必须是任务对象列表。"
        normalized_tasks: list[dict[str, Any]] = []
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                return None, f"第 {index} 个 task 必须是对象。"
            task_goal = str(task.get("goal") or "").strip()
            if not task_goal:
                return None, f"第 {index} 个 task 缺少非空的 goal。"
            normalized_tasks.append(
                {
                    "goal": task_goal,
                    "context": str(task.get("context") or context or ""),
                    "mode": _normalize_delegate_mode(str(task.get("mode") or mode)),
                }
            )
        return normalized_tasks, None

    task_goal = str(goal or "").strip()
    if not task_goal:
        return None, "请提供非空的 goal 或 tasks 数组。"
    return [{"goal": task_goal, "context": str(context or ""), "mode": _normalize_delegate_mode(mode)}], None


def _is_rate_limit_error(exc: BaseException) -> bool:
    """判断异常是否为速率限制错误（429/RateLimit/Throttling 等）。"""
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return any(marker in text for marker in ("ratelimit", "rate limit", "429", "throttling", "too many requests"))


def _subagent_retry_settings() -> tuple[int, float, float]:
    """读取速率限制重试配置：最大重试次数、初始延迟、最大延迟。"""
    retries = _int_env("AGENT_SUBAGENT_RATE_LIMIT_RETRIES", 3, minimum=0)
    initial_delay = _float_env("AGENT_SUBAGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS", 1.0)
    max_delay = _float_env("AGENT_SUBAGENT_RATE_LIMIT_MAX_DELAY_SECONDS", 8.0)
    return retries, initial_delay, max_delay


def _run_with_rate_limit_retry(callable_obj):
    """对 429 速率限制错误进行指数退避重试，非速率限制错误直接抛出。"""
    retries, initial_delay, max_delay = _subagent_retry_settings()
    attempt = 0
    while True:
        try:
            return callable_obj()
        except Exception as exc:
            if not _is_rate_limit_error(exc) or attempt >= retries:
                raise
            delay = min(max_delay, initial_delay * (2**attempt))
            time.sleep(delay)
            attempt += 1


def spawn_subagent(description: str, cwd: str = "", max_steps: int | None = None) -> str:
    """启动一个隔离的同步子 Agent，仅返回最终结论。

    ── 上下文隔离 ──
    子 Agent 在 ThreadPoolExecutor 工作线程中执行，
    LangGraph 的 runnable-context ContextVar (var_child_runnable_config)
    不会被继承到父 Agent。这防止了子 Agent 的工具事件（read_file、
    run_shell_command 等）泄漏到父 Agent 的 SSE stream。
    详细机制和调试开关见 _invoke_subagent_in_isolated_thread。
    """
    task_description = str(description or "").strip()
    if not task_description:
        return "错误：子 Agent 任务描述不能为空。"

    steps = max_steps if max_steps is not None else _int_env("AGENT_SUBAGENT_MAX_STEPS", 30)
    steps = max(1, min(int(steps), _int_env("AGENT_SUBAGENT_MAX_STEPS_LIMIT", 60)))
    recursion_limit = max(4, steps * 2 + 2)
    user_content = task_description
    if cwd:
        user_content = f"{task_description}\n\nUse cwd for file and shell tools when relevant: {cwd}"

    result = _invoke_subagent_in_isolated_thread(user_content, cwd, recursion_limit)
    return _extract_final_text(result)


def delegate_task(
    goal: str = "",
    context: str = "",
    tasks: list[dict[str, Any]] | None = None,
    cwd: str = "",
    mode: str = "readonly",
    max_steps: int | None = None,
) -> str:
    """将一个或多个聚焦的 leaf 子任务委派给隔离子 Agent，返回 JSON 结果。

    ── 上下文隔离 ──
    每个子 Agent 在独立的 ThreadPoolExecutor 线程中执行，
    LangGraph 的 runnable-context ContextVar 不会从父 Agent 传递过来。
    如果不做隔离，子 Agent 的工具事件会泄漏到父 Agent 的 SSE stream
    （前端会中途看到 read_file、run_shell_command 等内部过程）。
    详细机制和调试开关见 _invoke_subagent_in_isolated_thread。

    ── 禁止递归委派 ──
    子 Agent 只有 readonly 或 workspace_write 工具集——
    两者都不包含 delegate_task / run_subagent，
    所以子 Agent 不能再 spawn 孙 Agent。
    这与 hermes-agent 的 DELEGATE_BLOCKED_TOOLS 设计一致，
    防止无限嵌套。
    """
    normalized_tasks, error = _normalize_delegate_tasks(goal=goal, context=context, tasks=tasks, mode=mode)
    if error:
        return f"错误：{error}"
    assert normalized_tasks is not None

    max_workers = _int_env("AGENT_DELEGATE_MAX_WORKERS", 2)
    max_workers = max(1, min(max_workers, _int_env("AGENT_DELEGATE_MAX_WORKERS_LIMIT", 8)))
    max_tasks = _int_env("AGENT_DELEGATE_MAX_TASKS", 8)
    if len(normalized_tasks) > max_tasks:
        return f"错误：委派任务过多（{len(normalized_tasks)} 个），上限为 {max_tasks}。"

    steps = max_steps if max_steps is not None else _int_env("AGENT_SUBAGENT_MAX_STEPS", 30)
    steps = max(1, min(int(steps), _int_env("AGENT_SUBAGENT_MAX_STEPS_LIMIT", 60)))
    recursion_limit = max(4, steps * 2 + 2)

    started = time.monotonic()

    def run_one(index: int, task: dict[str, Any]) -> dict[str, Any]:
        """执行单个子任务，捕获异常后返回结构化错误结果。"""
        child_mode = _normalize_delegate_mode(task.get("mode"))
        system_prompt = _delegate_system_prompt(task["goal"], task.get("context", ""), cwd, child_mode)
        try:
            result = _invoke_subagent_in_isolated_thread(
                "Start the delegated task now.",
                cwd,
                recursion_limit,
                tools=_subagent_tools_for_mode(child_mode),
                system_prompt=system_prompt,
                tags=["subagent", "delegate_task"],
            )
            return {
                "task_index": index,
                "goal": task["goal"],
                "mode": child_mode,
                "status": result.get("_subagent_status", "ok") if isinstance(result, dict) else "ok",
                "summary": _extract_final_text(result),
                **(
                    {"warning": result["_subagent_warning"], "error": result.get("_subagent_error", "")}
                    if isinstance(result, dict) and result.get("_subagent_warning")
                    else {}
                ),
            }
        except Exception as exc:  # noqa: BLE001 - 将子 Agent 失败暴露给父 Agent 模型。
            return {
                "task_index": index,
                "goal": task["goal"],
                "mode": child_mode,
                "status": "error",
                "error": str(exc),
            }

    if len(normalized_tasks) == 1:
        # 单任务——直接在当前线程执行，无需线程池开销。
        results = [run_one(0, normalized_tasks[0])]
    else:
        # 多任务——ThreadPoolExecutor 并行执行，spawn_stagger 防止瞬间 API burst。
        results: list[dict[str, Any]] = []
        spawn_stagger = _float_env("AGENT_DELEGATE_SPAWN_STAGGER_SECONDS", 0.5)
        worker_count = min(max_workers, len(normalized_tasks))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="delegate-task") as executor:
            future_to_index = {}
            for index, task in enumerate(normalized_tasks):
                future_to_index[executor.submit(run_one, index, task)] = index
                if spawn_stagger and index < len(normalized_tasks) - 1:
                    time.sleep(spawn_stagger)
            for future in as_completed(future_to_index):
                results.append(future.result())
        results.sort(key=lambda item: int(item.get("task_index", 0)))

    payload = {
        "results": results,
        "total_duration_seconds": round(time.monotonic() - started, 2),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _invoke_subagent_in_isolated_thread(
    user_content: str,
    cwd: str,
    recursion_limit: int,
    *,
    tools: list[Any] | None = None,
    system_prompt: str | None = None,
    tags: list[str] | None = None,
) -> Any:
    # ── 设计决策：LangGraph runnable-context 上下文隔离 ──────────────────
    #
    # LangChain/LangGraph 把当前 run 的 config 存进 ContextVar
    # (var_child_runnable_config)。工具函数在主 Agent 的 runnable
    # context 里执行，所以如果在同一 context 中直接调用
    # graph.invoke()，子 graph 的 ensure_config() 会隐式继承父
    # config。父 config 里包含 CONFIG_KEY_RUNTIME / stream_writer /
    # CONFIG_KEY_STREAM，导致子 Agent 的工具事件（read_file、
    # run_shell_command 等）泄漏到父 Agent 的 SSE stream——
    # 前端会临时看到子 Agent 的内部过程。
    #
    # 解决方案：在普通 ThreadPoolExecutor 工作线程上执行 invoke()。
    # Python ContextVar 不自动继承到新线程，所以子 Agent 只能看到
    # 我们显式传入 graph.invoke() 的 config——没有继承的 stream，
    # 没有泄漏的事件。前端只展示 delegate_task / run_subagent
    # 这一个工具调用和最终结果。
    #
    # 调试开关：AGENT_SUBAGENT_STREAM_TO_PARENT=true 时切换到
    # copy_context().run(invoke)，显式把父 runnable context 带入
    # 工作线程。仅在调试时使用——它故意启用泄漏，以便在父 stream
    # 中观察子 Agent 内部行为。
    # ────────────────────────────────────────────────────────────────────
    def invoke() -> Any:
        middleware = [
            AgentContextReferenceMiddleware(),
            AgentContextCompactMiddleware(),
            AgentPermissionMiddleware(interactive=False),
        ]
        if is_agent_logging_enabled():
            middleware.append(AgentLoggingMiddleware(agent_name="subagent"))

        graph = create_agent(
            model=_llm_model(),
            tools=tools or SUBAGENT_TOOLS,
            middleware=middleware,
            system_prompt=system_prompt or _subagent_system_prompt(cwd),
        )
        input_state = {"messages": [{"role": "user", "content": user_content}]}
        config = {
            "recursion_limit": recursion_limit,
            "callbacks": [],
            "tags": tags or ["subagent"],
        }
        last_state: Any = None
        try:
            for state in graph.stream(input_state, config=config, stream_mode="values"):
                last_state = state
            if last_state is None:
                return graph.invoke(input_state, config=config)
            return last_state
        except GraphRecursionError as exc:
            if last_state is not None:
                return _fallback_summarize_recursion_limit(last_state, exc)
            raise

    stream_to_parent = _bool_env("AGENT_SUBAGENT_STREAM_TO_PARENT", False)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="subagent") as executor:
        if stream_to_parent:
            # 调试模式：copy_context() 把父 runnable context 显式带入子线程，
            # 子 Agent 工具事件会泄漏到父 SSE stream，方便调试观察。
            ctx = copy_context()
            return _run_with_rate_limit_retry(lambda: executor.submit(ctx.run, invoke).result())
        # 默认模式：新线程不继承 ContextVar，子 Agent 完全隔离。
        return _run_with_rate_limit_retry(lambda: executor.submit(invoke).result())
