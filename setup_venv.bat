@ECHO OFF
CHCP 65001 >NUL
TITLE AI Chat - Setup venv

CD /D "%~dp0"

ECHO.
ECHO ========================================
ECHO  AI Chat - Virtual Environment Setup
ECHO ========================================
ECHO.

REM Find Python
WHERE python >NUL 2>NUL
IF %ERRORLEVEL% NEQ 0 (
    IF EXIST "WPy64-31320\scripts\python.bat" (
        SET "PATH=%CD%\WPy64-31320\python;%PATH%"
    ) ELSE (
        ECHO ERROR: Python not found!
        ECHO Please install Python 3.10+ or run setup.cmd first.
        PAUSE
        EXIT /B 1
    )
)

ECHO Python found:
python --version
ECHO.

REM Check if venv already exists
IF EXIST "venv\Scripts\activate.bat" (
    ECHO Virtual environment already exists.
    ECHO.
    SET /P RECREATE="Recreate it? (y/N): "
    IF /I NOT "%RECREATE%"=="y" (
        ECHO Skipping.
        GOTO :install_deps
    )
    ECHO Removing old venv...
    RMDIR /S /Q venv 2>NUL
)

ECHO [1/3] Creating virtual environment...
python -m venv venv
IF %ERRORLEVEL% NEQ 0 (
    ECHO ERROR: Failed to create virtual environment.
    PAUSE
    EXIT /B 1
)
ECHO       Done.
ECHO.

:install_deps
ECHO [2/3] Activating venv...
CALL venv\Scripts\activate.bat
ECHO       Done.
ECHO.

ECHO [3/3] Installing dependencies...
pip install --upgrade pip >NUL 2>NUL
pip install -r requirements.txt
IF %ERRORLEVEL% NEQ 0 (
    ECHO.
    ECHO WARNING: Some dependencies failed to install.
    ECHO Trying individual packages...
    pip install flask>=3.0
    pip install pyyaml>=6.0
    pip install requests>=2.31
    pip install waitress>=3.0
    pip install psutil>=5.9
)
ECHO       Done.
ECHO.

ECHO ========================================
ECHO  Setup Complete!
ECHO ========================================
ECHO.
ECHO  Run "start.cmd" to launch the server.
ECHO.
PAUSE
