<#
.SYNOPSIS
    Put a "Paper Review Automation" shortcut on the Desktop.

.DESCRIPTION
    Creates a .lnk that runs `pythonw tray_app.py --open` — a one-shot action,
    not a second tray icon: if the dashboard is already running (e.g. started
    automatically at login by register_tray_autostart.ps1), it just opens a
    browser tab; otherwise it starts the tray/server first, then opens the tab.
    See tray_app.py's module docstring for why this is a separate mode from
    the plain tray icon.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_desktop_icon.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_desktop_icon.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$ShortcutName = "Paper Review Automation",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$trayScript = Join-Path $projectDir "tray_app.py"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "$ShortcutName.lnk"

if ($Remove) {
    if (Test-Path $shortcutPath) {
        Remove-Item $shortcutPath -Force
        Write-Host "Removed desktop shortcut: $shortcutPath"
    } else {
        Write-Host "No shortcut found at $shortcutPath"
    }
    return
}

if (-not (Test-Path $trayScript)) {
    throw "Could not find tray_app.py at $trayScript"
}

$py = (Get-Command py -ErrorAction SilentlyContinue)
if (-not $py) {
    throw "The 'py' launcher was not found on PATH. Install Python or adjust this script."
}

$pyDir = Split-Path -Parent (& $py.Source -c "import sys; print(sys.executable)")
$pythonw = Join-Path $pyDir "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $py.Source }

$iconPath = Join-Path $projectDir "webui\static\tray.ico"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$trayScript`" --open"
$shortcut.WorkingDirectory = $projectDir
if (Test-Path $iconPath) {
    $shortcut.IconLocation = $iconPath
}
$shortcut.Description = "Open the Paper Review Automation dashboard"
$shortcut.Save()

Write-Host "Created desktop shortcut: $shortcutPath"
Write-Host "  Target: $pythonw `"$trayScript`" --open"
