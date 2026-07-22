"""HTTP MCP client support for dynamically discovered tools."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from agent.config import CONFIG_FILE, load_agent_config
from agent.logging import log_event

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT_SECONDS = 15
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_DISALLOWED_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass(frozen=True)
class McpHttpServerConfig:
    name: str
    url: str
    headers: dict[str, str]
    protocol_version: str
    timeout_seconds: int


def normalize_mcp_name(name: str) -> str:
    """Normalize MCP server/tool names for LangChain tool names."""
    normalized = _DISALLOWED_NAME_CHARS.sub("_", str(name or "").strip())
    return normalized.strip("_") or "unnamed"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1) or m.group(2) or "", ""), value)
    if isinstance(value, dict):
        return {str(k): _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def _candidate_config_paths() -> list[Path]:
    configured = os.getenv("AGENT_MCP_CONFIG", "").strip()
    paths: list[Path] = []
    if configured:
        path = Path(configured).expanduser()
        paths.append(path if path.is_absolute() else PROJECT_ROOT / path)
    return paths


def _load_config_file() -> tuple[Path | None, dict[str, Any]]:
    """Load MCP config.

    The normal source is the ``mcp`` section in ``config/qingzhou-agent.json``. The
    ``AGENT_MCP_CONFIG`` environment variable remains as an explicit debug/test
    override and is not used as an automatic compatibility fallback.
    """
    for path in _candidate_config_paths():
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return path, _expand_env(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log_event("mcp.config_error", path=str(path), error=repr(exc))
            return path, {}
    raw = load_agent_config().get("mcp", {})
    if isinstance(raw, dict):
        return CONFIG_FILE, _expand_env(raw)
    return CONFIG_FILE, {}


def _server_configs(raw: dict[str, Any]) -> list[McpHttpServerConfig]:
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        return []

    configs: list[McpHttpServerConfig] = []
    default_timeout = _int_env("AGENT_MCP_HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    for name, item in servers.items():
        if not isinstance(item, dict):
            continue
        if item.get("enabled", True) is False:
            continue
        if str(item.get("type", "http")).lower() != "http":
            log_event("mcp.unsupported_transport", server=name, transport=item.get("type"))
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            log_event("mcp.config_error", server=name, error="missing url")
            continue
        headers = item.get("headers") if isinstance(item.get("headers"), dict) else {}
        try:
            timeout_seconds = int(item.get("timeout_seconds", default_timeout))
        except (TypeError, ValueError):
            timeout_seconds = default_timeout
        configs.append(
            McpHttpServerConfig(
                name=str(name),
                url=url,
                headers={str(k): str(v) for k, v in headers.items()},
                protocol_version=str(item.get("protocol_version") or DEFAULT_PROTOCOL_VERSION),
                timeout_seconds=max(timeout_seconds, 1),
            )
        )
    return configs


class McpHttpClient:
    """Small JSON-RPC client for HTTP MCP tools/list and tools/call."""

    def __init__(self, config: McpHttpServerConfig) -> None:
        self.config = config
        self._next_id = 1
        self._initialized = False

    def _rpc_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _post(self, payload: dict[str, Any], *, expect_response: bool = True) -> dict[str, Any] | None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.config.protocol_version,
            **self.config.headers,
        }
        request = urllib.request.Request(self.config.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                data = response.read()
                if not data:
                    return None
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"HTTP connection failed: {exc.reason}") from exc

        if "text/event-stream" in content_type.lower():
            return _parse_sse_json(data.decode("utf-8", errors="replace"), payload.get("id"))
        parsed = json.loads(data.decode("utf-8", errors="replace"))
        if isinstance(parsed, dict):
            return parsed
        if expect_response:
            raise RuntimeError("MCP response was not a JSON object.")
        return None

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._rpc_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        response = self._post(payload)
        if not response:
            raise RuntimeError(f"MCP method {method} returned no response.")
        if response.get("error"):
            raise RuntimeError(json.dumps(response["error"], ensure_ascii=False))
        return response.get("result")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload, expect_response=False)

    def initialize(self) -> None:
        if self._initialized:
            return
        result = self._request(
            "initialize",
            {
                "protocolVersion": self.config.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "xu-agent", "version": "1.2.0"},
            },
        )
        if isinstance(result, dict) and result.get("protocolVersion"):
            self._initialized = True
            try:
                self._notify("notifications/initialized")
            except RuntimeError as exc:
                log_event("mcp.initialized_notification_error", server=self.config.name, error=repr(exc))
            return
        self._initialized = True

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        self.initialize()
        log_event("mcp.tool_call", server=self.config.name, tool=tool_name, args=args)
        result = self._request("tools/call", {"name": tool_name, "arguments": args})
        return _format_tool_result(result)


def _parse_sse_json(text: str, expected_id: Any) -> dict[str, Any] | None:
    for event in text.split("\n\n"):
        data_lines = []
        for line in event.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            continue
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            continue
        if expected_id is None or parsed.get("id") == expected_id:
            return parsed
    return None


def _format_tool_result(result: Any) -> str:
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)
    if result.get("isError"):
        prefix = "MCP tool error:\n"
    else:
        prefix = ""
    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return prefix + "\n".join(parts)
    return prefix + json.dumps(result, ensure_ascii=False, indent=2)


def _tool_schema(tool_def: dict[str, Any]) -> dict[str, Any]:
    schema = tool_def.get("inputSchema") or tool_def.get("input_schema")
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    return schema


def _tool_description(server_name: str, original_name: str, tool_def: dict[str, Any]) -> str:
    description = str(tool_def.get("description") or "").strip()
    prefix = f"MCP server '{server_name}', tool '{original_name}'."
    return f"{prefix} {description}".strip()


def _make_mcp_tool(client: McpHttpClient, server_name: str, tool_def: dict[str, Any]) -> StructuredTool | None:
    original_name = str(tool_def.get("name") or "").strip()
    if not original_name:
        return None
    safe_server = normalize_mcp_name(server_name)
    safe_tool = normalize_mcp_name(original_name)
    tool_name = f"mcp__{safe_server}__{safe_tool}"

    def call_mcp_tool(**kwargs: Any) -> str:
        return client.call_tool(original_name, kwargs)

    return StructuredTool.from_function(
        func=call_mcp_tool,
        name=tool_name,
        description=_tool_description(server_name, original_name, tool_def),
        args_schema=_tool_schema(tool_def),
        infer_schema=False,
    )


def load_mcp_tools() -> list[StructuredTool]:
    """Load configured HTTP MCP servers and return LangChain tools."""
    config_path, raw = _load_config_file()
    if not raw:
        return []

    tools: list[StructuredTool] = []
    for config in _server_configs(raw):
        client = McpHttpClient(config)
        try:
            tool_defs = client.list_tools()
        except Exception as exc:
            log_event("mcp.connect_error", config_path=str(config_path), server=config.name, error=repr(exc))
            continue
        for tool_def in tool_defs:
            tool = _make_mcp_tool(client, config.name, tool_def)
            if tool is not None:
                tools.append(tool)
        log_event(
            "mcp.connected",
            config_path=str(config_path),
            server=config.name,
            tool_count=len(tool_defs),
            tool_names=[tool.get("name") for tool in tool_defs],
        )
    return tools
