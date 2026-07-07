import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools


class BackgroundTaskTest(unittest.TestCase):
    def test_cancel_background_task_marks_orphaned_running_task_cancelled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            task_id = "bg_test_cancel"
            meta = {
                "id": task_id,
                "type": "shell",
                "status": "running",
                "command": "long running command",
                "cwd": tmpdir,
                "shell": "cmd",
                "timeout_seconds": 60,
                "started_at": 1.0,
                "finished_at": None,
                "pid": 0,
                "exit_code": None,
                "timed_out": False,
                "log_path": str(Path(tmpdir) / f"{task_id}.log"),
                "error": "",
            }
            background_dir = Path(tmpdir)
            (background_dir / f"{task_id}.json").write_text(
                json.dumps(meta),
                encoding="utf-8",
            )

            cancel_func = getattr(tools.cancel_background_task, "func", tools.cancel_background_task)
            with patch("tools.registry.BACKGROUND_DIR", background_dir):
                result = cancel_func(task_id)
                updated = json.loads((background_dir / f"{task_id}.json").read_text(encoding="utf-8"))

        self.assertIn("Cancel requested", result)
        self.assertEqual(updated["status"], "cancelled")
        self.assertFalse(updated["timed_out"])
        self.assertIsNone(updated["exit_code"])
        self.assertIsNotNone(updated["finished_at"])

    def test_cancel_background_task_is_registered(self):
        names = {getattr(tool, "name", "") for tool in tools.ALL_TOOLS}
        self.assertIn("cancel_background_task", names)


if __name__ == "__main__":
    unittest.main()
