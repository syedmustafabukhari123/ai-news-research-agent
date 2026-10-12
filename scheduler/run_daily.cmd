@echo off
setlocal

set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\logs"
set "SCHEDULER_LOG=%LOG_DIR%\scheduler-console.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
pushd "%PROJECT_ROOT%" || exit /b 10

if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" goto use_project_python

py -3.12 "%PROJECT_ROOT%\scheduler\windows_launcher.py" %* >> "%SCHEDULER_LOG%" 2>&1
set "RUN_EXIT=%ERRORLEVEL%"
goto finished

:use_project_python
"%PROJECT_ROOT%\.venv\Scripts\python.exe" "%PROJECT_ROOT%\scheduler\windows_launcher.py" %* >> "%SCHEDULER_LOG%" 2>&1
set "RUN_EXIT=%ERRORLEVEL%"

:finished
>> "%SCHEDULER_LOG%" echo [%date% %time%] launcher_exit_code=%RUN_EXIT%
popd
exit /b %RUN_EXIT%
