from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).parent.parent
SRC_DIR = BACKEND_DIR / "src"

sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(BACKEND_DIR.parent))

import playwright_tools
from agent.permissions import check_tool_permission


def _tool_names_with_env(enabled: bool) -> set[str]:
    env = os.environ.copy()
    env["AGENT_PLAYWRIGHT_ENABLED"] = "true" if enabled else "false"
    env["PYTHONPATH"] = f"{BACKEND_DIR.parent}{os.pathsep}{SRC_DIR}"
    script = (
        "from tools import ALL_TOOLS; "
        "print(','.join(sorted(getattr(tool, 'name', '') for tool in ALL_TOOLS)))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return set(completed.stdout.strip().split(","))


class PlaywrightToolsTests(unittest.IsolatedAsyncioTestCase):
    def test_playwright_tools_are_not_injected_by_default(self):
        self.assertNotIn("playwright_open", _tool_names_with_env(False))

    def test_playwright_tools_are_injected_when_enabled(self):
        names = _tool_names_with_env(True)
        self.assertTrue(
            {
                "playwright_open",
                "playwright_snapshot",
                "playwright_click",
                "playwright_type",
                "playwright_press",
                "playwright_scroll",
                "playwright_screenshot",
                "playwright_close",
            }.issubset(names)
        )

    async def test_open_rejects_non_http_urls(self):
        result = await playwright_tools.playwright_open.ainvoke(
            {"url": "file:///etc/passwd"}
        )
        self.assertTrue(result.startswith("Error:"))

    async def test_open_dispatches_valid_url(self):
        with patch.object(playwright_tools, "_dispatch", return_value="ok") as dispatch:
            result = await playwright_tools.playwright_open.ainvoke(
                {"url": "https://example.com"}
            )
        self.assertEqual("ok", result)
        dispatch.assert_awaited_once_with("open", url="https://example.com")

    def test_auto_detect_prefers_installed_edge(self):
        with patch.object(
            playwright_tools,
            "_browser_channel_available",
            side_effect=lambda channel: channel == "msedge",
        ):
            self.assertEqual(
                "msedge",
                playwright_tools._detect_installed_chromium_channel(),
            )

    async def test_screenshot_dispatches_path_handling_to_worker(self):
        with patch.object(playwright_tools, "_dispatch", return_value="queued") as dispatch:
            result = await playwright_tools.playwright_screenshot.ainvoke(
                {"path": "output/example.png", "full_page": True}
            )
        self.assertEqual("queued", result)
        dispatch.assert_awaited_once_with(
            "screenshot",
            path="output/example.png",
            full_page=True,
        )

    def test_output_path_rejects_path_outside_backend(self):
        with self.assertRaises(ValueError):
            playwright_tools._output_path("../outside.png")

    def test_interactive_browser_tools_require_approval(self):
        self.assertEqual(
            "ask",
            check_tool_permission(
                "playwright_open", {"url": "https://example.com"}
            ).behavior,
        )
        self.assertEqual(
            "ask",
            check_tool_permission(
                "playwright_type", {"selector": "#q", "text": "hello"}
            ).behavior,
        )
        self.assertEqual(
            "allow",
            check_tool_permission("playwright_snapshot", {}).behavior,
        )


if __name__ == "__main__":
    unittest.main()
