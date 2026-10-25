<#
.SYNOPSIS
    Bluetooth Bridge — Windows Installer

.DESCRIPTION
    Single-command install for the Windows side of the Bluetooth Bridge.

    Run from an elevated PowerShell:
        irm https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/windows/installer/install.ps1 | iex

.ENVIRONMENT
    Requires 64-bit Python 3.9+, internet access, and administrator privileges
    (for ViGEmBus driver installation).
#>

param(
    [switch]$Help,
    [switch]$NoLaunch,
    [switch]$NoBrowser,
    [switch]$NoStartup,
    [switch]$SkipBuild,
    [string]$LocalIp = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

# ── Constants ─────────────────────────────────────────────────────────────────

$VERSION       = "1.2.0"
$INSTALL_DIR   = "$env:USERPROFILE\bluetooth_bridge"
$VENV_DIR      = "$INSTALL_DIR\venv"
$LOG_DIR       = "$env:LOCALAPPDATA\bluetooth_bridge"
$LISTEN_PORT   = 9999
$WEBUI_PORT    = 0
$REPO_ZIP_URL  = "https://github.com/xynnpg/bluetooth-bridge/archive/refs/heads/main.zip"

# `irm | iex` cannot pass parameters, so support environment overrides too.
if ($env:BB_HELP -eq "1")          { $Help = $true }
if ($env:BB_NO_LAUNCH -eq "1")     { $NoLaunch = $true }
if ($env:BB_NO_BROWSER -eq "1")    { $NoBrowser = $true }
if ($env:BB_NO_STARTUP -eq "1")    { $NoStartup = $true }
if ($env:BB_SKIP_BUILD -eq "1")    { $SkipBuild = $true }
if ([string]::IsNullOrWhiteSpace($LocalIp) -and $env:BB_LOCAL_IP) { $LocalIp = $env:BB_LOCAL_IP }

if ($Help) {
    Write-Host ""
    Write-Host "  Bluetooth Bridge — Windows installer v$VERSION" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Installs the Windows receiver, ViGEmBus driver, firewall rules,"
    Write-Host "  virtual controller and tray app."
    Write-Host ""
    Write-Host "  Run PowerShell as Administrator. Settings in config.ini are kept"
    Write-Host "  when you re-run this installer."
    Write-Host ""
    Write-Host "  Usage:"
    Write-Host "    powershell -ExecutionPolicy Bypass -File install.ps1 [options]"
    Write-Host "    irm <installer-url> | iex"
    Write-Host ""
    Write-Host "  Options:"
    Write-Host "    -Help          Show this help"
    Write-Host "    -NoLaunch      Install but do not start the app"
    Write-Host "    -NoBrowser     Do not open the dashboard after install"
    Write-Host "    -NoStartup     Do not add the app to Windows startup"
    Write-Host "    -SkipBuild     Reuse an existing BluetoothBridge.exe"
    Write-Host "    -LocalIp IP    Use this address instead of prompting"
    Write-Host ""
    Write-Host "  Environment overrides for irm | iex:"
    Write-Host "    BB_NO_LAUNCH=1, BB_NO_BROWSER=1, BB_NO_STARTUP=1,"
    Write-Host "    BB_SKIP_BUILD=1, BB_LOCAL_IP=192.168.1.50,"
    Write-Host "    BB_NONINTERACTIVE=1"
    Write-Host ""
    exit 0
}

# Detect whether we are running from a saved .ps1 file or piped via irm | iex.
$isIrmMode = [string]::IsNullOrWhiteSpace($PSCommandPath)

# ── Colours ───────────────────────────────────────────────────────────────────

function Write-Step($msg)   { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Success($msg){ Write-Host "[✓] $msg" -ForegroundColor Green }
function Write-Warn($msg)   { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)    { Write-Host "[✗] $msg" -ForegroundColor Red }
function Write-Info($msg)   { Write-Host "    $msg" -ForegroundColor Gray }

function Get-IniValue {
    param([string]$SectionName, [string]$Key, $Default)
    $config = "$INSTALL_DIR\config.ini"
    if (-not (Test-Path $config)) { return $Default }
    $section = ""
    foreach ($line in (Get-Content $config -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*\[(.+?)\]\s*$') { $section = $Matches[1]; continue }
        if ($section -eq $SectionName -and $line -match "^\s*$Key\s*=\s*(.+?)\s*$") {
            return $Matches[1]
        }
    }
    return $Default
}

$effectiveListenPort = $LISTEN_PORT
$parsedPort = 0
if ([int]::TryParse([string](Get-IniValue "app" "listen_port" $LISTEN_PORT), [ref]$parsedPort) -and $parsedPort -gt 0 -and $parsedPort -le 65535) {
    $effectiveListenPort = $parsedPort
}

$effectiveDiscoveryPort = 9876
$parsedPort = 0
if ([int]::TryParse([string](Get-IniValue "network" "discovery_port" 9876), [ref]$parsedPort) -and $parsedPort -gt 0 -and $parsedPort -le 65535) {
    $effectiveDiscoveryPort = $parsedPort
}

Write-Host ""
Write-Host "  Bluetooth Bridge" -ForegroundColor Cyan
Write-Host "  Windows installer v$VERSION" -ForegroundColor DarkGray
Write-Host "  The Linux box reads your controller; this PC shows a virtual Xbox pad." -ForegroundColor DarkGray
Write-Host "  Keep PowerShell open; the installer will report each step." -ForegroundColor DarkGray
Write-Host ""

# ── Admin check ───────────────────────────────────────────────────────────────

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Err "This installer must be run as Administrator (needed for ViGEmBus)."
    Write-Info "Restart PowerShell as Admin and run again."
    exit 1
}

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Err "PowerShell 5.1 or newer is required."
    Write-Info "Update PowerShell, then re-run this installer."
    exit 1
}

# ── Preflight ─────────────────────────────────────────────────────────────────

Write-Step "Checking this PC"

if (-not [Environment]::Is64BitOperatingSystem) {
    Write-Err "A 64-bit version of Windows is required."
    Write-Info "ViGEmBus and the virtual Xbox controller are 64-bit only."
    exit 1
}

$existingInstall = Test-Path "$INSTALL_DIR\src\main.py"
if ($existingInstall) {
    Write-Info "Existing install found. This is an update; config.ini is kept."
} else {
    Write-Info "Fresh install."
}

function Stop-BridgeApp {
    $stopped = $false
    Get-Process -Name "BluetoothBridge" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            $stopped = $true
        }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like "*$INSTALL_DIR*src.main*"
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $stopped = $true
        }
    if ($stopped) {
        Write-Info "Stopped the running app so its files can be replaced."
    }
}

if ($existingInstall) {
    Stop-BridgeApp
}

# ── 1. Detect Python ──────────────────────────────────────────────────────────

Write-Step "Checking Python"

$pythonCmd = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $v = & $candidate --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match "Python (\d+)\.(\d+)") {
            $major, $minor = [int]$Matches[1], [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 9)) {
                $pythonCmd = $candidate
                Write-Success "Found $v"
                break
            }
        }
    } catch { }
}

if (-not $pythonCmd) {
    Write-Warn "Python 3.9+ was not found. Downloading the official 64-bit installer …"
    $pyUrl = "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe"
    $tmp = "$env:TEMP\python-setup.exe"
    try {
        Invoke-WebRequest -Uri $pyUrl -OutFile $tmp -TimeoutSec 120 -UseBasicParsing
        Start-Process -Wait -FilePath $tmp -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1"
        Remove-Item $tmp -Force

        # A fresh install may not be on this process's PATH yet.
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User")
        foreach ($candidate in @("python", "py")) {
            try {
                $v = & $candidate --version 2>$null
                if ($LASTEXITCODE -eq 0) { $pythonCmd = $candidate; break }
            } catch { }
        }
        if (-not $pythonCmd) {
            $pyExe = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" `
                -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
            if ($pyExe) { $pythonCmd = $pyExe.FullName }
        }
        if (-not $pythonCmd) { throw "Python was installed but could not be located." }
        Write-Success "Python installed."
    } catch {
        Write-Err "Python installation failed: $_"
        Write-Info "Install 64-bit Python 3.9+ from https://python.org, then re-run."
        exit 1
    }
}

try {
    $bits = (& $pythonCmd -c "import struct; print(struct.calcsize('P') * 8)" 2>$null).Trim()
    if ($bits -ne "64") {
        Write-Err "The detected Python is $bits-bit. A 64-bit Python is required."
        Write-Info "Install 64-bit Python 3.9+ and remove the 32-bit one from PATH."
        exit 1
    }
} catch {
    Write-Warn "Could not verify the Python architecture; continuing."
}

# ── 2. Check / Install ViGEmBus ───────────────────────────────────────────────

Write-Step "Checking the ViGEmBus driver"
Write-Info "ViGEmBus is the driver that creates the virtual Xbox controller."

$vigembInstalled = $null -ne (Get-Service -Name "ViGEmBus" -ErrorAction SilentlyContinue)

if (-not $vigembInstalled) {
    Write-Info "Not installed. Downloading the latest official release …"
    $releaseUrl = "https://api.github.com/repos/ViGEm/ViGEmBus/releases/latest"
    try {
        $json  = Invoke-RestMethod -Uri $releaseUrl -TimeoutSec 20
        $asset = $json.assets |
            Where-Object { $_.name -match '\.exe$' -and $_.name -match 'x64|amd64|x86_64' } |
            Select-Object -First 1
        if (-not $asset) {
            $asset = $json.assets | Where-Object { $_.name -match '\.exe$' } | Select-Object -First 1
        }
        if (-not $asset) { throw "No installer asset found in the latest release." }

        $vigEmbUrl = $asset.browser_download_url
        $tmp = "$env:TEMP\ViGEmBus_setup.exe"
        Invoke-WebRequest -Uri $vigEmbUrl -OutFile $tmp -TimeoutSec 180 -UseBasicParsing
        Start-Process -Wait -FilePath $tmp -ArgumentList "/quiet", "/norestart"
        Remove-Item $tmp -Force

        if ($null -ne (Get-Service -Name "ViGEmBus" -ErrorAction SilentlyContinue)) {
            Write-Success "ViGEmBus installed."
        } else {
            Write-Warn "The installer finished, but the ViGEmBus service was not found."
            Write-Info "Install it manually from https://github.com/ViGEm/ViGEmBus/releases"
            Write-Info "then re-run this installer."
        }
    } catch {
        Write-Warn "Could not install ViGEmBus automatically: $_"
        Write-Info "Download it manually from:"
        Write-Info "  https://github.com/ViGEm/ViGEmBus/releases"
        Write-Info "Then re-run this installer."
    }
} else {
    Write-Success "ViGEmBus already installed."
    $vigemService = Get-Service -Name "ViGEmBus" -ErrorAction SilentlyContinue
    if ($vigemService -and $vigemService.Status -ne "Running") {
        try {
            Start-Service -Name "ViGEmBus" -ErrorAction Stop
            Write-Success "ViGEmBus service started."
        } catch {
            Write-Warn "ViGEmBus is installed but not running. Start it from Services."
        }
    }
}

# ── 2b. Configure Windows Defender Firewall ──────────────────────────────────

Write-Step "Configuring Windows Defender Firewall"
Write-Info "Opening TCP 9999 for controller data and UDP 9876 for auto-discovery."

try {
    Remove-NetFirewallRule -DisplayName "Xbox Bluetooth Bridge" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Xbox Bluetooth Bridge Discovery" -ErrorAction SilentlyContinue


    New-NetFirewallRule -DisplayName "Xbox Bluetooth Bridge" `
                        -Direction Inbound `
                        -Action Allow `
                        -Protocol TCP `
                        -LocalPort $effectiveListenPort `
                        -Enabled True | Out-Null

    New-NetFirewallRule -DisplayName "Xbox Bluetooth Bridge Discovery" `
                        -Direction Inbound `
                        -Action Allow `
                        -Protocol UDP `
                        -LocalPort $effectiveDiscoveryPort `
                        -Enabled True | Out-Null

    Write-Success "Firewall inbound rules added (TCP $effectiveListenPort, UDP $effectiveDiscoveryPort)."
} catch {
    Write-Warn "Could not auto-configure Firewall: $_"
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
        # The old sources are removed first: Copy-Item -Recurse copies a
        # directory *into* an existing one, which would nest the new files
        # in src\src\ on a re-install and break the imports.
        if (Test-Path "$INSTALL_DIR\src") {
            Remove-Item "$INSTALL_DIR\src" -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path "$INSTALL_DIR\src" | Out-Null
        Copy-Item -Recurse "$localAppSrc\*" "$INSTALL_DIR\src\" -Force
        if (Test-Path "$INSTALL_DIR\src\__pycache__") {
            Remove-Item "$INSTALL_DIR\src\__pycache__" -Recurse -Force
        }
        if (Test-Path $localReq) {
            Copy-Item $localReq "$INSTALL_DIR\" -Force
        }
        $localTools = Join-Path $repoRoot "tools"
        if (Test-Path $localTools) {
            if (Test-Path "$INSTALL_DIR\tools") {
                Remove-Item "$INSTALL_DIR\tools" -Recurse -Force
            }
            New-Item -ItemType Directory -Force -Path "$INSTALL_DIR\tools" | Out-Null
            Copy-Item -Recurse "$localTools\*" "$INSTALL_DIR\tools\" -Force
        }
        $localUninstall = Join-Path $repoRoot "windows\installer\uninstall.ps1"
        if (Test-Path $localUninstall) {
            Copy-Item $localUninstall "$INSTALL_DIR\uninstall.ps1" -Force
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
        Invoke-WebRequest -Uri $REPO_ZIP_URL -OutFile $tmpZip -TimeoutSec 60 -UseBasicParsing
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

        # Remove the old sources first — Copy-Item -Recurse copies a
        # directory *into* an existing one, which would nest the new files
        # in src\src\ on a re-install and break the imports.
        if (Test-Path "$INSTALL_DIR\src") {
            Remove-Item "$INSTALL_DIR\src" -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path "$INSTALL_DIR\src" | Out-Null
        Copy-Item -Recurse "$winSrc\*" "$INSTALL_DIR\src\" -Force
        if (Test-Path "$INSTALL_DIR\src\__pycache__") {
            Remove-Item "$INSTALL_DIR\src\__pycache__" -Recurse -Force
        }
        if (Test-Path $winReq) {
            Copy-Item $winReq "$INSTALL_DIR\" -Force
        }

        # The diagnostic collector (merges the Windows and the Linux trace).
        $winTools = Join-Path $extractedRoot.FullName "tools"
        if (Test-Path $winTools) {
            if (Test-Path "$INSTALL_DIR\tools") {
                Remove-Item "$INSTALL_DIR\tools" -Recurse -Force
            }
            New-Item -ItemType Directory -Force -Path "$INSTALL_DIR\tools" | Out-Null
            Copy-Item -Recurse "$winTools\*" "$INSTALL_DIR\tools\" -Force
        }

        $winUninstall = Join-Path $extractedRoot.FullName "windows\installer\uninstall.ps1"
        if (Test-Path $winUninstall) {
            Copy-Item $winUninstall "$INSTALL_DIR\uninstall.ps1" -Force
        }

        Remove-Item $tmpDir -Recurse -Force
        Write-Success "Source downloaded and deployed."
    } catch {
        Write-Err "Failed to download app source: $_"
        exit 1
    }
}

# A stable entry point for the standalone exe.  Running src\main.py directly
# makes PyInstaller treat it as a top-level script, where the package's
# relative imports can fail.  Importing src.main as a package avoids that.
$runPy = "$INSTALL_DIR\run.py"
@'
from src.main import main

if __name__ == "__main__":
    main()
'@ | Set-Content -Path $runPy -Encoding UTF8

# ── 5. Install Python dependencies ────────────────────────────────────────────

Write-Step "Installing Python dependencies"
Write-Info "Flask, the virtual-pad binding, tray support and Pillow."

if (Test-Path $VENV_DIR) {
    Write-Info "Reusing the existing virtual environment."
} else {
    & $pythonCmd -m venv $VENV_DIR
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Could not create the Python virtual environment."
        Write-Info "Check that Python is installed correctly, then re-run."
        exit 1
    }
    Write-Success "Virtual environment created."
}

$pythonExe  = Join-Path $VENV_DIR "Scripts\python.exe"    # pip / venv ops

# Prefer the venv's pythonw. If it is missing (some headless Python builds),
# use the venv's python.exe so the app still sees its installed dependencies.
# The app hides its own console window on Windows.
$pythonwExe = Join-Path $VENV_DIR "Scripts\pythonw.exe"
if (-not (Test-Path $pythonwExe)) {
    Write-Warn "pythonw.exe is missing from the venv; using the venv python.exe."
    $pythonwExe = $pythonExe
}

# Upgrade pip silently
& $pythonExe -m pip install --upgrade pip --quiet 2>$null | Out-Null

$reqFile = Join-Path $INSTALL_DIR "requirements.txt"
if (Test-Path $reqFile) {
    & $pythonExe -m pip install --upgrade -r $reqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "pip install failed with version pins — retrying without pins …"
        $content = (Get-Content $reqFile) -replace ">=\d+(\.\d+)*", ""
        $tmpReq  = "$env:TEMP\requirements_fallback.txt"
        $content | Set-Content $tmpReq
        & $pythonExe -m pip install --upgrade -r $tmpReq
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

Write-Step "Finding this PC's LAN address"

$localIp = $LocalIp
if (-not $localIp) {
    $candidates = @()
    try {
        foreach ($cfg in (Get-NetIPConfiguration | Where-Object {
                    $_.NetAdapter.Status -eq "Up" -and $_.IPv4Address
                })) {
            $alias = [string]$cfg.InterfaceAlias
            if ($alias -match "Loopback|vEthernet|WSL|Docker|Hyper-V|VMware|VirtualBox|Tailscale|ZeroTier") {
                continue
            }
            foreach ($addr in @($cfg.IPv4Address)) {
                $ip = [string]$addr.IPAddress
                if ($ip -like "127.*" -or $ip -like "169.254.*") { continue }
                $candidates += [pscustomobject]@{
                    IP      = $ip
                    Alias   = $alias
                    Default = ($null -ne $cfg.IPv4DefaultGateway)
                }
            }
        }
    } catch {
        Write-Warn "Could not enumerate network interfaces: $_"
    }

    $candidates = @($candidates | Sort-Object -Property Default -Descending)
    if ($candidates.Count -eq 0) {
        $localIp = "127.0.0.1"
        Write-Warn "No LAN address found. Set it later in config.ini."
    } elseif ($candidates.Count -eq 1) {
        $localIp = $candidates[0].IP
        Write-Success "LAN address: $localIp ($($candidates[0].Alias))"
    } else {
        Write-Info "This PC has more than one address. Choose the Wi-Fi/Ethernet"
        Write-Info "address that your Linux machine can reach:"
        $candidates | ForEach-Object -Begin { $i = 0 } -Process {
            $i++
            $mark = if ($_.Default) { "  recommended" } else { "" }
            Write-Host "  [$i] $($_.IP)  ($($_.Alias))$mark"
        }
        $nonInteractive = ($env:BB_NONINTERACTIVE -eq "1")
        if ($nonInteractive) {
            $choice = "1"
            Write-Info "Non-interactive mode: using the first address."
        } else {
            do {
                $choice = Read-Host "Enter number (default = 1)"
                if ([string]::IsNullOrWhiteSpace($choice)) { $choice = "1" }
            } until (($choice -as [int]) -gt 0 -and ($choice -as [int]) -le $candidates.Count)
        }
        $localIp = $candidates[([int]$choice - 1)].IP
        Write-Info "Selected: $localIp"
    }
} else {
    Write-Success "Using the address from -LocalIp: $localIp"
}

if ($localIp -ne "127.0.0.1" -and -not (Test-Connection -ComputerName $localIp -Count 1 -Quiet -ErrorAction SilentlyContinue)) {
    Write-Warn "The selected address did not respond to a local ping; continuing anyway."
}

# ── 6. Build Standalone GUI Executable ────────────────────────────────────────

$exePath = "$INSTALL_DIR\BluetoothBridge.exe"

if ($SkipBuild -and (Test-Path $exePath)) {
    Write-Step "Keeping the existing desktop app"
    Write-Info "Skipped the rebuild because -SkipBuild / BB_SKIP_BUILD=1 was set."
} elseif ($SkipBuild) {
    Write-Warn "-SkipBuild was set but no BluetoothBridge.exe exists."
    Write-Info "Falling back to the Python launcher."
} else {
    Write-Step "Building the desktop app"
    Write-Info "This can take 1–3 minutes. A log is kept in $env:TEMP."

    $pyinstallerExe = Join-Path $VENV_DIR "Scripts\pyinstaller.exe"
    if (-not (Test-Path $pyinstallerExe)) {
        Write-Info "Installing PyInstaller …"
        & $pythonExe -m pip install pyinstaller --quiet 2>$null | Out-Null
    }

    $buildLog = "$env:TEMP\bluetooth-bridge-build.log"
    & $pythonExe -m PyInstaller `
        --noconfirm `
        --noconsole `
        --onefile `
        --name BluetoothBridge `
        --distpath $INSTALL_DIR `
        --workpath "$env:TEMP\build_bb" `
        --specpath "$env:TEMP\spec_bb" `
        --collect-all vgamepad `
        --collect-all pystray `
        --collect-all PIL `
        --collect-all flask `
        --collect-submodules src `
        --hidden-import waitress `
        --paths "$INSTALL_DIR" `
        "$INSTALL_DIR\run.py" *> $buildLog

    if ($LASTEXITCODE -eq 0 -and (Test-Path $exePath) -and ((Get-Item $exePath).Length -gt 0)) {
            Write-Success "Built $exePath"
    } else {
        Write-Warn "The packaged exe could not be built. The app will use the Python launcher."
        Write-Info "Build log: $buildLog"
        if (Test-Path $buildLog) {
            Get-Content $buildLog -Tail 12 | ForEach-Object { Write-Info "  $_" }
        }
        if (Test-Path $exePath) {
            Remove-Item $exePath -Force -ErrorAction SilentlyContinue
            Write-Info "Removed the stale exe so the Python launcher is used."
        }
    }
}

# ── 7. Write config.ini & launch.vbs ──────────────────────────────────────────

Write-Step "Writing configuration …"

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# The settings of the running installation are kept: a re-install must not
# reset the web UI port, the Linux management token or anything else.
$existing = @{}
if (Test-Path "$INSTALL_DIR\config.ini") {
    Get-Content "$INSTALL_DIR\config.ini" -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([^#;\[][^=]*?)\s*=\s*(.*?)\s*$') {
            $existing[$Matches[1].Trim()] = $Matches[2].Trim()
        }
    }
    Write-Info "Keeping the existing settings from config.ini"
}

function Get-Setting($key, $fallback) {
    if ($existing.ContainsKey($key) -and $existing[$key] -ne "") {
        return $existing[$key]
    }
    return $fallback
}

$cfg = @"
[app]
listen_port   = $(Get-Setting "listen_port"   $LISTEN_PORT)
listen_host   = $(Get-Setting "listen_host"   "0.0.0.0")
auto_discover = $(Get-Setting "auto_discover" "true")
auto_start    = $(Get-Setting "auto_start"    "true")
log_level     = $(Get-Setting "log_level"     "INFO")

[webui]
enabled         = $(Get-Setting "enabled"         "true")
host            = $(Get-Setting "host"            "127.0.0.1")
port            = $(Get-Setting "port"            $WEBUI_PORT)
open_on_launch  = $(Get-Setting "open_on_launch"  "false")
auto_refresh_ms = $(Get-Setting "auto_refresh_ms" "750")

[controller]
rumble_enabled                = $(Get-Setting "rumble_enabled"              "true")
low_battery_warn_pct          = $(Get-Setting "low_battery_warn_pct"        "15")
deadzone_stick                = $(Get-Setting "deadzone_stick"              "15")
deadzone_trigger              = $(Get-Setting "deadzone_trigger"            "1")
invert_left_y                 = $(Get-Setting "invert_left_y"               "false")
invert_right_y                = $(Get-Setting "invert_right_y"              "false")
battery_display_enabled       = $(Get-Setting "battery_display_enabled"    "true")
performance_warning_enabled   = $(Get-Setting "performance_warning_enabled" "true")
performance_warning_ms        = $(Get-Setting "performance_warning_ms"     "180")

[tray]
show_battery = $(Get-Setting "show_battery" "true")

[network]
discovery_port       = $(Get-Setting "discovery_port"      "9876")
keepalive_timeout_s  = $(Get-Setting "keepalive_timeout_s" "6.0")

[linux]
management_port = $(Get-Setting "management_port" "9988")
management_token = $(Get-Setting "management_token" "")
"@

[System.IO.File]::WriteAllText("$INSTALL_DIR\config.ini", $cfg, $utf8NoBom)

# Fallback VBS launcher for systems without a packaged exe, and for the
# Start Menu / Startup shortcuts when the exe build failed.
$vbsPath = "$INSTALL_DIR\launch.vbs"
if (Test-Path $exePath) {
    $vbsCommand = '""' + $exePath + '""'
} else {
    $vbsCommand = '""' + $pythonwExe + '"" -m src.main'
}
$vbsContent = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "$INSTALL_DIR"
WshShell.Run "$vbsCommand", 0, False
"@
[System.IO.File]::WriteAllText($vbsPath, $vbsContent, $utf8NoBom)

Write-Success "Configuration written to $INSTALL_DIR\config.ini"
Write-Info "Settings are preserved on re-install."

# ── 8. Create Start Menu + Startup shortcuts ──────────────────────────────────

Write-Step "Creating shortcuts"

$startMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$startupDir   = "$startMenuDir\Startup"
New-Item -ItemType Directory -Force -Path $startMenuDir, $startupDir | Out-Null

if (Test-Path $exePath) {
    $shortcutTarget = $exePath
    $shortcutArgs   = ""
} else {
    $shortcutTarget = "$env:SystemRoot\System32\wscript.exe"
    $shortcutArgs   = "`"$vbsPath`""
}

function New-BridgeShortcut {
    param(
        [string]$Path,
        [string]$Target,
        [string]$Arguments,
        [string]$Description,
        [int]$WindowStyle,
        [string]$Icon
    )
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $INSTALL_DIR
    $lnk.Description = $Description
    $lnk.WindowStyle = $WindowStyle
    if ($Icon) { $lnk.IconLocation = $Icon }
    $lnk.Save()
}

try {
    New-BridgeShortcut `
        -Path "$startMenuDir\Bluetooth Bridge.lnk" `
        -Target $shortcutTarget `
        -Arguments $shortcutArgs `
        -Description "Xbox controller bridge" `
        -WindowStyle 1 `
        -Icon "$env:SystemRoot\System32\shell32.dll,19"
    Write-Success "Start Menu shortcut created."
} catch {
    Write-Warn "Could not create the Start Menu shortcut: $_"
}

if (-not $NoStartup) {
    try {
        New-BridgeShortcut `
            -Path "$startupDir\Bluetooth Bridge.lnk" `
            -Target $shortcutTarget `
            -Arguments $shortcutArgs `
            -Description "Start the Xbox controller bridge with Windows" `
            -WindowStyle 7 `
            -Icon "$env:SystemRoot\System32\shell32.dll,19"
        Write-Success "Startup shortcut created (the tray app starts with Windows)."
    } catch {
        Write-Warn "Could not create the startup shortcut: $_"
    }
} else {
    Remove-Item "$startupDir\Bluetooth Bridge.lnk" -Force -ErrorAction SilentlyContinue
    Write-Info "Skipped the startup shortcut (-NoStartup / BB_NO_STARTUP=1)."
}

# A real uninstall entry beats "delete this folder" instructions.
$uninstallScript = "$INSTALL_DIR\uninstall.ps1"
if (Test-Path $uninstallScript) {
    try {
        New-BridgeShortcut `
            -Path "$startMenuDir\Uninstall Bluetooth Bridge.lnk" `
            -Target "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -Arguments "-NoProfile -ExecutionPolicy Bypass -File `"$uninstallScript`"" `
            -Description "Remove Bluetooth Bridge" `
            -WindowStyle 1 `
            -Icon "$env:SystemRoot\System32\shell32.dll,27"
        Write-Success "Uninstall shortcut created."
    } catch {
        Write-Warn "Could not create the uninstall shortcut: $_"
    }
}

# ── 9. Launch app ─────────────────────────────────────────────────────────────

function Get-WebUiPort {
    $section = ""
    $config = "$INSTALL_DIR\config.ini"
    if (-not (Test-Path $config)) { return 0 }
    foreach ($line in (Get-Content $config -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*\[(.+?)\]\s*$') { $section = $Matches[1]; continue }
        if ($section -eq "webui" -and $line -match '^\s*port\s*=\s*(\d+)') {
            return [int]$Matches[1]
        }
    }
    return 0
}

$webPort = 0
if ($NoLaunch) {
    Write-Host ""
    Write-Step "App installed, not started"
    Write-Info "Start it from the Start Menu shortcut 'Bluetooth Bridge'."
    $webPort = Get-WebUiPort
} else {
    Write-Host ""
    Write-Step "Starting Bluetooth Bridge"
    Stop-BridgeApp

    if (Test-Path $exePath) {
        $proc = Start-Process -FilePath $exePath `
            -WorkingDirectory $INSTALL_DIR -PassThru
    } else {
        $proc = Start-Process -FilePath $pythonwExe `
            -ArgumentList "-m", "src.main" `
            -WorkingDirectory $INSTALL_DIR -PassThru
    }

    Start-Sleep 3
    if ($proc.HasExited) {
        Write-Err "The app exited immediately with code $($proc.ExitCode)."
        Write-Info "Check the log at: $LOG_DIR\bluetooth-bridge.log"
        exit 1
    }
    Write-Success "App started (PID: $($proc.Id))."

    # The dashboard port is chosen at first launch and written to config.ini.
    for ($i = 0; $i -lt 30; $i++) {
        $webPort = Get-WebUiPort
        if ($webPort -gt 0) { break }
        Start-Sleep -Milliseconds 500
    }
}

# A dashboard shortcut is much easier than remembering a random port.
$dashboardUrl = ""
if ($webPort -gt 0) {
    $dashboardUrl = "http://127.0.0.1:$webPort/"
    try {
        $urlPath = "$startMenuDir\Bluetooth Bridge Dashboard.url"
        $urlContent = "[InternetShortcut]`r`nURL=$dashboardUrl`r`nIconFile=$env:SystemRoot\System32\shell32.dll`r`nIconIndex=13`r`n"
        [System.IO.File]::WriteAllText($urlPath, $urlContent, [System.Text.Encoding]::ASCII)
        Write-Info "Dashboard: $dashboardUrl"
    } catch {
        Write-Warn "Could not create the dashboard shortcut: $_"
    }
} else {
    Write-Info "Dashboard port not assigned yet; use the tray icon's 'Open Web UI'."
}

$appOpensBrowser = $false
if (Test-Path "$INSTALL_DIR\config.ini") {
    $appOpensBrowser = [bool](Select-String -Path "$INSTALL_DIR\config.ini" `
        -Pattern '^\s*open_on_launch\s*=\s*true' -Quiet -ErrorAction SilentlyContinue)
}

if (-not $NoBrowser -and -not $NoLaunch -and $dashboardUrl -and -not $appOpensBrowser) {
    try {
        Start-Process $dashboardUrl
        Write-Info "Opened the dashboard in your browser."
    } catch {
        Write-Info "Open the dashboard from the tray icon or the new Start Menu shortcut."
    }
} elseif ($appOpensBrowser) {
    Write-Info "The app is configured to open the dashboard on launch."
}

# ── 10. Done ──────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  Bluetooth Bridge is installed." -ForegroundColor Green
Write-Host ""
Write-Host "  App          $INSTALL_DIR" -ForegroundColor Gray
Write-Host "  Windows IP   $localIp" -ForegroundColor Gray
Write-Host "  Listen       TCP $effectiveListenPort" -ForegroundColor Gray
if ($dashboardUrl) {
    Write-Host "  Dashboard    $dashboardUrl" -ForegroundColor Gray
}
Write-Host "  Log          $LOG_DIR\bluetooth-bridge.log" -ForegroundColor Gray
Write-Host "  Input trace  $LOG_DIR\input-trace.log" -ForegroundColor Gray
Write-Host ""
Write-Host "  Next steps" -ForegroundColor White
Write-Host "    1. Keep the app running (the tray icon is the on/off switch)."
Write-Host "    2. On the Linux machine, run:"
$linuxPortArg = if ($effectiveListenPort -ne 9999) { " --windows-port $effectiveListenPort" } else { "" }
Write-Host "       curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash -s -- --windows-ip $localIp$linuxPortArg"
Write-Host "    3. Turn on the controller and press the Xbox button."
Write-Host "    4. Open the dashboard from the tray icon to check battery, input"
Write-Host "       and rumble (Test motors / Stop)."
Write-Host ""
Write-Host "  Updating:   run the installer again; config.ini is preserved." -ForegroundColor Gray
if (Test-Path "$INSTALL_DIR\uninstall.ps1") {
    Write-Host "  Uninstall:  Start Menu → Uninstall Bluetooth Bridge" -ForegroundColor Gray
} else {
    Write-Host "  Uninstall:  remove $INSTALL_DIR and the Start Menu shortcut" -ForegroundColor Gray
}
Write-Host "  Settings:   $INSTALL_DIR\config.ini" -ForegroundColor Gray
Write-Host ""

$vigemReady = $null -ne (Get-Service -Name "ViGEmBus" -ErrorAction SilentlyContinue)
if (-not $vigemReady) {
    Write-Host "  ACTION REQUIRED" -ForegroundColor Yellow
    Write-Host "  ViGEmBus is not installed, so the virtual controller cannot be created." -ForegroundColor Yellow
    Write-Host "  Install it from https://github.com/ViGEm/ViGEmBus/releases, then restart the app." -ForegroundColor Yellow
    Write-Host ""
}