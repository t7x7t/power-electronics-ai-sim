@echo off
setlocal
rem Double-click launcher; extra arguments pass through to launch.py.
if exist "%~dp0..\..\.venv\Scripts\python.exe" (set "PY=%~dp0..\..\.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" "%~dp0launch.py" %*
if errorlevel 1 pause
