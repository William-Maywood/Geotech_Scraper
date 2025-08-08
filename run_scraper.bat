@echo off
REM Navigate to the folder containing this script
cd /d "%~dp0"

REM Optional: Activate your Python environment (only if using venv)
REM call venv\Scripts\activate

REM Install dependencies (only the first time)
pip install -r requirements.txt

REM Run the Python script
python app.py

pause