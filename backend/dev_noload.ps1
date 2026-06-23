$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not $env:LANGGRAPH_NO_CLEAN) {
    # Remove stale persistent state so old runs/thread data doesn't auto-resume on startup
    Remove-Item -Recurse -Force .langgraph_api
}

langgraph dev --no-reload
