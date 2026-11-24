@echo off
setlocal
if not exist "%~dp0runtime\pythonw.exe" (
    echo Missing bundled runtime. Extract the complete ZIP to a new folder.
    pause
    exit /b 1
)
start "" "%~dp0runtime\pythonw.exe" -B -X utf8 -m upload_platform.portable_window --bundle "%~dp0." %*
