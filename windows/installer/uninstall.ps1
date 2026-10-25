<#
.SYNOPSIS
    Remove the Bluetooth Bridge Windows app.

.DESCRIPTION
    Stops the app, removes its firewall rules, shortcuts and install folder.
    ViGEmBus and Python are shared system components and are intentionally
    left installed.

    Run from the Start Menu shortcut "Uninstall Bluetooth Bridge", or:
        powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1
#>

param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$INSTALL_DIR = $PSScriptRoot
$LOG_DIR     = "$env:LOCALAPPDATA\bluetooth_bridge"
$START_MENU  = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$STARTUP     = "$START_MENU\Startup"

Write-Host ""
Write-Host "  Bluetooth Bridge — uninstall" -ForegroundColor Cyan
Write-Host "  This removes the app, logs, config and shortcuts from this PC." -ForegroundColor Gray
Write-Host "  ViGEmBus and Python are left installed." -ForegroundColor Gray
Write-Host ""

if (-not $Force) {
    $answer = Read-Host "Type 'yes' to remove $INSTALL_DIR"
    if ($answer -ne "yes") {
        Write-Host "Cancelled." -ForegroundColor Yellow
        exit 0
    }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "[*] Requesting administrator rights for the firewall cleanup …"
    try {
        Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -Verb RunAs `
            -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", "`"$PSCommandPath`"", "-Force"
            )
        exit 0
    } catch {
        Write-Host "[!] Continuing without administrator rights." -ForegroundColor Yellow
        Write-Host "    Firewall rules will be left in place." -ForegroundColor Yellow
    }
}

Write-Host "[*] Stopping the running app …"
Get-Process -Name "BluetoothBridge" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -like "*$INSTALL_DIR*src.main*"
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

if ($isAdmin) {
    Write-Host "[*] Removing firewall rules …"
    Remove-NetFirewallRule -DisplayName "Xbox Bluetooth Bridge" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Xbox Bluetooth Bridge Discovery" -ErrorAction SilentlyContinue
}

Write-Host "[*] Removing shortcuts …"
$shortcuts = @(
    "$START_MENU\Bluetooth Bridge.lnk",
    "$START_MENU\Bluetooth Bridge Dashboard.url",
    "$START_MENU\Uninstall Bluetooth Bridge.lnk",
    "$STARTUP\Bluetooth Bridge.lnk"
)
foreach ($path in $shortcuts) {
    Remove-Item $path -Force -ErrorAction SilentlyContinue
}

Write-Host "[*] Removing logs …"
if (Test-Path $LOG_DIR) {
    Remove-Item $LOG_DIR -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "[*] Removing files …"
Set-Location $env:TEMP
Remove-Item $INSTALL_DIR -Recurse -Force -ErrorAction SilentlyContinue

if (Test-Path $INSTALL_DIR) {
    Write-Host "[!] Some files are still in use. Retrying with elevated file ownership …" -ForegroundColor Yellow
    & takeown /F $INSTALL_DIR /R /D Y 2>$null | Out-Null
    & icacls $INSTALL_DIR /T /grant Administrators:F 2>$null | Out-Null
    Remove-Item $INSTALL_DIR -Recurse -Force -ErrorAction SilentlyContinue
}

if (Test-Path $INSTALL_DIR) {
    Write-Host "[!] Could not remove every file: $INSTALL_DIR" -ForegroundColor Yellow
    Write-Host "    Close the app, then remove that folder manually." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "  Bluetooth Bridge removed." -ForegroundColor Green
    Write-Host "  You can reinstall at any time with the same one-line installer." -ForegroundColor Gray
    Write-Host ""
}
