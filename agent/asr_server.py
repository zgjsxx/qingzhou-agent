"""Long-running SenseVoice ASR HTTP service."""

from __future__ import annotations

import argparse
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from agent.asr import ASR_OUTPUT_DIR, AsrDependencyError, transcribe_audio, warm_asr_model


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
UPLOAD_ROOT = ASR_OUTPUT_DIR / "server"
_WARM_LOCK = threading.Lock()
_WARM_THREAD: threading.Thread | None = None
_WARM_STATE: dict[str, Any] = {"ready": False, "status": "cold"}

app = FastAPI(title="Qingzhou ASR Server")


def _safe_filename(name: str | None) -> str:
    base = Path(name or "recording.webm").name
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if ord(char) <= 31 or char in invalid else char for char in base).strip()
    return cleaned or "recording.webm"


def _warm_state_snapshot() -> dict[str, Any]:
    with _WARM_LOCK:
        return dict(_WARM_STATE)


def _set_warm_state(**updates: Any) -> None:
    with _WARM_LOCK:
        _WARM_STATE.update(updates)


def _warmup_worker() -> None:
    try:
        result = warm_asr_model()
    except Exception as exc:  # noqa: BLE001 - expose local service readiness state.
        _set_warm_state(
            ready=False,
            status="error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    _set_warm_state(ready=True, status="ready", error=None, error_type=None, **result)


def _ensure_warmup_started() -> None:
    global _WARM_THREAD

    with _WARM_LOCK:
        if _WARM_STATE.get("status") == "ready":
            return
        if _WARM_THREAD is not None and _WARM_THREAD.is_alive():
            return
        _WARM_STATE.update({"ready": False, "status": "loading", "error": None, "error_type": None})
        thread = threading.Thread(target=_warmup_worker, name="qingzhou-asr-warmup", daemon=True)
        _WARM_THREAD = thread

    thread.start()


@app.on_event("startup")
def startup() -> None:
    _ensure_warmup_started()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, **_warm_state_snapshot()}


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...), language: str = Form("auto")) -> JSONResponse:
    request_dir = UPLOAD_ROOT / uuid.uuid4().hex
    request_dir.mkdir(parents=True, exist_ok=True)
    audio_path = request_dir / _safe_filename(file.filename)

    try:
        _ensure_warmup_started()
        content = await file.read()
        audio_path.write_bytes(content)
        result = transcribe_audio(audio_path, language=language)
        result.pop("raw", None)
        return JSONResponse({"ok": True, **result})
    except AsrDependencyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - expose structured local-service errors.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Qingzhou SenseVoice ASR server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run("agent.asr_server:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
