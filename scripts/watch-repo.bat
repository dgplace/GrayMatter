@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem /**
rem  * @file watch-repo.bat
rem  * @brief Run CodeBrain watch-mode ingestion for a target repository.
rem  *
rem  * Wraps the indexer container invocation and appends `--watch` unless the
rem  * caller already supplied it explicitly.
rem  */

if /I "%~1"=="-h" (
  call :show_help
  exit /b 0
)
if /I "%~1"=="--help" (
  call :show_help
  exit /b 0
)

set "script_dir=%~dp0"
set "forward_args="
set "has_watch_flag=false"

:collect_args
if "%~1"=="" goto run_index
if /I "%~1"=="--watch" set "has_watch_flag=true"
if defined forward_args (
  set "forward_args=!forward_args! \"%~1\""
) else (
  set "forward_args=\"%~1\""
)
shift
goto collect_args

:run_index
if /I "%has_watch_flag%"=="false" (
  if defined forward_args (
    set "forward_args=!forward_args! \"--watch\""
  ) else (
    set "forward_args=\"--watch\""
  )
)

if defined forward_args (
  call "%script_dir%index-repo.bat" !forward_args!
  exit /b %errorlevel%
)

call "%script_dir%index-repo.bat" --watch
exit /b %errorlevel%

:show_help
echo Usage:
echo   scripts\watch-repo.bat [REPO_PATH] [INGEST_ARGS...]
echo.
echo Examples:
echo   scripts\watch-repo.bat
echo   scripts\watch-repo.bat C:\absolute\path\to\repo
echo   scripts\watch-repo.bat ..\other-repo --force --no-classify
echo.
echo Notes:
echo   - REPO_PATH defaults to the current working directory.
echo   - `--watch` is added automatically if you do not pass it yourself.
exit /b 0
