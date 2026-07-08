@echo off
setlocal

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo Cannot find project Python at "%PYTHON%". Create .venv or run the project setup first. 1>&2
  exit /b 1
)

"%PYTHON%" -m qingzhou_cli.main %*
exit /b %ERRORLEVEL%
