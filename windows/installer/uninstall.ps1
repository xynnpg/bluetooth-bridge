#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Bluetooth Bridge — Uninstaller

.DESCRIPTION
    Removes the Windows Bluetooth Bridge installation including:
    - Application files and venv
    - Startup + Start Menu shortcuts
    - Log files

    Run from an elevated PowerShell OR the Start Menu shortcut:
        powershell -ExecutionPolicy Bypass -File "%USERPROFILE%\bluetooth_bridge\uninstall.ps1"
#>

$ErrorActionPreference = "Stop"

$INSTALL_DIR  = "$env:USERPROFILE\bluetooth_bridge"
$STARTUP_DIR  = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$LOG_DIR      = "$env:LOCALAPPDATA\bluetooth_bridge"

Write-Host "[*] Bluetooth Bridge — Uninstalling …" -ForegroundColor Cyan

# Stop the running app if it is running
$running = Get-Process | Where-Object {
    $_.Path -like "*bluetooth_bridge*"
} -ErrorAction SilentlyContinue

if ($running) {
    Write-Host "[*] Stopping running instance …" -ForegroundColor Cyan
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep 2
}

# Remove shortcuts
$shortcuts = @(
    "$STARTUP_DIR\Bluetooth Bridge.lnk",
    "$INSTALL_DIR\Bluetooth Bridge.lnk"
)

foreach ($lnk in $shortcuts) {
    if (Test-Path $lnk) {
        Remove-Item $lnk -Force
        Write-Host "[✓] Removed shortcut: $lnk" -ForegroundColor Green
    }
}

# Remove app directory
if (Test-Path $INSTALL_DIR) {
    Write-Host "[*] Removing application directory …" -ForegroundColor Cyan
    # First try normal delete
    Remove-Item $INSTALL_DIR -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $INSTALL_DIR) {
        # Retry with takeown on locked files
        & takeown /F $INSTALL_DIR /R /D Y 2>$null | Out-Null
        & icacls $INSTALL_DIR /T /grant Administrators:F 2>$null | Out-Null
        Remove-Item $INSTALL_DIR -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $INSTALL_DIR) {
        Write-Host "[!] Could not remove $INSTALL_DIR — remove it manually." -ForegroundColor Yellow
    } else {
        Write-Host "[✓] Application directory removed." -ForegroundColor Green
    }
}

# Suggest removing log directory too (keep user's logs by default — logs are small)
$removeLogs = Read-Host "Remove log files too? [y/N]"
if ($removeLogs -eq "y" -or $removeLogs -eq "Y") {
    if (Test-Path $LOG_DIR) {
        Remove-Item $LOG_DIR -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[✓] Log directory removed." -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "[✓] Uninstall complete." -ForegroundColor Green
Write-Host "    ViGEmBus driver was left installed (it is a shared system component)."
Write-Host "    To remove it, use Programs & Features in Control Panel."
Write-Host ""