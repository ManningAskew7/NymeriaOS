<#
.SYNOPSIS
    NymeriaOS installer for Windows (Slim track).

.DESCRIPTION
    Installs the single-process (uv/SQLite) NymeriaOS backend on Windows 11,
    the on-device beta experience: the agent runs on your PC so it can act on
    your real filesystem, with the desktop app as a thin client pointed at
    http://localhost:8000.

    Mirrors the Slim track of install.sh. The Full (Docker) track is offered
    only as a fallback message if the native wheel install fails.

        irm https://get.nymeriaos.com/install.ps1 | iex

    Cautious users: download and read this script before running it, e.g.
        irm https://get.nymeriaos.com/install.ps1 -OutFile install.ps1
        notepad install.ps1
        powershell -ExecutionPolicy Bypass -File .\install.ps1

.PARAMETER NonInteractive
    Do not prompt. Skips the setup wizard and the autostart prompt (an
    unattended install just lands the package and PATH).

.PARAMETER NoAutostart
    Do not offer to create the logon autostart task.

.PARAMETER Help
    Show this help and exit.

.NOTES
    Environment overrides (parity with install.sh):
      NYMERIA_PYPI_SIMPLE_INDEX_URL  Private index URL for the beta wheel.
      NYMERIA_INSTALL_AUTOSTART      "1"/"0" to force the autostart choice
                                     under -NonInteractive (default 0).

    Admin is NOT required. The install is per-user: uv, the package, PATH, and
    the logon task all live under the current user account.
#>

[CmdletBinding()]
param(
    [switch]$NonInteractive,
    [switch]$NoAutostart,
    [switch]$Help
)

# Stop on any unhandled error so a partial install cannot masquerade as success.
$ErrorActionPreference = 'Stop'

# Name of the scheduled task created for logon autostart (work item 2).
$script:TaskName = 'NymeriaOS Slim'

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
function Write-Info { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "warning: $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "error: $Message" -ForegroundColor Red }

function Confirm-Yes {
    # Prompt with a default of yes. Returns $true unless the user types n/no.
    param([string]$Prompt)
    if ($NonInteractive) { return $true }
    $reply = Read-Host -Prompt $Prompt
    if ([string]::IsNullOrWhiteSpace($reply)) { return $true }
    return ($reply -notmatch '^(n|no)$')
}

function Test-Command {
    param([string]$Name)
    $null = Get-Command $Name -ErrorAction SilentlyContinue
    return $?
}

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    return
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
function Invoke-Preflight {
    if ($PSVersionTable.PSVersion.Major -lt 5) {
        Write-Err "PowerShell 5 or newer is required (found $($PSVersionTable.PSVersion))."
        throw 'unsupported-powershell'
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        Write-Err 'A 64-bit version of Windows is required.'
        throw 'unsupported-arch'
    }
    Write-Info "NymeriaOS installer (Windows, PowerShell $($PSVersionTable.PSVersion))"
}

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------
function Install-Uv {
    if (Test-Command 'uv') { return }
    Write-Info 'Installing uv (https://astral.sh/uv) ...'
    # Prefer winget (signed, on modern Windows); fall back to Astral's script.
    $installed = $false
    if (Test-Command 'winget') {
        try {
            winget install --id astral-sh.uv --source winget --accept-source-agreements --accept-package-agreements --disable-interactivity
            $installed = $true
        } catch {
            Write-Warn 'winget install of uv did not complete; falling back to the astral.sh script.'
        }
    }
    if (-not $installed) {
        # Official one-liner. Downloaded and executed in-process; equivalent to
        # install.sh's astral.sh fallback.
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    }
    # uv installs to %USERPROFILE%\.local\bin; make it visible in this session.
    $uvBin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path $uvBin) { $env:PATH = "$uvBin;$env:PATH" }
    if (-not (Test-Command 'uv')) {
        Write-Err 'uv was installed but is not on PATH. Open a new terminal and re-run this installer.'
        throw 'uv-not-on-path'
    }
}

# ---------------------------------------------------------------------------
# PATH persistence
# ---------------------------------------------------------------------------
function Add-ToUserPath {
    # Ensure a directory is on the persistent User PATH and the current session.
    param([string]$Directory)
    if (-not (Test-Path $Directory)) { return }
    $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
    if ([string]::IsNullOrEmpty($userPath)) { $userPath = '' }
    $parts = $userPath -split ';' | Where-Object { $_ -ne '' }
    if ($parts -notcontains $Directory) {
        $newPath = if ($userPath -eq '') { $Directory } else { "$userPath;$Directory" }
        [Environment]::SetEnvironmentVariable('PATH', $newPath, 'User')
        Write-Info "Added $Directory to your PATH (new terminals will see it)."
    }
    if (($env:PATH -split ';') -notcontains $Directory) {
        $env:PATH = "$Directory;$env:PATH"
    }
}

# ---------------------------------------------------------------------------
# Package install (Slim track)
# ---------------------------------------------------------------------------
function Install-Nymeria {
    Write-Info 'Installing the nymeriaos package with uv ...'
    $indexUrl = $env:NYMERIA_PYPI_SIMPLE_INDEX_URL
    try {
        if (-not [string]::IsNullOrWhiteSpace($indexUrl)) {
            uv tool install nymeriaos --index $indexUrl
        } else {
            uv tool install nymeriaos
        }
    } catch {
        Show-WheelFailureHelp
        throw
    }
    if ($LASTEXITCODE -ne 0) {
        Show-WheelFailureHelp
        throw 'package-install-failed'
    }

    # uv tool shims land in %USERPROFILE%\.local\bin.
    $toolBin = Join-Path $env:USERPROFILE '.local\bin'
    Add-ToUserPath -Directory $toolBin

    if (-not (Test-Command 'nymeria')) {
        Write-Err 'The nymeria command is not on PATH after install. Open a new terminal and re-run.'
        throw 'nymeria-not-on-path'
    }
    Write-Info 'Installed. nymeria is on your PATH.'
}

function Show-WheelFailureHelp {
    # A wheel install failed (a source build kicked in and died, or a
    # dependency has no Windows wheel). Point at the containerized Slim
    # fallback rather than dumping a stack trace, matching install.sh's intent.
    Write-Warn 'The native package install failed.'
    Write-Host ''
    Write-Host 'Fall back to the containerized Slim stack (Docker Desktop required):' -ForegroundColor Cyan
    Write-Host '  1. Install Docker Desktop: https://www.docker.com/products/docker-desktop/'
    Write-Host '  2. Then run the Full (Docker) installer:'
    Write-Host '       irm https://get.nymeriaos.com/install.sh -OutFile install.sh'
    Write-Host '       # in WSL or Git Bash:  sh install.sh --full'
    Write-Host ''
    Write-Host 'The Docker path bundles Python and every dependency, so a missing'
    Write-Host 'Windows wheel cannot block it.'
}

# ---------------------------------------------------------------------------
# Setup wizard + doctor
# ---------------------------------------------------------------------------
function Invoke-Wizard {
    if ($NonInteractive) { return }
    if (Confirm-Yes 'Run the setup wizard now (nymeria init)? [Y/n]') {
        # The wizard owns the terminal; run it in-process so it can prompt.
        nymeria init
    }
    Write-Info 'Verifying the install (nymeria doctor) ...'
    nymeria doctor
}

# ---------------------------------------------------------------------------
# Logon autostart task (work item 2)
# ---------------------------------------------------------------------------
function Install-AutostartTask {
    if ($NoAutostart) { return }
    $want = if ($NonInteractive) {
        $env:NYMERIA_INSTALL_AUTOSTART -eq '1'
    } else {
        Confirm-Yes 'Start Nymeria automatically when you log in? [Y/n]'
    }
    if (-not $want) {
        Write-Info 'Skipping autostart. Start the backend anytime with: nymeria slim'
        return
    }

    $nymeriaExe = (Get-Command 'nymeria' -ErrorAction SilentlyContinue).Source
    if (-not $nymeriaExe) {
        Write-Warn 'Could not locate nymeria.exe; skipping autostart. Re-run after opening a new terminal.'
        return
    }

    # Console-window suppression: a plain scheduled task running a console app
    # flashes a window at every logon. We launch through a generated .vbs shim
    # with WScript.Shell.Run(..., 0, False) (0 = hidden window), which starts
    # the backend with no console and no stored-credential requirement (the
    # task runs in the interactive logon session). This is the chosen approach
    # over powershell -WindowStyle Hidden (which still flashes briefly) and
    # over "run whether logged on or not" (which demands a stored password).
    $shimDir = Join-Path $env:USERPROFILE '.nymeria'
    if (-not (Test-Path $shimDir)) { New-Item -ItemType Directory -Path $shimDir -Force | Out-Null }
    $shimPath = Join-Path $shimDir 'nymeria-slim-hidden.vbs'
    $shimBody = @"
' Generated by install.ps1. Launches the Nymeria slim backend with no console
' window. Regenerated on every install; safe to delete (autostart stops).
Set shell = CreateObject("WScript.Shell")
shell.Run """$nymeriaExe"" slim", 0, False
"@
    Set-Content -Path $shimPath -Value $shimBody -Encoding ASCII

    try {
        $action  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$shimPath`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        # Interactive-session task: runs as the logged-in user, no stored
        # password. StartWhenAvailable lets a missed logon catch up.
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force | Out-Null
        Write-Info "Autostart enabled: the '$script:TaskName' task runs 'nymeria slim' hidden at logon."
        Write-Host "  Start it now without logging out:  schtasks /run /tn `"$script:TaskName`""
        Write-Host "  Remove it later:                   schtasks /delete /tn `"$script:TaskName`" /f"
    } catch {
        Write-Warn "Could not register the autostart task: $($_.Exception.Message)"
        Write-Host "  Start the backend manually with: nymeria slim"
    }
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
function Main {
    Invoke-Preflight
    Install-Uv
    Install-Nymeria
    Invoke-Wizard
    Install-AutostartTask

    Write-Host ''
    Write-Info 'NymeriaOS (Slim) is installed.'
    Write-Host 'Next steps:'
    Write-Host '  nymeria init      # guided setup (provider, model, API key)'
    Write-Host '  nymeria doctor    # verify the install'
    Write-Host '  nymeria slim      # start the single-process backend'
    Write-Host ''
    Write-Host 'Then open http://localhost:8000 and paste the bootstrap token,'
    Write-Host 'or point the desktop app at http://localhost:8000.'
}

Main
