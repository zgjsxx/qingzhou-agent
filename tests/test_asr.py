import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import asr


class AsrTest(unittest.TestCase):
    def test_extract_text_from_sensevoice_result(self):
        result = [
            {"text": "<|zh|><|NEUTRAL|><|Speech|><|woitn|>你好，青舟。"},
            {"text": "第二句。"},
        ]

        with patch("agent.asr._postprocess_text", side_effect=lambda text: text.replace("<|zh|><|NEUTRAL|><|Speech|><|woitn|>", "")):
            text = asr._extract_text(result)

        self.assertEqual(text, "你好，青舟。\n第二句。")

    def test_transcribe_audio_requires_existing_file(self):
        result_path = Path(tempfile.gettempdir()) / "qingzhou-missing-audio.wav"

        with self.assertRaises(FileNotFoundError):
            asr.transcribe_audio(result_path)

    def test_warm_asr_model_loads_configured_model(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "SENSEVOICE_MODEL": "test/model",
                    "SENSEVOICE_DEVICE": "cpu",
                    "SENSEVOICE_USE_VAD": "false",
                },
            ),
            patch("agent.asr._load_sensevoice_model") as load_model,
        ):
            result = asr.warm_asr_model()

        load_model.assert_called_once_with("test/model", "cpu", False)
        self.assertEqual(result["model"], "test/model")
        self.assertEqual(result["device"], "cpu")
        self.assertFalse(result["use_vad"])

    def test_load_model_reports_missing_torch(self):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "torch":
                raise ImportError("missing torch")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(asr.AsrDependencyError, "PyTorch is required"):
                asr._load_sensevoice_model("test/model", "cpu", False)

    def test_cleanup_old_asr_files_removes_expired_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_file = root / "old" / "audio.webm"
            old_file.parent.mkdir(parents=True)
            old_file.write_text("old", encoding="utf-8")
            old_mtime = 1
            old_file.touch()
            import os

            os.utime(old_file, (old_mtime, old_mtime))

            with (
                patch("agent.asr.ASR_OUTPUT_DIR", root),
                patch("agent.asr._asr_max_age_seconds", return_value=1),
                patch("agent.asr.time.time", return_value=10),
            ):
                asr.cleanup_old_asr_files()

            self.assertFalse(old_file.exists())
            self.assertFalse((root / "old").exists())


if __name__ == "__main__":
    unittest.main()
