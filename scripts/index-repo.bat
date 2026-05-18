@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem /**
rem  * @file index-repo.bat
rem  * @brief Run CodeBrain ingestion for a target repository through the indexer container.
rem  *
rem  * This helper hides the docker compose profile and bind-mount convention
rem  * behind one command. The first positional argument is the host repository
rem  * path; any remaining arguments are forwarded to `python -m codebrain.ingest`.
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
set "target_repo_set=false"
set "database_url="
set "docker_add_host_args="
set "ingest_args="

:collect_args
if "%~1"=="" goto args_done
set "arg=%~1"
if /I "%arg%"=="--database-url" (
  if "%~2"=="" (
    echo Missing value for --database-url 1>&2
    exit /b 1
  )
  set "database_url=%~2"
  shift
  shift
  goto collect_args
)
if /I "%arg%"=="--add-host" (
  if "%~2"=="" (
    echo Missing value for --add-host 1>&2
    exit /b 1
  )
  set "docker_add_host_args=%docker_add_host_args% --add-host %~2"
  shift
  shift
  goto collect_args
)
echo(%arg%| findstr /B /I /C:"--database-url=" >nul
if not errorlevel 1 (
  set "database_url=%arg:~15%"
  shift
  goto collect_args
)
echo(%arg%| findstr /B /I /C:"--add-host=" >nul
if not errorlevel 1 (
  set "docker_add_host_args=%docker_add_host_args% --add-host %arg:~11%"
  shift
  goto collect_args
)

set "first_char=%arg:~0,1%"
if /I "%target_repo_set%"=="false" if not "%first_char%"=="-" (
  set "target_repo=%~1"
  set "target_repo_set=true"
  shift
  goto collect_args
)

if defined ingest_args (
  set "ingest_args=!ingest_args! %1"
) else (
  set "ingest_args=%1"
)
shift
goto collect_args

:args_done
for %%I in ("%target_repo%") do set "target_repo=%%~fI"
if not exist "%target_repo%\" (
  echo Repository path does not exist: %target_repo% 1>&2
  exit /b 1
)
for %%I in ("%target_repo%") do set "target_repo_name=%%~nxI"

set "has_repo_name_flag=false"
if defined ingest_args (
  echo(!ingest_args!| findstr /I /C:"--repo-name" >nul && set "has_repo_name_flag=true"
)

set "resolver_script=%script_dir%resolve-container-endpoints.ps1"
if not exist "%resolver_script%" (
  echo Missing helper script: %resolver_script% 1>&2
  exit /b 1
)

set "resolver_out=%TEMP%\codebrain_resolver_%RANDOM%_%RANDOM%.txt"
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

set "db_env_arg="
if defined database_url (
  set "db_env_arg=-e DATABASE_URL=%database_url%"
)

if /I "%has_repo_name_flag%"=="true" (
  if defined ingest_args (
    call docker compose -f "%compose_file%" --profile indexer run --rm %db_env_arg% %docker_add_host_args% -v "%target_repo%:/target" indexer python -m codebrain.ingest /target !ingest_args!
    exit /b %errorlevel%
  )
  docker compose -f "%compose_file%" --profile indexer run --rm %db_env_arg% %docker_add_host_args% -v "%target_repo%:/target" indexer python -m codebrain.ingest /target
  exit /b %errorlevel%
)

if defined ingest_args (
  call docker compose -f "%compose_file%" --profile indexer run --rm %db_env_arg% %docker_add_host_args% -v "%target_repo%:/target" indexer python -m codebrain.ingest /target --repo-name "%target_repo_name%" !ingest_args!
  exit /b %errorlevel%
)

docker compose -f "%compose_file%" --profile indexer run --rm %db_env_arg% %docker_add_host_args% -v "%target_repo%:/target" indexer python -m codebrain.ingest /target --repo-name "%target_repo_name%"
exit /b %errorlevel%

:show_help
echo Usage:
echo   scripts\index-repo.bat [REPO_PATH] [--database-url URL] [--add-host HOST:IP] [INGEST_ARGS...]
echo.
echo Examples:
echo   scripts\index-repo.bat
echo   scripts\index-repo.bat C:\absolute\path\to\repo --force
echo   scripts\index-repo.bat ..\other-repo --force --no-classify
echo   scripts\index-repo.bat C:\absolute\path\to\repo --force --synthesize
echo.
echo Notes:
echo   - REPO_PATH defaults to the current working directory.
echo   - --database-url overrides the target PostgreSQL DSN for this run only.
echo   - --add-host adds a container host mapping and may be passed multiple times.
echo   - Remaining arguments are passed through to `python -m codebrain.ingest`.
echo   - Pass `--synthesize` to overlay narrative module_intents inline.
echo   - The target repo is mounted at /target inside the container.
echo   - The host folder basename is passed via `--repo-name` unless overridden.
exit /b 0
