import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent import kanban
from tools import registry


class KanbanLiteTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.env = patch.dict(
            os.environ,
            {"AGENT_KANBAN_DB": str(Path(self.tmpdir.name) / "kanban.db")},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_create_parent_dependency_and_promote(self):
        with kanban.connect_closing() as conn:
            parent = kanban.create_task(conn, title="Parent")
            child = kanban.create_task(conn, title="Child", parents=[parent.id])

            self.assertEqual(parent.status, "ready")
            self.assertEqual(child.status, "todo")

            self.assertTrue(kanban.complete_task(conn, parent.id, summary="Parent done"))
            child = kanban.get_task(conn, child.id)

            self.assertEqual(child.status, "ready")

    def test_claim_is_atomic(self):
        with kanban.connect_closing() as conn:
            task = kanban.create_task(conn, title="Claim me")

            first = kanban.claim_task(conn, task.id, owner="worker-a")
            second = kanban.claim_task(conn, task.id, owner="worker-b")

            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(kanban.get_task(conn, task.id).status, "running")

    def test_comment_and_worker_context_include_handoff(self):
        with kanban.connect_closing() as conn:
            parent = kanban.create_task(conn, title="Research")
            child = kanban.create_task(conn, title="Implement", parents=[parent.id])
            kanban.complete_task(conn, parent.id, summary="Use approach A")
            kanban.add_comment(conn, child.id, body="Watch the API shape", author="tester")

            text = kanban.build_worker_context(conn, child.id)

            self.assertIn("Use approach A", text)
            self.assertIn("Watch the API shape", text)

    def test_dispatch_uses_delegate_task_and_completes_card(self):
        with kanban.connect_closing() as conn:
            task = kanban.create_task(conn, title="Investigate")

            with patch(
                "agent.subagent.delegate_task",
                return_value=json.dumps(
                    {
                        "results": [
                            {
                                "status": "ok",
                                "summary": "Investigation complete",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            ) as delegated:
                result = kanban.dispatch_once(conn, max_tasks=1)

            self.assertEqual(result["dispatched"][0]["task_id"], task.id)
            self.assertEqual(kanban.get_task(conn, task.id).status, "done")
            self.assertTrue(delegated.called)

    def test_tools_are_registered(self):
        names = {tool.name for tool in registry.ALL_TOOLS}

        self.assertIn("kanban_create", names)
        self.assertIn("kanban_dispatch", names)


if __name__ == "__main__":
    unittest.main()
