"""Permission checks for agent tool calls."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langgraph.types import interrupt

from agent_logging import log_event
from tools import reset_current_tool_thread_id, set_current_tool_thread_id

PermissionBehavior = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class PermissionDecision:
    behavior: PermissionBehavior
    reason: str = ""


SHELL_DENY_PATTERNS = [
    (r"\brm\s+-rf\s+/", "Refusing to remove the filesystem root."),
    (r"(^|[;&|]\s*)sudo\b", "Refusing privileged sudo execution."),
    (r"(^|[;&|]\s*)shutdown\b", "Refusing system shutdown."),
    (r"(^|[;&|]\s*)reboot\b", "Refusing system reboot."),
    (r"(^|[;&|]\s*)mkfs(?:\.\w+)?\b", "Refusing filesystem formatting."),
    (r"(^|[;&|]\s*)diskpart\b", "Refusing disk partitioning."),
    (r"(^|[;&|]\s*|cmd(?:\.exe)?\s+/[ck]\s+)format(?:\.com|\.exe)?(?=$|\s)", "Refusing disk formatting."),
    (r"(^|[;&|]\s*)dd\b.*\bif=", "Refusing raw disk writes."),
    (r">\s*/dev/sda\b", "Refusing writes to a raw disk device."),
]

SHELL_ASK_PATTERNS = [
    (r"\brm\b", "Potentially destructive file removal command."),
    (r"\bdel\b", "Potentially destructive file deletion command."),
    (r"\brd\b|\brmdir\b", "Potentially destructive directory removal command."),
    (r"\bremove-item\b", "Potentially destructive PowerShell removal command."),
    (r"\b(?:pip|pip3)\s+install\b", "Installing Python packages requires approval."),
    (r"\b(?:python|python3|py)\b\s+-m\s+pip\s+install\b", "Installing Python packages requires approval."),
    (r"\buv\s+pip\s+install\b", "Installing Python packages requires approval."),
    (r"\bchmod\s+777\b", "Potentially unsafe permission change."),
    (r"\bchown\b", "Potentially unsafe ownership change."),
    (r">\s*/etc/", "Potentially unsafe write to system configuration."),
]

WRITE_TOOLS = {"write_file", "edit_file"}
APPROVED_TOOL_CALLS: dict[str, set[str]] = {}
APPROVED_TOOL_CALLS_LOCK = threading.Lock()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_tool_call(tool_call: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return "", {}

    name = str(tool_call.get("name") or "")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        args = tool_call.get("input")
    if not isinstance(args, dict):
        args = {}

    return name, args


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _extract_configurable(config: Any) -> Any:
    return _mapping_get(config, "configurable") or {}


def _extract_thread_id_from_config(config: Any) -> str | None:
    configurable = _extract_configurable(config)
    for key in ("thread_id", "threadId", "session_id", "sessionId"):
        value = _mapping_get(configurable, key) or _mapping_get(config, key)
        if value:
            return str(value)
    return None


def _extract_thread_id(request: Any) -> str | None:
    for source in (
        request,
        getattr(request, "runtime", None),
        getattr(request, "config", None),
        getattr(getattr(request, "runtime", None), "config", None),
        getattr(getattr(request, "runtime", None), "context", None),
    ):
        if source is None:
            continue
        value = _mapping_get(source, "thread_id") or _mapping_get(source, "threadId")
        if value:
            return str(value)
        value = _extract_thread_id_from_config(source)
        if value:
            return value
    return None


def _normalize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _normalize_json_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(v) for v in value]
    return repr(value)


def _canonical_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    if tool_name == "run_shell_command":
        normalized["command"] = str(normalized.get("command", "")).strip()
        normalized["cwd"] = str(normalized.get("cwd", ""))
        normalized["shell"] = str(normalized.get("shell", "auto"))
        normalized["timeout_seconds"] = normalized.get("timeout_seconds", 30)
    return _normalize_json_value(normalized)


def _tool_call_signature(tool_name: str, args: dict[str, Any]) -> str:
    return json.dumps(
        {
            "tool": tool_name,
            "args": _canonical_tool_args(tool_name, args),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _resolve_root(cwd: str) -> Path:
    return Path(cwd or Path.cwd()).expanduser().resolve()


def _path_escapes_root(path: str, cwd: str = "") -> bool:
    if not path:
        return False

    root = _resolve_root(cwd)
    requested = Path(path).expanduser()
    resolved = (
        (root / requested).resolve()
        if not requested.is_absolute()
        else requested.resolve()
    )
    return not resolved.is_relative_to(root)


def _check_shell_deny_list(command: str) -> PermissionDecision:
    normalized = command.strip().lower()
    for pattern, reason in SHELL_DENY_PATTERNS:
        if re.search(pattern, normalized):
            return PermissionDecision("deny", reason)

    return PermissionDecision("allow")


def _check_permission_rules(tool_name: str, args: dict[str, Any]) -> PermissionDecision:
    if tool_name in WRITE_TOOLS and _path_escapes_root(
        str(args.get("path", "")),
        str(args.get("cwd", "")),
    ):
        return PermissionDecision("ask", "Writing outside the working directory requires approval.")

    if tool_name == "run_shell_command":
        command = str(args.get("command", ""))
        normalized = command.strip().lower()
        for pattern, reason in SHELL_ASK_PATTERNS:
            if re.search(pattern, normalized):
                return PermissionDecision("ask", reason)

    return PermissionDecision("allow")


def check_tool_permission(tool_name: str, args: dict[str, Any]) -> PermissionDecision:
    """Run the s03 permission pipeline for a tool call."""
    if tool_name == "run_shell_command":
        deny_decision = _check_shell_deny_list(str(args.get("command", "")))
        if deny_decision.behavior == "deny":
            return deny_decision

    return _check_permission_rules(tool_name, args)


def _permission_denied_message(tool_name: str, decision: PermissionDecision) -> str:
    if decision.behavior == "ask":
        return (
            "Permission denied: this operation requires user approval before execution. "
            f"Tool: {tool_name}. Reason: {decision.reason}"
        )

    return f"Permission denied: {decision.reason}"


def _user_rejected_message(tool_name: str, args: dict[str, Any], reason: str) -> str:
    return (
        "User rejected this tool call. Do not retry the same tool call unless the user "
        "explicitly changes their instruction.\n"
        f"Tool: {tool_name}\n"
        f"Args: {json.dumps(_normalize_json_value(args), ensure_ascii=False, sort_keys=True)}\n"
        f"User rejection reason: {reason}"
    )


def _approval_request(tool_name: str, args: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "action_requests": [
            {
                "name": tool_name,
                "args": args,
                "description": reason,
            }
        ],
        "review_configs": [
            {
                "action_name": tool_name,
                "allowed_decisions": ["approve", "reject"],
            }
        ],
    }


def _first_decision(resume_value: Any) -> dict[str, Any]:
    if isinstance(resume_value, dict):
        decisions = resume_value.get("decisions")
        if isinstance(decisions, list) and decisions:
            decision = decisions[0]
            return decision if isinstance(decision, dict) else {}
        return resume_value
    if isinstance(resume_value, list) and resume_value:
        decision = resume_value[0]
        return decision if isinstance(decision, dict) else {}
    return {}


def _is_approved(thread_id: str | None, signature: str) -> bool:
    if not thread_id:
        return False
    with APPROVED_TOOL_CALLS_LOCK:
        return signature in APPROVED_TOOL_CALLS.get(thread_id, set())


def _remember_approval(thread_id: str | None, signature: str) -> None:
    if not thread_id:
        return
    with APPROVED_TOOL_CALLS_LOCK:
        APPROVED_TOOL_CALLS.setdefault(thread_id, set()).add(signature)


class AgentPermissionMiddleware(AgentMiddleware):
    """Block or gate tool calls before they execute."""

    def _check_request(self, request: Any) -> str | None:
        tool_name, args = _normalize_tool_call(getattr(request, "tool_call", None))
        decision = check_tool_permission(tool_name, args)
        if decision.behavior == "allow":
            return None

        log_event(
            "permission.check",
            tool=tool_name,
            behavior=decision.behavior,
            reason=decision.reason,
            tool_args=args,
        )

        if decision.behavior == "ask" and _bool_env("AGENT_PERMISSION_ALLOW_ASK_RULES", False):
            return None

        if decision.behavior == "ask":
            signature = _tool_call_signature(tool_name, args)
            thread_id = _extract_thread_id(request)
            if _is_approved(thread_id, signature):
                log_event(
                    "permission.cached_allow",
                    tool=tool_name,
                    thread_id=thread_id,
                    tool_args=args,
                )
                return None

            resume_value = interrupt(_approval_request(tool_name, args, decision.reason))
            human_decision = _first_decision(resume_value)
            if human_decision.get("type") == "approve":
                _remember_approval(thread_id, signature)
                log_event(
                    "permission.approved",
                    tool=tool_name,
                    thread_id=thread_id,
                    tool_args=args,
                )
                return None

            reason = str(human_decision.get("message") or decision.reason)
            log_event(
                "permission.rejected",
                tool=tool_name,
                thread_id=thread_id,
                reason=reason,
                tool_args=args,
            )
            return _user_rejected_message(tool_name, args, reason)

        log_event(
            "permission.denied",
            tool=tool_name,
            behavior=decision.behavior,
            reason=decision.reason,
            tool_args=args,
        )
        return _permission_denied_message(tool_name, decision)

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        denied_message = self._check_request(request)
        if denied_message is not None:
            return denied_message

        token = set_current_tool_thread_id(_extract_thread_id(request))
        try:
            return handler(request)
        finally:
            reset_current_tool_thread_id(token)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        denied_message = self._check_request(request)
        if denied_message is not None:
            return denied_message

        token = set_current_tool_thread_id(_extract_thread_id(request))
        try:
            return await handler(request)
        finally:
            reset_current_tool_thread_id(token)
