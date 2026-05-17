@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem /**
rem  * @file build.bat
rem  * @brief Rebuild the CodeBrain Docker images and recreate the affected containers.
rem  *
rem  * Default mode rebuilds every image and recreates the proxy sidecars plus
rem  * `mcp`/`mcp_frontdoor`, which is the common case when iterating on MCP
rem  * server code. Pass --reset to also recreate `postgres` (the named
rem  * `postgres_data` volume is preserved).
rem  * Pass --wipe to drop the named volume before recreating, which forces
rem  * `schema.sql` to be re-applied; this destroys all indexed data.
rem  */

set "POSTGRES_VOLUME=codebrain_postgres_data"
set "mode=mcp"
set "assume_yes=false"

:parse_args
if "%~1"=="" goto args_done
set "arg=%~1"
if /I "%arg%"=="-y" (
  set "assume_yes=true"
  shift
  goto parse_args
)
if /I "%arg%"=="--yes" (
  set "assume_yes=true"
  shift
  goto parse_args
)
if /I "%arg%"=="--mcp" (
  set "mode=mcp"
  shift
  goto parse_args
)
if /I "%arg%"=="--reset" (
  set "mode=reset"
  shift
  goto parse_args
)
if /I "%arg%"=="--wipe" (
  set "mode=wipe"
  shift
  goto parse_args
)
if /I "%arg%"=="-h" (
  call :show_help
  exit /b 0
)
if /I "%arg%"=="--help" (
  call :show_help
  exit /b 0
)
echo Unknown argument: %arg% 1>&2
call :show_help
exit /b 2

:args_done
set "script_dir=%~dp0"
for %%I in ("%script_dir%..") do set "repo_root=%%~fI"
set "compose_file=%repo_root%\docker\docker-compose.yml"
set "resolver_script=%script_dir%resolve-container-endpoints.ps1"
if not exist "%resolver_script%" (
  echo Missing helper script: %resolver_script% 1>&2
  exit /b 1
)

set "resolver_out=%TEMP%\codebrain_build_resolver_%RANDOM%_%RANDOM%.txt"
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

if /I "%mode%"=="wipe" (
  if /I not "%assume_yes%"=="true" (
    echo About to DESTROY all indexed data by dropping the `%POSTGRES_VOLUME%` named volume.
    echo Every repository indexed in this CodeBrain instance will be lost and must be re-ingested.
    echo.
    echo Pass --yes to skip this prompt.
    set "confirmation="
    set /p "confirmation=Type 'wipe' to confirm: "
    if /I not "!confirmation!"=="wipe" (
      echo Aborted. 1>&2
      exit /b 1
    )
  )

  echo Stopping postgres + mcp...
  docker compose -f "%compose_file%" --profile indexer --profile tools rm -sf postgres embed_proxy classifier_proxy mcp mcp_frontdoor
  if errorlevel 1 exit /b %errorlevel%

  echo Removing volume %POSTGRES_VOLUME%...
  docker volume inspect "%POSTGRES_VOLUME%" >nul 2>&1
  if errorlevel 1 (
    echo Volume %POSTGRES_VOLUME% did not exist; continuing.
  ) else (
    docker volume rm "%POSTGRES_VOLUME%"
    if errorlevel 1 exit /b %errorlevel%
  )
)

docker compose -f "%compose_file%" --profile indexer --profile tools build
if errorlevel 1 exit /b %errorlevel%

if /I "%mode%"=="reset" (
  docker compose -f "%compose_file%" --profile indexer --profile tools up -d --force-recreate postgres embed_proxy classifier_proxy mcp mcp_frontdoor
  exit /b %errorlevel%
)

if /I "%mode%"=="wipe" (
  docker compose -f "%compose_file%" --profile indexer --profile tools up -d --force-recreate postgres embed_proxy classifier_proxy mcp mcp_frontdoor
  exit /b %errorlevel%
)

docker compose -f "%compose_file%" --profile indexer --profile tools up -d --force-recreate embed_proxy classifier_proxy mcp mcp_frontdoor
exit /b %errorlevel%

:show_help
echo Usage:
echo   scripts\build.bat             Rebuild images and recreate proxy sidecars, `mcp`, and `mcp_frontdoor`.
echo   scripts\build.bat --reset     Rebuild images and recreate `postgres`, proxy sidecars, `mcp`, and `mcp_frontdoor`.
echo                                  Indexed data is preserved.
echo   scripts\build.bat --wipe      Drop the `%POSTGRES_VOLUME%` named volume,
echo                                  then rebuild images and recreate `postgres`
echo                                  proxy sidecars, and `mcp`. Schema.sql is re-applied on first
echo                                  init. DESTROYS ALL INDEXED DATA. Prompts for
echo                                  confirmation unless -y/--yes is also passed.
echo   scripts\build.bat -h^|--help   Show this help.
echo.
echo Flags:
echo   -y, --yes                    Skip the --wipe confirmation prompt.
echo.
echo All modes build all images (including the indexer image consumed by
echo `scripts\index-repo.bat` and `scripts\watch-repo.bat`).
exit /b 0
