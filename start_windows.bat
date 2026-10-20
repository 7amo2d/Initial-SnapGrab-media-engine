@echo off
setlocal
cd /d "%~dp0"
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do if not "%%A"=="" set "%%A=%%B"
)
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher not found. Install Python 3.10 or newer.
  pause
  exit /b 1
)
where node >nul 2>nul
if errorlevel 1 echo WARNING: Node.js 22+ is recommended for current YouTube support.
where ffmpeg >nul 2>nul
if errorlevel 1 echo WARNING: FFmpeg is required for MP3 extraction and many MP4 merges.
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)
echo Media Engine: http://127.0.0.1:8081
python -m uvicorn main:app --host 127.0.0.1 --port 8081
pause
