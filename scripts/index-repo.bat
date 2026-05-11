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
set "ingest_args="

if not "%~1"=="" (
  set "candidate=%~1"
  set "first_char=!candidate:~0,1!"
  if not "!first_char!"=="-" (
    set "target_repo=%~1"
    shift
  )
)

:parse_args
if "%~1"=="" goto args_done
if defined ingest_args (
  set "ingest_args=!ingest_args! \"%~1\""
) else (
  set "ingest_args=\"%~1\""
)
shift
goto parse_args

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

set "tmp_py=%TEMP%\codebrain_translate_%RANDOM%_%RANDOM%.py"
> "%tmp_py%" (
  echo import os
  echo import sys
  echo from pathlib import Path
  echo from urllib.parse import urlparse, urlunparse
  echo.
  echo try:
  echo^    import tomllib
  echo except ModuleNotFoundError:
  echo^    import tomli as tomllib  # type: ignore[no-redef]
  echo.
  echo repo_root = Path(sys.argv[1])
  echo candidate_paths = [repo_root / ".env" / "codebrain.toml", repo_root / "codebrain.toml"]
  echo cfg = {}
  echo for path in candidate_paths:
  echo^    if path.is_file():
  echo^        with path.open("rb") as fh:
  echo^            data = tomllib.load(fh)
  echo^        cfg = {**cfg, **data} if cfg else data
  echo.
  echo LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
  echo.
  echo def translate(url: str) -^> str:
  echo^    if not url:
  echo^        return ""
  echo^    parsed = urlparse(url)
  echo^    if parsed.hostname in LOCAL_HOSTS:
  echo^        port = f":{parsed.port}" if parsed.port else ""
  echo^        return urlunparse(parsed._replace(netloc=f"host.docker.internal{port}"))
  echo^    return url
  echo.
  echo for env_name, section in (("EMBED_BASE_URL", "embeddings"), ("CLASSIFIER_BASE_URL", "classifier")):
  echo^    if os.environ.get(env_name):
  echo^        print(f"{env_name}={os.environ[env_name]}")
  echo^        continue
  echo^    raw = cfg.get(section, {}).get("base_url", "")
  echo^    translated = translate(raw)
  echo^    if translated:
  echo^        print(f"{env_name}={translated}")
)

for /f "usebackq delims=" %%L in (`python "%tmp_py%" "%repo_root%"`) do (
  if not "%%L"=="" set "%%L"
)
set "python_status=%ERRORLEVEL%"
del "%tmp_py%" >nul 2>&1
if not "%python_status%"=="0" exit /b %python_status%

if /I "%has_repo_name_flag%"=="true" (
  if defined ingest_args (
    call docker compose -f "%compose_file%" --profile indexer run --rm -v "%target_repo%:/target" indexer python -m codebrain.ingest /target !ingest_args!
    exit /b %errorlevel%
  )
  docker compose -f "%compose_file%" --profile indexer run --rm -v "%target_repo%:/target" indexer python -m codebrain.ingest /target
  exit /b %errorlevel%
)

if defined ingest_args (
  call docker compose -f "%compose_file%" --profile indexer run --rm -v "%target_repo%:/target" indexer python -m codebrain.ingest /target --repo-name "%target_repo_name%" !ingest_args!
  exit /b %errorlevel%
)

docker compose -f "%compose_file%" --profile indexer run --rm -v "%target_repo%:/target" indexer python -m codebrain.ingest /target --repo-name "%target_repo_name%"
exit /b %errorlevel%

:show_help
echo Usage:
echo   scripts\index-repo.bat [REPO_PATH] [INGEST_ARGS...]
echo.
echo Examples:
echo   scripts\index-repo.bat
echo   scripts\index-repo.bat C:\absolute\path\to\repo --force
echo   scripts\index-repo.bat ..\other-repo --force --no-classify
echo   scripts\index-repo.bat C:\absolute\path\to\repo --force --synthesize
echo.
echo Notes:
echo   - REPO_PATH defaults to the current working directory.
echo   - Remaining arguments are passed through to `python -m codebrain.ingest`.
echo   - Pass `--synthesize` to overlay narrative module_intents inline.
echo   - The target repo is mounted at /target inside the container.
echo   - The host folder basename is passed via `--repo-name` unless overridden.
exit /b 0
