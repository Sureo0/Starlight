@ECHO OFF
CHCP 65001 >NUL
TITLE AI Chat - Backup

CD /D "%~dp0"

ECHO.
ECHO ========================================
ECHO  AI Chat Backup
ECHO ========================================
ECHO.

REM Try system Python first
WHERE python >NUL 2>NUL
IF %ERRORLEVEL% NEQ 0 (
    REM Try WPy64
    IF EXIST "WPy64-31320\scripts\python.bat" (
        SET "PATH=%CD%\WPy64-31320\python;%PATH%"
    ) ELSE (
        ECHO ERROR: Python not found!
        PAUSE
        EXIT /B 1
    )
)

ECHO Creating backup...
ECHO.
python data\backup.py backup

ECHO.
ECHO ========================================
ECHO  Backup complete! Check data\backups\
ECHO ========================================
ECHO.

REM Show recent backups
python data\backup.py list

PAUSE
