import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent import tts


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".agent_outputs" / "test-temp"


class WorkspaceTempDir:
    def __enter__(self):
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = TEST_TMP_ROOT / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        return str(self.path)

    def __exit__(self, *_args):
        shutil.rmtree(self.path, ignore_errors=True)


def temporary_workspace_dir():
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return WorkspaceTempDir()


class TtsTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_TMP_ROOT, ignore_errors=True)

    def test_tts_enabled_reads_environment_flag(self):
        with patch.dict("os.environ", {"AGENT_TTS_ENABLED": "true"}):
            self.assertTrue(tts.tts_enabled())

        with patch.dict("os.environ", {"AGENT_TTS_ENABLED": "false"}):
            self.assertFalse(tts.tts_enabled())

    def test_synthesize_speech_writes_default_wav(self):
        with temporary_workspace_dir() as tmpdir:
            root = Path(tmpdir)

            def fake_synthesize(_text, output_path, voice=""):
                output_path.write_bytes(b"RIFFtest")
                return {"voice": voice, "rate": 0, "volume": 100}

            with (
                patch("agent.tts.TTS_OUTPUT_DIR", root),
                patch("agent.tts._synthesize_with_system_speech", side_effect=fake_synthesize) as synthesize,
            ):
                result = tts.synthesize_speech("hello")

            synthesize.assert_called_once()
            self.assertEqual(result["format"], "wav")
            self.assertEqual(result["provider"], tts.DEFAULT_PROVIDER)
            self.assertTrue(Path(result["path"]).exists())
            self.assertGreater(result["size"], 0)

    def test_synthesize_speech_rejects_empty_text(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            tts.synthesize_speech("  ")

    def test_synthesize_speech_rejects_unsupported_provider(self):
        with self.assertRaisesRegex(ValueError, "unsupported TTS provider"):
            tts.synthesize_speech("hello", provider="missing")

    def test_load_engine_reports_missing_pyttsx3(self):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "pyttsx3":
                raise ImportError("missing pyttsx3")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(tts.TtsDependencyError, "pyttsx3 is required"):
                tts._load_pyttsx3_engine()

    def test_warm_tts_engine_loads_provider_and_counts_voices(self):
        engine = MagicMock()
        engine.getProperty.side_effect = lambda key: {
            "voices": [MagicMock(id="voice-1", name="Voice One")],
            "rate": 180,
            "volume": 0.8,
        }[key]

        with (
            patch.dict(os.environ, {"AGENT_TTS_PROVIDER": "pyttsx3"}, clear=False),
            patch("agent.tts._load_pyttsx3_engine", return_value=engine),
        ):
            result = tts.warm_tts_engine()

        self.assertEqual(result["provider"], "pyttsx3")
        self.assertEqual(result["voice_count"], 1)
        self.assertEqual(result["rate"], 180)

    def test_warm_system_speech_counts_voices(self):
        with patch(
            "agent.tts._run_system_speech_script",
            return_value={
                "voice": "Microsoft Zira Desktop",
                "rate": 0,
                "volume": 100,
                "available_voices": ["Microsoft Zira Desktop"],
            },
        ):
            result = tts.warm_tts_engine()

        self.assertEqual(result["provider"], tts.DEFAULT_PROVIDER)
        self.assertEqual(result["voice_count"], 1)

    def test_run_system_speech_script_parses_json(self):
        process = MagicMock()
        process.returncode = 0
        process.stdout = json.dumps({"voice": "v"})
        process.stderr = ""

        with patch("agent.tts.subprocess.run", return_value=process):
            result = tts._run_system_speech_script("script")

        self.assertEqual(result, {"voice": "v"})

    def test_cleanup_old_tts_files_removes_expired_files(self):
        with temporary_workspace_dir() as tmpdir:
            root = Path(tmpdir)
            old_file = root / "old" / "reply.wav"
            old_file.parent.mkdir(parents=True)
            old_file.write_bytes(b"old")
            os.utime(old_file, (1, 1))

            with (
                patch("agent.tts.TTS_OUTPUT_DIR", root),
                patch("agent.tts._tts_max_age_seconds", return_value=1),
                patch("agent.tts.time.time", return_value=10),
            ):
                tts.cleanup_old_tts_files()

            self.assertFalse(old_file.exists())
            self.assertFalse((root / "old").exists())


if __name__ == "__main__":
    unittest.main()
