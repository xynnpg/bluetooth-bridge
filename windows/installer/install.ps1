<#
.SYNOPSIS
    Bluetooth Bridge — Windows Installer

.DESCRIPTION
    Single-command install for the Windows side of the Bluetooth Bridge.

    Run from an elevated PowerShell:
        irm https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/windows/installer/install.ps1 | iex

.ENVIRONMENT
    Requires Python 3.8+, internet access, and administrator privileges
    (for ViGEmBus driver installation).
#>

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

# ── Constants ─────────────────────────────────────────────────────────────────

$VERSION       = "1.0.0"
$APP_NAME      = "Bluetooth Bridge"
$INSTALL_DIR   = "$env:USERPROFILE\bluetooth_bridge"
$VENV_DIR      = "$INSTALL_DIR\venv"
$LISTEN_PORT   = 9999
$REPO_BASE     = "https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/windows"
$REPO_ZIP_URL  = "https://github.com/xynnpg/bluetooth-bridge/archive/refs/heads/main.zip"

# Detect whether we are running from a saved .ps1 file or piped via irm | iex.
# When piped, $PSCommandPath and $PSScriptRoot are both empty strings / $null.
$isIrmMode = [string]::IsNullOrWhiteSpace($PSCommandPath)

# ── Colours ───────────────────────────────────────────────────────────────────

function Write-Step($msg)   { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Success($msg){ Write-Host "[✓] $msg" -ForegroundColor Green }
function Write-Warn($msg)   { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)    { Write-Host "[✗] $msg" -ForegroundColor Red }
function Write-Info($msg)   { Write-Host "    $msg" -ForegroundColor Gray }

# ── Banner ────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║      Bluetooth Bridge — Installer v$VERSION    ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ── Admin check ───────────────────────────────────────────────────────────────

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Err "This installer must be run as Administrator (needed for ViGEmBus)."
    Write-Info "Restart PowerShell as Admin and run again."
    exit 1
}

# ── 1. Detect Python ──────────────────────────────────────────────────────────

Write-Step "Checking Python installation …"

$pythonCmd = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $v = & $candidate --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match "Python (\d+)\.(\d+)") {
            $major, $minor = [int]$Matches[1], [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 8)) {
                $pythonCmd = $candidate
                Write-Success "Found: $v"
                break
            }
        }
    } catch { }
}

if (-not $pythonCmd) {
    Write-Warn "Python 3.8+ not found. Downloading from python.org …"
    $pyUrl = "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe"
    $tmp = "$env:TEMP\python-setup.exe"
    try {
        Invoke-WebRequest -Uri $pyUrl -OutFile $tmp -TimeoutSec 120
        Start-Process -Wait -FilePath $tmp -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1"
        Remove-Item $tmp -Force
        $pythonCmd = "python"
        Write-Success "Python installed."
    } catch {
        Write-Err "Failed to install Python. Install manually from https://python.org then re-run."
        exit 1
    }
}

# ── 2. Check / Install ViGEmBus ───────────────────────────────────────────────

Write-Step "Checking ViGEmBus driver …"

$vigembInstalled = $null -ne (Get-Service -Name "ViGEmBus" -ErrorAction SilentlyContinue)

if (-not $vigembInstalled) {
    Write-Info "ViGEmBus not found. Downloading latest release …"
    $releaseUrl = "https://api.github.com/repos/ViGEm/ViGEmBus/releases/latest"
    try {
        $json    = Invoke-RestMethod -Uri $releaseUrl -TimeoutSec 15
        $asset   = $json.assets | Where-Object { $_.name -like "*x86*64*.exe" } | Select-Object -First 1
        if (-not $asset) { $asset = $json.assets | Select-Object -First 1 }
        $vigEmbUrl = $asset.browser_download_url
        $tmp = "$env:TEMP\ViGEmBus_setup.exe"
        Invoke-WebRequest -Uri $vigEmbUrl -OutFile $tmp -TimeoutSec 120
        Start-Process -Wait -FilePath $tmp -ArgumentList "/quiet", "/norestart"
        Remove-Item $tmp -Force
        Write-Success "ViGEmBus installed."
    } catch {
        Write-Warn "Could not auto-download ViGEmBus. Download manually from:"
        Write-Info "  https://github.com/ViGEm/ViGEmBus/releases"
        Write-Info "Then re-run this installer."
    }
} else {
    Write-Success "ViGEmBus already installed."
}

# ── 3. Create install directory ───────────────────────────────────────────────

Write-Step "Installing to $INSTALL_DIR …"
New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null

# ── 4. Deploy application files ──────────────────────────────────────────────

Write-Step "Deploying application files …"

# When run as a saved script from a checked-out repo, try to copy local files.
# When run via irm | iex (no backing file), always download from GitHub.
$localDeployed = $false

if (-not $isIrmMode -and -not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
    # $PSScriptRoot = …/windows/installer  →  repo root is two levels up
    $repoRoot    = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    $localAppSrc = Join-Path $repoRoot "windows\src"
    $localReq    = Join-Path $repoRoot "windows\requirements.txt"

    if (Test-Path $localAppSrc) {
        Copy-Item -Recurse $localAppSrc "$INSTALL_DIR\src\" -Force
        if (Test-Path $localReq) {
            Copy-Item $localReq "$INSTALL_DIR\" -Force
        }
        Write-Info "Deployed from local repo: $repoRoot"
        $localDeployed = $true
    }
}

if (-not $localDeployed) {
    Write-Info "Downloading source from GitHub …"
    $tmpZip = "$env:TEMP\bluetooth-bridge.zip"
    $tmpDir = "$env:TEMP\bluetooth-bridge-extract"

    try {
        Invoke-WebRequest -Uri $REPO_ZIP_URL -OutFile $tmpZip -TimeoutSec 60
        if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
        Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force
        Remove-Item $tmpZip -Force

        # The zip extracts to a folder named bluetooth-bridge-main (or similar)
        $extractedRoot = Get-ChildItem $tmpDir -Directory | Select-Object -First 1
        if (-not $extractedRoot) {
            throw "Could not find extracted directory inside $tmpDir"
        }

        $winSrc = Join-Path $extractedRoot.FullName "windows\src"
        $winReq = Join-Path $extractedRoot.FullName "windows\requirements.txt"

        if (-not (Test-Path $winSrc)) {
            throw "windows\src not found inside downloaded archive"
        }

        Copy-Item $winSrc "$INSTALL_DIR\src\" -Recurse -Force
        if (Test-Path $winReq) {
            Copy-Item $winReq "$INSTALL_DIR\" -Force
        }

        Remove-Item $tmpDir -Recurse -Force
        Write-Success "Source downloaded and deployed."
    } catch {
        Write-Err "Failed to download app source: $_"
        exit 1
    }
}

# ── 5. Install Python dependencies ────────────────────────────────────────────

Write-Step "Installing Python dependencies …"

if (Test-Path $VENV_DIR) {
    Write-Info "Reusing existing venv."
} else {
    & $pythonCmd -m venv $VENV_DIR
    Write-Success "Virtual environment created."
}

$pipExe     = Join-Path $VENV_DIR "Scripts\pip.exe"
$pythonExe  = Join-Path $VENV_DIR "Scripts\python.exe"    # pip / venv ops
$pythonwExe = Join-Path $VENV_DIR "Scripts\pythonw.exe"   # no-console launcher

# pythonw.exe may be absent in minimal Python installs; fall back gracefully
if (-not (Test-Path $pythonwExe)) {
    Write-Warn "pythonw.exe not found — using python.exe (console will be hidden by the app itself)"
    $pythonwExe = $pythonExe
}

# Upgrade pip silently
& $pythonExe -m pip install --upgrade pip --quiet 2>$null | Out-Null

$reqFile = Join-Path $INSTALL_DIR "requirements.txt"
if (Test-Path $reqFile) {
    & $pipExe install --upgrade -r $reqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "pip install failed with version pins — retrying without pins …"
        $content = (Get-Content $reqFile) -replace ">=\d+(\.\d+)*", ""
        $tmpReq  = "$env:TEMP\requirements_fallback.txt"
        $content | Set-Content $tmpReq
        & $pipExe install --upgrade -r $tmpReq
        Remove-Item $tmpReq -Force
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Some packages may not have installed correctly."
        }
    }
    Write-Success "Python dependencies installed."
} else {
    Write-Warn "requirements.txt not found — skipping pip install."
}

# ── 6. Detect local IP address ────────────────────────────────────────────────

Write-Step "Detecting local IP address …"

$ips = @(Get-NetIPAddress -AddressFamily IPv4 |
         Where-Object { $_.IPAddress -ne "127.0.0.1" -and
                        $_.PrefixOrigin -ne "WellKnown" -and
                        -not $_.InterfaceAlias.Contains("Loopback") })

$localIp = "127.0.0.1"

if ($ips.Count -eq 0) {
    Write-Warn "Could not auto-detect local IP. Configure manually."
} elseif ($ips.Count -eq 1) {
    $localIp = $ips[0].IPAddress
    Write-Success "Local IP: $localIp"
} else {
    Write-Info "Multiple network interfaces detected. Choose one:"
    $ips | ForEach-Object -Begin { $i = 0 } -Process {
        $i++
        Write-Host "  [$i] $($_.IPAddress)  ($($_.InterfaceAlias))"
    }
    do {
        $choice = Read-Host "Enter number (default = 1)"
        if ([string]::IsNullOrWhiteSpace($choice)) { $choice = "1" }
    } until (($choice -as [int]) -gt 0 -and ($choice -as [int]) -le $ips.Count)
    $localIp = $ips[([int]$choice - 1)].IPAddress
    Write-Info "Selected: $localIp"
}

# ── 7. Write config.ini ───────────────────────────────────────────────────────

Write-Step "Writing configuration …"

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText("$INSTALL_DIR\config.ini", @"
[app]
listen_port = $LISTEN_PORT
auto_start  = true

[network]
discovery_port = 9876
"@, $utf8NoBom)

Write-Success "Configuration written to $INSTALL_DIR\config.ini"

# ── 8. Create Start Menu + Startup shortcuts ──────────────────────────────────

Write-Step "Creating shortcuts …"

$appScript     = Join-Path $INSTALL_DIR "src\main.py"
$shortcutDirs  = @(
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
)

foreach ($dir in $shortcutDirs) {
    try {
        $shortcutPath = Join-Path $dir "Bluetooth Bridge.lnk"
        if (-not (Test-Path $shortcutPath)) {
            $ws  = New-Object -ComObject WScript.Shell
            $lnk = $ws.CreateShortcut($shortcutPath)
            $lnk.TargetPath       = $pythonwExe
            $lnk.Arguments        = "-m src.main"
            $lnk.WorkingDirectory = $INSTALL_DIR
            $lnk.Description      = "Xbox Controller Bluetooth Bridge"
            $lnk.WindowStyle      = 7  # Minimised, no flash
            $lnk.IconLocation     = "$env:SystemRoot\System32\shell32.dll,19"
            $lnk.Save()
        }
    } catch {
        Write-Warn "Could not create shortcut in ${dir}: $_"
    }
}
Write-Success "Shortcuts created."

# ── 9. Launch app ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Starting Bluetooth Bridge …"

# Set env vars in the current session before launching; Start-Process inherits them.
$env:LISTEN_HOST = "0.0.0.0"
$env:LISTEN_PORT = "$LISTEN_PORT"
$env:LOG_LEVEL   = "INFO"

$proc = Start-Process `
    -FilePath $pythonwExe `
    -ArgumentList "-m src.main" `
    -WorkingDirectory $INSTALL_DIR `
    -PassThru

Start-Sleep 3

if ($proc.HasExited) {
    Write-Err "App exited immediately with code $($proc.ExitCode)."
    Write-Info "Check the log at: $INSTALL_DIR\bluetooth-bridge.log"
    exit 1
}

Write-Success "App started (PID: $($proc.Id))"

# ── 10. Done ──────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║            Bluetooth Bridge installed! ✓                   ║" -ForegroundColor Green
Write-Host "╚═══════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Info "Your Windows IP: $localIp"
Write-Info "App location:    $INSTALL_DIR"
Write-Info "Listen port:     $LISTEN_PORT"
Write-Host ""
Write-Host "  On your Linux machine, run:" -ForegroundColor Yellow
Write-Host "  curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash -s $localIp" -ForegroundColor Yellow
Write-Host ""
Write-Info "Logs:      $INSTALL_DIR\bluetooth-bridge.log"
Write-Info "Uninstall: delete $INSTALL_DIR and the Start Menu shortcut"
Write-Host ""