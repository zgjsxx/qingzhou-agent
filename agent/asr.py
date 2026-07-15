"""Local speech recognition helpers for SenseVoice."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
ASR_OUTPUT_DIR = ROOT_DIR / ".agent_outputs" / "asr"
DEFAULT_MODEL = "iic/SenseVoiceSmall"
DEFAULT_LANGUAGE = "auto"
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60
_MODEL_CACHE: dict[tuple[str, str, bool], Any] = {}
_MODEL_LOCK = threading.Lock()


class AsrDependencyError(RuntimeError):
    """Raised when optional SenseVoice dependencies are missing."""


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _asr_max_age_seconds() -> int:
    try:
        configured = int(os.getenv("AGENT_ASR_OUTPUT_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS))
    except (TypeError, ValueError):
        configured = DEFAULT_MAX_AGE_SECONDS
    return max(configured, 0)


def cleanup_old_asr_files() -> None:
    max_age = _asr_max_age_seconds()
    if max_age <= 0 or not ASR_OUTPUT_DIR.exists():
        return

    cutoff = time.time() - max_age
    for file_path in ASR_OUTPUT_DIR.rglob("*"):
        try:
            if file_path.is_file() and file_path.stat().st_mtime < cutoff:
                file_path.unlink()
        except OSError:
            continue

    for dir_path in sorted(
        (item for item in ASR_OUTPUT_DIR.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            dir_path.rmdir()
        except OSError:
            continue


def _load_sensevoice_model(model_name: str, device: str, use_vad: bool):
    try:
        import torch  # noqa: F401
    except ImportError as exc:
        raise AsrDependencyError(
            "PyTorch is required for SenseVoice. Run: pip install -r requirements-asr.txt"
        ) from exc

    try:
        from funasr import AutoModel
    except ImportError as exc:
        raise AsrDependencyError(
            "SenseVoice dependencies are not installed. Run: pip install -r requirements-asr.txt"
        ) from exc

    key = (model_name, device, use_vad)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached

    kwargs: dict[str, Any] = {
        "model": model_name,
        "trust_remote_code": True,
        "device": device,
        "disable_update": True,
    }
    if use_vad:
        kwargs.update(
            {
                "vad_model": "fsmn-vad",
                "vad_kwargs": {"max_single_segment_time": 30000},
            }
        )

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        model = AutoModel(**kwargs)
        _MODEL_CACHE[key] = model
        return model


def _postprocess_text(text: str) -> str:
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
    except ImportError:
        return text.strip()
    return rich_transcription_postprocess(text).strip()


def _extract_text(result: Any) -> str:
    items = result if isinstance(result, list) else [result]
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sentence_info = item.get("sentence_info")
        if isinstance(sentence_info, list) and sentence_info:
            for sentence in sentence_info:
                if isinstance(sentence, dict) and sentence.get("text"):
                    parts.append(_postprocess_text(str(sentence["text"])))
            continue
        if item.get("text"):
            parts.append(_postprocess_text(str(item["text"])))
    return "\n".join(part for part in parts if part).strip()


def transcribe_audio(audio_path: str | Path, language: str = DEFAULT_LANGUAGE) -> dict[str, Any]:
    cleanup_old_asr_files()

    path = Path(audio_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"audio file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"audio path is not a file: {path}")

    model_name = os.getenv("SENSEVOICE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    device = os.getenv("SENSEVOICE_DEVICE", "cpu").strip() or "cpu"
    use_vad = _bool_env("SENSEVOICE_USE_VAD", True)
    selected_language = (language or os.getenv("SENSEVOICE_LANGUAGE", DEFAULT_LANGUAGE)).strip() or DEFAULT_LANGUAGE

    model = _load_sensevoice_model(model_name, device, use_vad)
    generate_kwargs: dict[str, Any] = {
        "input": str(path),
        "cache": {},
        "language": selected_language,
        "use_itn": _bool_env("SENSEVOICE_USE_ITN", True),
    }
    if use_vad:
        generate_kwargs.update({"batch_size_s": 60, "merge_vad": True, "merge_length_s": 15})
    else:
        generate_kwargs.update({"batch_size": 64})

    raw_result = model.generate(**generate_kwargs)
    text = _extract_text(raw_result)
    return {
        "text": text,
        "language": selected_language,
        "model": model_name,
        "device": device,
        "raw": raw_result,
    }


def warm_asr_model() -> dict[str, Any]:
    model_name = os.getenv("SENSEVOICE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    device = os.getenv("SENSEVOICE_DEVICE", "cpu").strip() or "cpu"
    use_vad = _bool_env("SENSEVOICE_USE_VAD", True)
    _load_sensevoice_model(model_name, device, use_vad)
    return {
        "model": model_name,
        "device": device,
        "use_vad": use_vad,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe audio with SenseVoice.")
    parser.add_argument("--audio", help="Path to audio file.")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE, help="Language code: auto, zh, en, yue, ja, ko, nospeech.")
    parser.add_argument("--warm", action="store_true", help="Load the configured SenseVoice model and exit.")
    args = parser.parse_args(argv)

    try:
        if args.warm:
            result = warm_asr_model()
        else:
            if not args.audio:
                parser.error("--audio is required unless --warm is set")
            result = transcribe_audio(args.audio, language=args.language)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI must return structured errors.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
