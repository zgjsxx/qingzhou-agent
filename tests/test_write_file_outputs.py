import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import registry


class WriteFileOutputsTest(unittest.TestCase):
    def test_write_file_uses_thread_scoped_output_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "files"
            token = registry.set_current_tool_thread_id("thread:abc/123")
            try:
                with patch("tools.registry.WRITE_FILES_DIR", output_root):
                    result = registry._write_file_impl("scripts/demo.py", "print('ok')", cwd=str(Path(tmpdir) / "cwd"))
            finally:
                registry.reset_current_tool_thread_id(token)

            expected = output_root / "thread_abc_123" / "scripts" / "demo.py"
            self.assertTrue(expected.exists())
            self.assertEqual(expected.read_text(encoding="utf-8"), "print('ok')")
            self.assertIn("scripts/demo.py", result)
            self.assertIn(str(expected.resolve()), result)
            self.assertFalse((Path(tmpdir) / "cwd" / "scripts" / "demo.py").exists())

    def test_write_file_rejects_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("tools.registry.WRITE_FILES_DIR", Path(tmpdir)):
                result = registry._write_file_impl(str(Path(tmpdir) / "demo.py"), "print('bad')")

        self.assertIn("only accepts relative paths", result)

    def test_write_file_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("tools.registry.WRITE_FILES_DIR", Path(tmpdir)):
                result = registry._write_file_impl("../demo.py", "print('bad')")

        self.assertIn("escapes the agent output directory", result)

    def test_run_shell_command_empty_cwd_uses_thread_scoped_shell_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shell_root = Path(tmpdir) / "shell"
            token = registry.set_current_tool_thread_id("thread:shell/123")
            try:
                with patch("tools.registry.SHELL_CWD_DIR", shell_root):
                    result = registry.run_shell_command.invoke(
                        {
                            "command": "Write-Output hello",
                            "shell": "powershell",
                            "timeout_seconds": 5,
                        }
                    )
            finally:
                registry.reset_current_tool_thread_id(token)

            expected_cwd = (shell_root / "thread_shell_123").resolve()
            self.assertTrue(expected_cwd.exists())
            self.assertIn(f"cwd: {expected_cwd}", result)
            self.assertIn("hello", result)

    def test_run_shell_command_explicit_cwd_is_respected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            explicit_cwd = Path(tmpdir).resolve()
            result = registry.run_shell_command.invoke(
                {
                    "command": "Write-Output hello",
                    "cwd": str(explicit_cwd),
                    "shell": "powershell",
                    "timeout_seconds": 5,
                }
            )

        self.assertIn(f"cwd: {explicit_cwd}", result)
        self.assertIn("hello", result)


if __name__ == "__main__":
    unittest.main()
