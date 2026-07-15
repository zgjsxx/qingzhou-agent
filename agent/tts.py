"""Local text-to-speech helpers."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import platform
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
TTS_OUTPUT_DIR = ROOT_DIR / ".agent_outputs" / "tts"
DEFAULT_PROVIDER = "edge_tts"
FALLBACK_PROVIDER = "system_speech" if platform.system() == "Windows" else "pyttsx3"
DEFAULT_FORMAT = "wav"
EDGE_TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
EDGE_TTS_DEFAULT_OPUS_FORMAT = "webm-24khz-16bit-24kbps-mono-opus"
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


class TtsDependencyError(RuntimeError):
    """Raised when optional TTS dependencies are missing."""


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def tts_enabled() -> bool:
    return _bool_env("AGENT_TTS_ENABLED", False)


def _tts_max_age_seconds() -> int:
    try:
        configured = int(os.getenv("AGENT_TTS_OUTPUT_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS))
    except (TypeError, ValueError):
        configured = DEFAULT_MAX_AGE_SECONDS
    return max(configured, 0)


def cleanup_old_tts_files() -> None:
    max_age = _tts_max_age_seconds()
    if max_age <= 0 or not TTS_OUTPUT_DIR.exists():
        return

    cutoff = time.time() - max_age
    for file_path in TTS_OUTPUT_DIR.rglob("*"):
        try:
            if file_path.is_file() and file_path.stat().st_mtime < cutoff:
                file_path.unlink()
        except OSError:
            continue

    for dir_path in sorted(
        (item for item in TTS_OUTPUT_DIR.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            dir_path.rmdir()
        except OSError:
            continue


def _load_pyttsx3_engine():
    try:
        import pyttsx3
    except ImportError as exc:
        raise TtsDependencyError(
            "pyttsx3 is required for local TTS fallback. Run: .\\build.ps1 -WithAsr"
        ) from exc
    return pyttsx3.init()


def _int_env(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_env(name: str) -> float | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _select_voice(engine: Any, voice: str) -> str:
    selected = (voice or os.getenv("AGENT_TTS_VOICE", "")).strip()
    if not selected:
        return ""

    voices = engine.getProperty("voices") or []
    selected_lower = selected.lower()
    for item in voices:
        voice_id = str(getattr(item, "id", "") or "")
        voice_name = str(getattr(item, "name", "") or "")
        if voice_id == selected or voice_name == selected:
            return voice_id
        if selected_lower in voice_id.lower() or selected_lower in voice_name.lower():
            return voice_id
    raise ValueError(f"TTS voice not found: {selected}")


def _configure_pyttsx3_engine(engine: Any, voice: str = "") -> dict[str, Any]:
    selected_voice = _select_voice(engine, voice)
    if selected_voice:
        engine.setProperty("voice", selected_voice)

    rate = _int_env("AGENT_TTS_RATE")
    if rate is not None:
        engine.setProperty("rate", max(80, min(rate, 320)))

    volume = _float_env("AGENT_TTS_VOLUME")
    if volume is not None:
        engine.setProperty("volume", max(0.0, min(volume, 1.0)))

    return {
        "voice": selected_voice,
        "rate": engine.getProperty("rate"),
        "volume": engine.getProperty("volume"),
    }


def _synthesize_with_pyttsx3(text: str, output_path: Path, voice: str = "") -> dict[str, Any]:
    engine = _load_pyttsx3_engine()
    config = _configure_pyttsx3_engine(engine, voice=voice)
    engine.save_to_file(text, str(output_path))
    engine.runAndWait()
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"TTS synthesis did not create audio file: {output_path}")
    return config


def _edge_tts_voice(voice: str = "") -> str:
    return (voice or os.getenv("AGENT_TTS_VOICE", "") or EDGE_TTS_DEFAULT_VOICE).strip()


def _edge_tts_rate() -> str:
    return os.getenv("AGENT_TTS_RATE", "+0%").strip() or "+0%"


def _edge_tts_volume() -> str:
    return os.getenv("AGENT_TTS_VOLUME", "+0%").strip() or "+0%"


def _edge_tts_pitch() -> str:
    return os.getenv("AGENT_TTS_PITCH", "+0Hz").strip() or "+0Hz"


def _edge_tts_output_format(audio_format: str) -> str:
    if audio_format in {"opus", "webm", "webm_opus"}:
        return os.getenv("AGENT_EDGE_TTS_OPUS_FORMAT", EDGE_TTS_DEFAULT_OPUS_FORMAT).strip() or EDGE_TTS_DEFAULT_OPUS_FORMAT
    return os.getenv("AGENT_EDGE_TTS_OUTPUT_FORMAT", "").strip()


async def _edge_tts_save_async(text: str, output_path: Path, voice: str, audio_format: str) -> dict[str, Any]:
    try:
        import edge_tts
    except ImportError as exc:
        raise TtsDependencyError("edge-tts is required for Edge TTS. Run: .\\build.ps1 -WithAsr") from exc

    selected_voice = _edge_tts_voice(voice)
    kwargs: dict[str, Any] = {
        "text": text,
        "voice": selected_voice,
        "rate": _edge_tts_rate(),
        "volume": _edge_tts_volume(),
        "pitch": _edge_tts_pitch(),
    }
    style = os.getenv("AGENT_TTS_STYLE", "").strip()
    if style:
        kwargs["style"] = style
    output_format = _edge_tts_output_format(audio_format)
    if output_format:
        kwargs["output_format"] = output_format

    communicate_cls = edge_tts.Communicate
    signature = inspect.signature(communicate_cls)
    accepts_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
    supported_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters or accepts_var_kwargs
    }
    if output_format and "output_format" not in supported_kwargs:
        raise TtsDependencyError("installed edge-tts does not support output_format for Opus output")
    communicate = communicate_cls(**supported_kwargs)
    await communicate.save(str(output_path))
    return {
        "voice": selected_voice,
        "rate": kwargs["rate"],
        "volume": kwargs["volume"],
        "pitch": kwargs["pitch"],
        "style": style,
        "edge_output_format": output_format,
        "codec": "opus" if audio_format in {"opus", "webm", "webm_opus"} else "",
    }


def _synthesize_with_edge_tts(text: str, output_path: Path, voice: str = "", audio_format: str = DEFAULT_FORMAT) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        config = asyncio.run(_edge_tts_save_async(text, output_path, voice, audio_format))
    else:
        result: dict[str, Any] = {}
        error: BaseException | None = None

        def runner() -> None:
            nonlocal result, error
            try:
                result = asyncio.run(_edge_tts_save_async(text, output_path, voice, audio_format))
            except BaseException as exc:  # noqa: BLE001 - move async worker error to caller.
                error = exc

        import threading

        thread = threading.Thread(target=runner, name="edge-tts-save")
        thread.start()
        thread.join()
        if error is not None:
            raise error
        config = result

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"Edge TTS did not create audio file: {output_path}")
    return config


def _powershell_exe() -> str:
    return os.getenv(
        "AGENT_TTS_POWERSHELL",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )


def _run_system_speech_script(script: str) -> dict[str, Any]:
    process = subprocess.run(
        [_powershell_exe(), "-NoProfile", "-STA", "-Command", script],
        cwd=str(ROOT_DIR),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(5, _int_env("AGENT_TTS_TIMEOUT_SECONDS") or 60),
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        raise RuntimeError(f"System.Speech TTS failed: {detail or process.returncode}")
    output = (process.stdout or "").strip()
    if not output:
        return {}
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"System.Speech returned invalid JSON: {output}") from exc
    return data if isinstance(data, dict) else {}


def _system_speech_script(text: str, output_path: Path, voice: str = "") -> str:
    payload = {
        "text": text,
        "path": str(output_path),
        "voice": voice or os.getenv("AGENT_TTS_VOICE", ""),
        "rate": _int_env("AGENT_TTS_RATE"),
        "volume": _int_env("AGENT_TTS_VOLUME"),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    return f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$payload = ConvertFrom-Json @'
{payload_json}
'@
Add-Type -AssemblyName System.Speech
$dir = Split-Path -Parent $payload.path
if ($dir) {{ New-Item -ItemType Directory -Force -Path $dir | Out-Null }}
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{
    $installedVoices = @($speaker.GetInstalledVoices() | Where-Object {{ $_.Enabled }})
    if ($installedVoices.Count -le 0) {{
        throw 'No enabled System.Speech voices are installed.'
    }}
    if ($payload.voice) {{
        $speaker.SelectVoice($payload.voice)
    }}
    if ($null -ne $payload.rate) {{
        $speaker.Rate = [Math]::Max(-10, [Math]::Min(10, [int]$payload.rate))
    }}
    if ($null -ne $payload.volume) {{
        $speaker.Volume = [Math]::Max(0, [Math]::Min(100, [int]$payload.volume))
    }}
    $speaker.SetOutputToWaveFile($payload.path)
    $speaker.Speak($payload.text)
    $speaker.SetOutputToNull()
    $voices = @($installedVoices | ForEach-Object {{ $_.VoiceInfo.Name }})
    [pscustomobject]@{{
        voice = $speaker.Voice.Name
        rate = $speaker.Rate
        volume = $speaker.Volume
        available_voices = $voices
    }} | ConvertTo-Json -Compress
}}
finally {{
    $speaker.Dispose()
}}
"""


def _system_speech_warm_script() -> str:
    return """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $installedVoices = @($speaker.GetInstalledVoices() | Where-Object { $_.Enabled })
    $voices = @($installedVoices | ForEach-Object { $_.VoiceInfo.Name })
    [pscustomobject]@{
        voice = $speaker.Voice.Name
        rate = $speaker.Rate
        volume = $speaker.Volume
        available_voices = $voices
    } | ConvertTo-Json -Compress
}
finally {
    $speaker.Dispose()
}
"""


def _synthesize_with_system_speech(text: str, output_path: Path, voice: str = "") -> dict[str, Any]:
    if platform.system() != "Windows":
        raise TtsDependencyError("System.Speech TTS is only available on Windows")
    config = _run_system_speech_script(_system_speech_script(text, output_path, voice=voice))
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"TTS synthesis did not create audio file: {output_path}")
    return config


def _warm_system_speech() -> dict[str, Any]:
    if platform.system() != "Windows":
        raise TtsDependencyError("System.Speech TTS is only available on Windows")
    return _run_system_speech_script(_system_speech_warm_script())


def _safe_output_path(output_path: str | Path | None, audio_format: str) -> Path:
    suffix = ".webm" if audio_format == "webm_opus" else f".{audio_format}"
    if output_path:
        path = Path(output_path).expanduser().resolve()
        if path.suffix.lower() != suffix:
            path = path.with_suffix(suffix)
    else:
        TTS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = TTS_OUTPUT_DIR / f"tts-{int(time.time())}-{uuid.uuid4().hex[:8]}{suffix}"

    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def synthesize_speech(
    text: str,
    *,
    output_path: str | Path | None = None,
    provider: str = "",
    voice: str = "",
    audio_format: str = "",
) -> dict[str, Any]:
    cleanup_old_tts_files()

    content = str(text or "").strip()
    if not content:
        raise ValueError("TTS text must not be empty")

    selected_provider = (provider or os.getenv("AGENT_TTS_PROVIDER", DEFAULT_PROVIDER)).strip().lower()
    configured_format = (audio_format or os.getenv("AGENT_TTS_FORMAT", "")).strip().lower()
    if not configured_format and selected_provider == "edge_tts":
        configured_format = "mp3"
    selected_format = (configured_format or DEFAULT_FORMAT).strip().lower()
    selected_format = re.sub(r"[^a-z0-9_]+", "", selected_format) or DEFAULT_FORMAT
    if selected_format == "webmopus":
        selected_format = "webm_opus"
    if selected_format not in {"wav", "mp3", "opus", "webm", "webm_opus"}:
        raise ValueError(f"unsupported TTS audio format: {selected_format}")

    path = _safe_output_path(output_path, selected_format)
    fallback_from = ""
    try:
        if selected_provider == "edge_tts":
            config = _synthesize_with_edge_tts(content, path, voice=voice, audio_format=selected_format)
        elif selected_provider == "system_speech":
            if selected_format != "wav":
                raise ValueError("System.Speech TTS only supports wav output")
            config = _synthesize_with_system_speech(content, path, voice=voice)
        elif selected_provider == "pyttsx3":
            if selected_format != "wav":
                raise ValueError("pyttsx3 TTS only supports wav output")
            config = _synthesize_with_pyttsx3(content, path, voice=voice)
        else:
            raise ValueError(f"unsupported TTS provider: {selected_provider}")
    except Exception:
        if selected_provider != "edge_tts":
            raise
        fallback_from = selected_provider
        selected_provider = FALLBACK_PROVIDER
        selected_format = "wav"
        path = _safe_output_path(output_path, selected_format)
        if selected_provider == "system_speech":
            config = _synthesize_with_system_speech(content, path, voice=voice)
        elif selected_provider == "pyttsx3":
            config = _synthesize_with_pyttsx3(content, path, voice=voice)
        else:
            raise ValueError(f"unsupported fallback TTS provider: {selected_provider}")

    return {
        "path": str(path),
        "filename": path.name,
        "format": selected_format,
        "provider": selected_provider,
        "fallback_from": fallback_from,
        "size": path.stat().st_size,
        **config,
    }


def warm_tts_engine() -> dict[str, Any]:
    provider = os.getenv("AGENT_TTS_PROVIDER", DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    if provider == "edge_tts":
        try:
            import edge_tts  # noqa: F401

            return {
                "provider": provider,
                "voice": _edge_tts_voice(),
                "style": os.getenv("AGENT_TTS_STYLE", "").strip(),
            }
        except Exception:
            provider = FALLBACK_PROVIDER
    if provider == "system_speech":
        config = _warm_system_speech()
        voices = config.get("available_voices") if isinstance(config.get("available_voices"), list) else []
        return {
            "provider": provider,
            "fallback_from": "edge_tts" if os.getenv("AGENT_TTS_PROVIDER", DEFAULT_PROVIDER).strip().lower() in {"", "edge_tts"} else "",
            "voice_count": len(voices),
            **config,
        }
    if provider != "pyttsx3":
        raise ValueError(f"unsupported TTS provider: {provider}")
    engine = _load_pyttsx3_engine()
    config = _configure_pyttsx3_engine(engine)
    voices = engine.getProperty("voices") or []
    return {
        "provider": provider,
        "voice_count": len(voices),
        **config,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthesize speech with the configured local TTS provider.")
    parser.add_argument("--text", help="Text to synthesize.")
    parser.add_argument("--output", help="Output audio path.")
    parser.add_argument("--provider", default="", help="TTS provider. Default: AGENT_TTS_PROVIDER or platform default.")
    parser.add_argument("--voice", default="", help="Voice id or name.")
    parser.add_argument("--format", default="", help="Audio format. Default: AGENT_TTS_FORMAT or wav.")
    parser.add_argument("--warm", action="store_true", help="Load the configured TTS provider and exit.")
    args = parser.parse_args(argv)

    try:
        if args.warm:
            result = warm_tts_engine()
        else:
            if not args.text:
                parser.error("--text is required unless --warm is set")
            result = synthesize_speech(
                args.text,
                output_path=args.output,
                provider=args.provider,
                voice=args.voice,
                audio_format=args.format,
            )
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
