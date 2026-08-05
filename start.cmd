@ECHO OFF
CHCP 65001 >NUL
TITLE AI Chat

CD /D "%~dp0"

ECHO.
ECHO ========================================
ECHO  AI Chat Server
ECHO ========================================
ECHO.

REM Check if venv exists
IF NOT EXIST "venv\Scripts\activate.bat" (
    ECHO Virtual environment not found.
    ECHO Running setup first...
    ECHO.
    CALL setup_venv.bat
    IF %ERRORLEVEL% NEQ 0 (
        ECHO ERROR: Setup failed.
        PAUSE
        EXIT /B 1
    )
)

REM Activate venv
CALL venv\Scripts\activate.bat

ECHO Using venv: %VIRTUAL_ENV%
ECHO.

ECHO Starting server... (Ctrl+C to stop)
ECHO.
python app.py --workers 4

PAUSE
