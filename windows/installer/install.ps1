#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Bluetooth Bridge — Windows Installer

.DESCRIPTION
    Single-command install for the Windows side of the Bluetooth Bridge.

    Run from an elevated PowerShell:
        irm https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/windows/installer/install.ps1 | iex

    Replace 'user' with your GitHub username before running.

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
$DISCOVERY_URL = "https://github.com/xynnpg/bluetooth-bridge"  
$REPO_BASE     = "https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/windows"

# ── Colours ───────────────────────────────────────────────────────────────────

function Write-Step($msg)  { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Success($msg){ Write-Host "[✓] $msg" -ForegroundColor Green }
function Write-Warn($msg)   { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "[✗] $msg" -ForegroundColor Red }
function Write-Info($msg)  { Write-Host "    $msg" -ForegroundColor Gray }

# ── Banner ─────────────────────────────────────────────────────────────────────

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
    Write-Info "Restart PowerShell as Admin and run:"
    Write-Info "  irm $REPO_BASE/installer/install.ps1 | iex"
    exit 1
}

# ── 1. Detect Python ───────────────────────────────────────────────────────────

Write-Step "Checking Python installation …"

$pythonCmd = $null
foreach ($candidate in @("python", "python3", "py")) {
    $v = & $candidate --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $v -match "Python (\d+)\.(\d+)") {
        $major, $minor = [int]$Matches[1], [int]$Matches[2]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 8)) {
            $pythonCmd = $candidate
            Write-Success "Found: $v"
            break
        }
    }
}

if (-not $pythonCmd) {
    Write-Warn "Python 3.8+ not found. Downloading from python.org …"
    $pyUrl = "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe"
    $tmp = "$env:TEMP\python-setup.exe"
    try {
        Invoke-WebRequest -Uri $pyUrl -OutFile $tmp -TimeoutSec 60
        Start-Process -Wait -FilePath $tmp -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1"
        Remove-Item $tmp -Force
        $pythonCmd = "python"
        Write-Success "Python installed."
    } catch {
        Write-Err "Failed to install Python. Please install it manually from https://python.org and re-run this installer."
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
        $json = Invoke-RestMethod -Uri $releaseUrl -TimeoutSec 15
        $asset = $json.assets | Where-Object { $_.name -like "*x86*64*.exe" } | Select-Object -First 1
        if (-not $asset) { $asset = $json.assets | Select-Object -First 1 }
        $vigEmbUrl = $asset.browser_download_url
        $tmp = "$env:TEMP\ViGEmBus_setup.exe"
        Invoke-WebRequest -Uri $vigEmbUrl -OutFile $tmp -TimeoutSec 120
        Start-Process -Wait -FilePath $tmp -ArgumentList "/quiet", "/norestart"
        Remove-Item $tmp -Force
        Write-Success "ViGEmBus installed."
    } catch {
        Write-Warn "Could not auto-download ViGEmBus. Download it manually from:"
        Write-Info "  https://github.com/ViGEm/ViGEmBus/releases"
        Write-Info "Then re-run this installer."
        # Don't abort — the app will show a clear error message at runtime
    }
} else {
    Write-Success "ViGEmBus already installed."
}

# ── 3. Create install directory ─────────────────────────────────────────────────

Write-Step "Installing to $INSTALL_DIR …"

New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null

# ── 4. Copy app source files ────────────────────────────────────────────────

Write-Step "Deploying application files …"

# Files are deployed by IRM directly; during development we use local paths
$srcDir = Split-Path -Parent $PSCommandPath
if ($srcDir -match "temp") {
    # Running from IRM — files come from the downloaded install.ps1 directory
    # We embed a minimal inline deployment approach here:
    Write-Info "Source: $srcDir"
}

# Copy the windows/src/ directory (relative to repo root)
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$appSrc   = Join-Path $repoRoot "windows\src"
$requirements = Join-Path $repoRoot "windows\requirements.txt"

if (Test-Path $appSrc) {
    Copy-Item -Recurse $appSrc $INSTALL_DIR\src\ -Force
} else {
    # If running from a temp location, download source via GitHub
    Write-Info "Downloading source from GitHub …"
    $zipUrl = "$DISCOVERY_URL/../archive/refs/heads/main.zip"
    $tmpZip  = "$env:TEMP\bluetooth-bridge.zip"
    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $tmpZip -TimeoutSec 30
        Expand-Archive -Path $tmpZip -DestinationPath $env:TEMP -Force
        $extractDir = Get-ChildItem $env:TEMP -Filter "bluetooth-bridge-main" -Directory | Select-Object -First 1
        if ($extractDir) {
            Copy-Item "$($extractDir.FullName)\windows\src" $INSTALL_DIR\src\ -Recurse -Force
            Copy-Item "$($extractDir.FullName)\windows\requirements.txt" $INSTALL_DIR\ -Force
            Remove-Item $tmpZip -Force
            Remove-Item $extractDir.FullName -Recurse -Force
        }
    } catch {
        Write-Err "Failed to download app source: $_"
        exit 1
    }
}

# Copy requirements.txt if not already
if (-not (Test-Path (Join-Path $INSTALL_DIR "requirements.txt")) -and (Test-Path $requirements)) {
    Copy-Item $requirements $INSTALL_DIR\ -Force
}

# ── 5. Install Python dependencies ────────────────────────────────────────────

Write-Step "Installing Python dependencies …"

# Use a venv for isolation
if (Test-Path $VENV_DIR) {
    Write-Info "Reusing existing venv."
} else {
    & $pythonCmd -m venv $VENV_DIR
    Write-Success "Virtual environment created."
}

$pip   = Join-Path $VENV_DIR "Scripts\pip.exe"
$python = Join-Path $VENV_DIR "Scripts\python.exe"

# Upgrade pip first
& $python -m pip install --upgrade pip --quiet 2>$null | Out-Null

# Install requirements
$reqFile = Join-Path $INSTALL_DIR "requirements.txt"
if (Test-Path $reqFile) {
    & $pip install --upgrade -r $reqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Err "pip install failed. Trying without version pins …"
        # Fallback: strip version specs and retry
        $content = Get-Content $reqFile
        $content = $content -replace ">=\d+(\.\d+)*", ""
        $tmpReq  = "$env:TEMP\requirements_fallback.txt"
        $content | Set-Content $tmpReq
        & $pip install --upgrade -r $tmpReq
        Remove-Item $tmpReq -Force
        if ($LASTEXITCODE -ne 0) { Write-Warn "Some pip packages may not have installed correctly." }
    }
}

# ── 6. Detect local IP address ─────────────────────────────────────────────────

Write-Step "Detecting local IP address …"

$ips = Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -ne "127.0.0.1" -and -not $_.InterfaceAlias.Contains("Loopback") }

if ($ips.Count -eq 0) {
    Write-Warn "Could not auto-detect local IP. The Linux side may need manual configuration."
    $localIp = "127.0.0.1"
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
        if ([string]::IsNullOrWhiteSpace($choice)) { $choice = 1; break }
    } until ($choice -as [int] -gt 0 -and $choice -as [int] -le $ips.Count)
    $localIp = $ips[($choice - 1)].IPAddress
    Write-Info "Selected: $localIp"
}

# ── 7. Write config.ini ───────────────────────────────────────────────────────

Write-Step "Writing configuration …"

$config = @"
[app]
listen_port = $LISTEN_PORT
auto_start  = true

[network]
discovery_port = 9876
"@

Set-Content -Path "$INSTALL_DIR\config.ini" -Value $config -Encoding UTF8
Write-Success "Configuration written to $INSTALL_DIR\config.ini"

# ── 8. Register startup shortcut ──────────────────────────────────────────────

Write-Step "Registering startup shortcut …"

$startupDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$shortcutPath = Join-Path $startupDir "Bluetooth Bridge.lnk"
$appExe = Join-Path $VENV_DIR "Scripts\python.exe"
$appScript = Join-Path $INSTALL_DIR "src\main.py"

try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($shortcutPath)
    $lnk.TargetPath        = $appExe
    $lnk.Arguments         = "-m src.main"
    $lnk.WorkingDirectory  = $INSTALL_DIR
    $lnk.Description        = "Xbox Controller Bluetooth Bridge"
    $lnk.WindowStyle        = 4  # Minimized
    $lnk.IconLocation       = "$env:SystemRoot\System32\shell32.dll,19"
    $lnk.Save()
    Write-Success "Startup shortcut created."
} catch {
    Write-Warn "Could not create startup shortcut: $_"
}

# ── 9. Start Menu shortcut ────────────────────────────────────────────────────

Write-Step "Creating Start Menu shortcut …"

$startMenuDir = "$startupDir"
$startMenuShortcut = Join-Path $startMenuDir "Bluetooth Bridge.lnk"

try {
    if (-not (Test-Path $startMenuShortcut)) {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($startMenuShortcut)
        $lnk.TargetPath       = $appExe
        $lnk.Arguments        = "-m src.main"
        $lnk.WorkingDirectory = $INSTALL_DIR
        $lnk.Description       = "Xbox Controller Bluetooth Bridge"
        $lnk.WindowStyle       = 1  # Normal
        $lnk.Save()
    }
    Write-Success "Start Menu shortcut created."
} catch {
    Write-Warn "Could not create Start Menu shortcut: $_"
}

# ── 10. Register uninstaller ───────────────────────────────────────────────────

$uninstallScript = Join-Path $PSScriptRoot "uninstall.ps1"
if (-not (Test-Path $uninstallScript) -and $PSCommandPath -notmatch "temp") {
    $uninstallScript = Join-Path $INSTALL_DIR "uninstall.ps1"
}

# ── 11. Launch app ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Starting Bluetooth Bridge …"

$env:LISTEN_HOST = "0.0.0.0"
$env:LISTEN_PORT = $LISTEN_PORT

$proc = Start-Process -FilePath $appExe -ArgumentList "-m src.main" `
          -WorkingDirectory $INSTALL_DIR `
          -EnvironmentVariables `
            LISTEN_HOST="0.0.0.0",
            LISTEN_PORT="$LISTEN_PORT",
            LOG_LEVEL="INFO" `
          -PassThru -WindowStyle Minimized

Start-Sleep 3

if ($proc.HasExited) {
    Write-Err "App exited immediately with code $($proc.ExitCode)."
    Write-Info "Check the log file: $INSTALL_DIR\bluetooth-bridge.log"
    exit 1
}

Write-Success "App started (PID: $($proc.Id))"

# ── 12. Done ────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║            Bluetooth Bridge installed! ✓                   ║" -ForegroundColor Green
Write-Host "╚═══════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Info "Your Windows IP: $localIp"
Write-Info "App location:    $INSTALL_DIR"
Write-Info "Listen port:     $LISTEN_PORT"
Write-Host ""
Write-Host "  On your Linux server, run:" -ForegroundColor Yellow
Write-Host "  curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash -s $localIp" -ForegroundColor Yellow
Write-Host ""
Write-Info "Check logs:  $INSTALL_DIR\bluetooth-bridge.log"
Write-Info "Uninstall:   Start Menu → Bluetooth Bridge → Uninstall"
Write-Host ""