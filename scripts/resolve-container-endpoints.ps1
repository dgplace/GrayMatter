<#
.SYNOPSIS
Resolves container-safe endpoint environment overrides for CodeBrain helper scripts.

.DESCRIPTION
Reads `codebrain.toml` from `.env/codebrain.toml` and `codebrain.toml` at repo root,
and emits KEY=VALUE lines for EMBED_BASE_URL and CLASSIFIER_BASE_URL. If either value
targets localhost/loopback, host is rewritten to `host.docker.internal` so containers
can reach host services.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-SectionBaseUrlMap {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $map = @{}
    $currentSection = ""

    foreach ($line in Get-Content -Path $Path) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            $currentSection = $matches[1].Trim()
            continue
        }

        if ($line -match '^\s*base_url\s*=\s*"([^"]*)"\s*$') {
            $value = $matches[1]
            if ($currentSection -eq "embeddings") {
                $map["embeddings"] = $value
            } elseif ($currentSection -eq "classifier") {
                $map["classifier"] = $value
            }
        }
    }

    return $map
}

function Translate-ContainerUrl {
    param(
        [AllowEmptyString()]
        [string]$Url
    )

    if ([string]::IsNullOrWhiteSpace($Url)) {
        return ""
    }

    try {
        $uri = [System.Uri]$Url
    } catch {
        return $Url
    }

    $uriHost = $uri.Host.ToLowerInvariant()
    if ($uriHost -notin @("127.0.0.1", "localhost", "::1", "0.0.0.0")) {
        return $Url
    }

    $builder = New-Object System.UriBuilder($uri)
    $builder.Host = "host.docker.internal"
    return $builder.Uri.AbsoluteUri.TrimEnd('/')
}

$localConfigPath = Join-Path $RepoRoot ".env\codebrain.toml"
if (-not (Test-Path -LiteralPath $localConfigPath)) {
    Write-Error "Missing required .env\codebrain.toml. Copy codebrain.example.toml to .env\codebrain.toml and configure it."
    exit 1
}

$candidatePaths = @(
    (Join-Path $RepoRoot "codebrain.toml"),
    $localConfigPath
)

$configMap = @{}
foreach ($candidatePath in $candidatePaths) {
    if (Test-Path -LiteralPath $candidatePath) {
        $pathMap = Get-SectionBaseUrlMap -Path $candidatePath
        foreach ($key in $pathMap.Keys) {
            $configMap[$key] = $pathMap[$key]
        }
    }
}

foreach ($item in @(
    @{ Env = "EMBED_BASE_URL"; Section = "embeddings" },
    @{ Env = "CLASSIFIER_BASE_URL"; Section = "classifier" }
)) {
    $envName = $item.Env
    $section = $item.Section

    $existing = [Environment]::GetEnvironmentVariable($envName)
    if (-not [string]::IsNullOrWhiteSpace($existing)) {
        Write-Output "$envName=$existing"
        continue
    }

    $raw = ""
    if ($configMap.ContainsKey($section)) {
        $raw = $configMap[$section]
    }

    $translated = Translate-ContainerUrl -Url $raw
    if (-not [string]::IsNullOrWhiteSpace($translated)) {
        Write-Output "$envName=$translated"
    }
}
