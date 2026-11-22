@echo off
setlocal
pushd "%~dp0"
if exist ".runtime\deps\streamlink" set "PYTHONPATH=%~dp0.runtime\deps"
if exist "dist\LiveRecorderDesktop-0.1.3\LiveRecorderDesktop-0.1.3.exe" (
  "dist\LiveRecorderDesktop-0.1.3\LiveRecorderDesktop-0.1.3.exe" --data-dir "%~dp0.runtime\data" --recordings-dir "%~dp0Recordings"
) else if exist "dist\LiveRecorderDesktop-0.1.2\LiveRecorderDesktop-0.1.2.exe" (
  "dist\LiveRecorderDesktop-0.1.2\LiveRecorderDesktop-0.1.2.exe" --data-dir "%~dp0.runtime\data" --recordings-dir "%~dp0Recordings"
) else if exist "dist\LiveRecorderDesktop-0.1.1\LiveRecorderDesktop-0.1.1.exe" (
  "dist\LiveRecorderDesktop-0.1.1\LiveRecorderDesktop-0.1.1.exe" --data-dir "%~dp0.runtime\data" --recordings-dir "%~dp0Recordings"
) else if exist "dist\LiveRecorderDesktop\LiveRecorderDesktop.exe" (
  "dist\LiveRecorderDesktop\LiveRecorderDesktop.exe" --data-dir "%~dp0.runtime\data" --recordings-dir "%~dp0Recordings"
) else if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m liverecorder --data-dir "%~dp0.runtime\data" --recordings-dir "%~dp0Recordings"
) else (
  python -m liverecorder --data-dir "%~dp0.runtime\data" --recordings-dir "%~dp0Recordings"
)
set "LIVE_EXIT=%ERRORLEVEL%"
popd
if not "%LIVE_EXIT%"=="0" pause
exit /b %LIVE_EXIT%
