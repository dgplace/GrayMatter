@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem /**
rem  * @file recluster-repo.bat
rem  * @brief Rebuild clusters/logical modules for an indexed repo without file re-ingestion.
rem  *
rem  * The first positional argument is a host repository path used to derive
rem  * default `--repo-name` (basename). Remaining arguments are forwarded to
rem  * `python -m codebrain.recluster`.
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
for %%I in ("%script_dir%..") do set "repo_root=%%~fI"
set "compose_file=%repo_root%\docker\docker-compose.yml"

set "target_repo=%CD%"
set "recluster_args="

if not "%~1"=="" (
  set "candidate=%~1"
  set "first_char=!candidate:~0,1!"
  if not "!first_char!"=="-" (
    set "target_repo=%~1"
    shift
  )
)

:collect_recluster_args
if "%~1"=="" goto args_done
if defined recluster_args (
  set "recluster_args=!recluster_args! %1"
) else (
  set "recluster_args=%1"
)
shift
goto collect_recluster_args

:args_done
for %%I in ("%target_repo%") do set "target_repo=%%~fI"
if not exist "%target_repo%\" (
  echo Repository path does not exist: %target_repo% 1>&2
  exit /b 1
)
for %%I in ("%target_repo%") do set "target_repo_name=%%~nxI"

set "has_repo_name_flag=false"
set "has_resolution_flag=false"
if defined recluster_args (
  echo(!recluster_args!| findstr /I /C:"--repo-name" >nul && set "has_repo_name_flag=true"
  echo(!recluster_args!| findstr /I /C:"--resolution" /C:"--resolution-multiplier" >nul && set "has_resolution_flag=true"
)

set "resolver_script=%script_dir%resolve-container-endpoints.ps1"
if not exist "%resolver_script%" (
  echo Missing helper script: %resolver_script% 1>&2
  exit /b 1
)

set "resolver_out=%TEMP%\codebrain_recluster_resolver_%RANDOM%_%RANDOM%.txt"
powershell -NoProfile -ExecutionPolicy Bypass -File "%resolver_script%" -RepoRoot "%repo_root%" > "%resolver_out%"
set "resolver_status=%ERRORLEVEL%"
if not "%resolver_status%"=="0" (
  type "%resolver_out%" 1>&2
  del "%resolver_out%" >nul 2>&1
  exit /b %resolver_status%
)

for /f "usebackq delims=" %%L in ("%resolver_out%") do (
  if not "%%L"=="" set "%%L"
)
del "%resolver_out%" >nul 2>&1

set "repo_name_arg="
if /I not "%has_repo_name_flag%"=="true" (
  set "repo_name_arg=--repo-name ""%target_repo_name%"""
)

set "resolution_arg="
if /I not "%has_resolution_flag%"=="true" (
  set "resolution_arg=--resolution-multiplier 2.0"
)

if defined recluster_args (
  call docker compose -f "%compose_file%" --profile indexer run --rm indexer python -m codebrain.recluster %repo_name_arg% %resolution_arg% !recluster_args!
  exit /b %errorlevel%
)

docker compose -f "%compose_file%" --profile indexer run --rm indexer python -m codebrain.recluster %repo_name_arg% %resolution_arg%
exit /b %errorlevel%

:show_help
echo Usage:
echo   scripts\recluster-repo.bat [REPO_PATH] [RECLUSTER_ARGS...]
echo.
echo Examples:
echo   scripts\recluster-repo.bat
echo   scripts\recluster-repo.bat C:\absolute\path\to\repo
echo   scripts\recluster-repo.bat C:\absolute\path\to\repo --resolution-multiplier 2.0
echo   scripts\recluster-repo.bat C:\absolute\path\to\repo --resolution 2.0 --min-files 2
echo.
echo Notes:
echo   - REPO_PATH defaults to the current working directory.
echo   - Remaining arguments are passed through to `python -m codebrain.recluster`.
echo   - If no resolution args are provided, the command defaults to 2x configured resolution.
echo   - This command does NOT re-index files; it only rebuilds clusters and logical modules.
exit /b 0
