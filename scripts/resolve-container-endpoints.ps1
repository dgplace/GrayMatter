<#
.SYNOPSIS
Resolves container-safe endpoint environment overrides for CodeBrain helper scripts.

.DESCRIPTION
Reads `codebrain.toml` from `.env/codebrain.toml` and `codebrain.toml` at repo root,
and emits KEY=VALUE lines for embedding settings, endpoint URLs, and proxy
upstream target values. Endpoint hosts are always reached through in-stack proxy
sidecars. Non-local hosts are only allowed when they match configured
embedding/classifier endpoint values.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-SectionConfigMap {
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

        if ($line -match '^\s*(base_url|model|api_style)\s*=\s*"([^"]*)"\s*$') {
            $fieldName = $matches[1]
            $value = $matches[2]
            if ($currentSection -in @("embeddings", "classifier")) {
                $map["$currentSection`_$fieldName"] = $value
            }
        }
    }

    return $map
}

function Resolve-ContainerProxyUrl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EnvName,
        [AllowEmptyString()]
        [string]$Url,
        [AllowEmptyString()]
        [string]$ConfiguredUrl
    )

    if ([string]::IsNullOrWhiteSpace($Url)) {
        return @{}
    }

    $resolved = Get-EndpointHostPort -Url $Url
    $uriHost = $resolved.Host
    $uriPort = $resolved.Port
    $configuredResolved = $null
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredUrl)) {
        $configuredResolved = Get-EndpointHostPort -Url $ConfiguredUrl
    }

    $proxyBaseUrl = ""
    $proxyTargetEnv = ""
    if ($EnvName -eq "EMBED_BASE_URL") {
        $proxyBaseUrl = "http://embed_proxy:11434"
        $proxyTargetEnv = "EMBED_PROXY_TARGET"
    } elseif ($EnvName -eq "CLASSIFIER_BASE_URL") {
        $proxyBaseUrl = "http://classifier_proxy:3000"
        $proxyTargetEnv = "CLASSIFIER_PROXY_TARGET"
    } else {
        throw "Unsupported endpoint variable: $EnvName"
    }

    if ($uriHost -notin @("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal")) {
        if ($null -eq $configuredResolved) {
            throw "Non-local endpoint is blocked by policy: $Url"
        }
        if ($uriHost -ne $configuredResolved.Host -or $uriPort -ne $configuredResolved.Port) {
            throw "Non-local endpoint does not match configured policy value: $Url"
        }
    }

    $upstreamHost = $uriHost
    if ($uriHost -in @("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal")) {
        $upstreamHost = "host.docker.internal"
    }

    return @{
        $EnvName = $proxyBaseUrl
        $proxyTargetEnv = "$upstreamHost`:$uriPort"
    }
}

function Get-EndpointHostPort {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url
    )

    try {
        $uri = [System.Uri]$Url
    } catch {
        throw "Invalid URL value: $Url"
    }

    $uriHost = $uri.Host.ToLowerInvariant()
    $uriPort = $uri.Port
    if ($uriPort -lt 1) {
        if ($uri.Scheme -eq "http") {
            $uriPort = 80
        } elseif ($uri.Scheme -eq "https") {
            $uriPort = 443
        } else {
            throw "Unsupported URL scheme (expected http/https): $Url"
        }
    }

    return @{
        Host = $uriHost
        Port = $uriPort
    }
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
        $pathMap = Get-SectionConfigMap -Path $candidatePath
        foreach ($key in $pathMap.Keys) {
            $configMap[$key] = $pathMap[$key]
        }
    }
}

foreach ($item in @(
    @{ Key = "embeddings_model"; Env = "EMBED_MODEL" },
    @{ Key = "embeddings_api_style"; Env = "EMBED_API_STYLE" }
)) {
    if ($configMap.ContainsKey($item.Key) -and -not [string]::IsNullOrWhiteSpace($configMap[$item.Key])) {
        Write-Output "$($item.Env)=$($configMap[$item.Key])"
    }
}

foreach ($item in @(
    @{ Env = "EMBED_BASE_URL"; Section = "embeddings" },
    @{ Env = "CLASSIFIER_BASE_URL"; Section = "classifier" }
)) {
    $envName = $item.Env
    $section = $item.Section

    $existing = [Environment]::GetEnvironmentVariable($envName)
    $configured = ""
    $configKey = "${section}_base_url"
    if ($configMap.ContainsKey($configKey)) {
        $configured = $configMap[$configKey]
    }

    if (-not [string]::IsNullOrWhiteSpace($existing)) {
        $translatedExisting = Resolve-ContainerProxyUrl -EnvName $envName -Url $existing -ConfiguredUrl $configured
        foreach ($key in $translatedExisting.Keys) {
            Write-Output "$key=$($translatedExisting[$key])"
        }
        continue
    }

    $raw = $configured

    $translated = Resolve-ContainerProxyUrl -EnvName $envName -Url $raw -ConfiguredUrl $configured
    foreach ($key in $translated.Keys) {
        Write-Output "$key=$($translated[$key])"
    }
}
