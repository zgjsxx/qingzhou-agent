import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agent.cron as agent_cron


class AgentCronTest(unittest.TestCase):
    def setUp(self):
        agent_cron._jobs.clear()
        agent_cron._pending.clear()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _env(self):
        return patch.dict(
            "os.environ",
            {
                "AGENT_CRON_STORAGE_PATH": str(Path(self.tmpdir.name) / "scheduled_tasks.json"),
                "AGENT_CRON_MAX_JOBS": "50",
            },
        )

    def test_cron_matches_basic_expressions(self):
        dt = datetime(2026, 6, 24, 9, 30)

        self.assertTrue(agent_cron.cron_matches("30 9 * * *", dt))
        self.assertTrue(agent_cron.cron_matches("*/15 9 * * *", dt))
        self.assertFalse(agent_cron.cron_matches("31 9 * * *", dt))

    def test_schedule_list_and_cancel_job(self):
        with self._env():
            result = agent_cron.schedule_job(
                thread_id="thread_a",
                cron="*/5 * * * *",
                prompt="say ok",
                recurring=True,
                durable=True,
            )

            self.assertIsInstance(result, agent_cron.CronJob)
            self.assertEqual(agent_cron.list_jobs("thread_a"), [result])
            self.assertIn("Cancelled", agent_cron.cancel_job(result.id))
            self.assertEqual(agent_cron.list_jobs("thread_a"), [])

    def test_due_job_is_enqueued_once_per_minute(self):
        with self._env():
            result = agent_cron.schedule_job(
                thread_id="thread_a",
                cron="30 9 * * *",
                prompt="say ok",
                recurring=True,
                durable=True,
            )
            self.assertIsInstance(result, agent_cron.CronJob)

            now = datetime(2026, 6, 24, 9, 30)
            agent_cron._enqueue_due_jobs(now)
            agent_cron._enqueue_due_jobs(now)

            self.assertEqual(list(agent_cron._pending), [result.id])
            self.assertEqual(agent_cron._jobs[result.id].last_fired_at, "2026-06-24 09:30")


if __name__ == "__main__":
    unittest.main()
