import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import asr_server


class AsrServerTest(unittest.TestCase):
    def setUp(self):
        with asr_server._WARM_LOCK:
            asr_server._WARM_THREAD = None
            asr_server._WARM_STATE.clear()
            asr_server._WARM_STATE.update({"ready": False, "status": "cold"})

    def tearDown(self):
        thread = asr_server._WARM_THREAD
        if thread is not None:
            thread.join(timeout=1)

    def test_health_responds_while_model_is_loading(self):
        started = threading.Event()
        release = threading.Event()

        def warm_model():
            started.set()
            release.wait(timeout=5)
            return {"model": "test/model", "device": "cpu", "use_vad": False}

        with patch("agent.asr_server.warm_asr_model", side_effect=warm_model):
            with TestClient(asr_server.app) as client:
                self.assertTrue(started.wait(timeout=1))

                response = client.get("/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json(),
                    {
                        "ok": True,
                        "ready": False,
                        "status": "loading",
                        "error": None,
                        "error_type": None,
                    },
                )

                release.set()


if __name__ == "__main__":
    unittest.main()
