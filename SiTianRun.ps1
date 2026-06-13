param(
    [ValidateSet("scan", "loop", "state", "summary")]
    [string]$Action = "scan",

    [string]$ConfigPath = "",

    [string]$RecordsRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $RepoRoot "config/sitian.local.yaml"
}

if ([string]::IsNullOrWhiteSpace($RecordsRoot)) {
    $RecordsRoot = Join-Path $HOME ".kongming/SiTian"
}

function Invoke-SiTianCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & uv run kongming-sitian @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "SiTian command failed: kongming-sitian $($Arguments -join ' ')"
    }
}

function Show-SiTianSummary {
    $summaryPath = Join-Path $RecordsRoot "latest_summary.md"
    if (Test-Path -LiteralPath $summaryPath) {
        Write-Host ""
        Write-Host "===== SiTian Summary ====="
        Get-Content -LiteralPath $summaryPath
    }
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

New-Item -ItemType Directory -Path $RecordsRoot -Force | Out-Null

switch ($Action) {
    "scan" {
        Write-Host "Running SiTian scan once..."
        Invoke-SiTianCommand -Arguments @(
            "run-once",
            "--config", $ConfigPath,
            "--root-dir", $RecordsRoot
        )

        Write-Host ""
        Write-Host "Reading SiTian state..."
        Invoke-SiTianCommand -Arguments @(
            "state",
            "--root-dir", $RecordsRoot
        )

        Show-SiTianSummary
    }

    "loop" {
        Write-Host "Starting SiTian loop..."
        Invoke-SiTianCommand -Arguments @(
            "loop",
            "--config", $ConfigPath,
            "--root-dir", $RecordsRoot
        )
    }

    "state" {
        Invoke-SiTianCommand -Arguments @(
            "state",
            "--root-dir", $RecordsRoot
        )
    }

    "summary" {
        Show-SiTianSummary
    }
}
