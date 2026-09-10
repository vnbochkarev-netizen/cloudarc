param(
    [string]$RemoteUrl = ""
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )
    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git command failed: git $($Arguments -join ' ')"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required. Install Git, then rerun this script."
}

if (-not (Test-Path -LiteralPath ".git")) {
    Invoke-Git @("init")
}

Invoke-Git @("branch", "-M", "main")
Invoke-Git @("add", "-A")

& git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "No staged changes; keeping the existing commit history."
} else {
    Invoke-Git @(
        "-c", "user.name=CloudArc Release",
        "-c", "user.email=release@cloudarc.invalid",
        "commit",
        "-m",
        "CloudArc 1.1.0: bounded-memory CI and telemetry"
    )
}

if ($RemoteUrl) {
    & git remote get-url origin *> $null
    if ($LASTEXITCODE -eq 0) {
        Invoke-Git @("remote", "set-url", "origin", $RemoteUrl)
    } else {
        Invoke-Git @("remote", "add", "origin", $RemoteUrl)
    }
}

Write-Host ""
Write-Host "Repository prepared. No push was performed."
Write-Host "Next command:"
Write-Host "  git push -u origin main"
