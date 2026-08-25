<#
.SYNOPSIS
    Auto-start the system tray control panel when you log in.

.DESCRIPTION
    Creates a Task Scheduler entry with a LOGON trigger (not the daily-09:00
    trigger register_task.ps1 uses) that launches `pythonw tray_app.py` — the
    persistent tray icon, which starts the web server immediately and sits in
    the notification area. No console window, no browser tab pops up on its
    own; use the tray menu's "Open dashboard", or the desktop shortcut
    (install_desktop_icon.ps1), to actually open it.

    Uses Task Scheduler rather than the plain Startup folder to stay
    consistent with how this project already registers its one other
    scheduled behavior, and so -Remove is symmetrical with register_task.ps1.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_tray_autostart.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_tray_autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$TaskName = "ResearchPaperAutomationTray",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$trayScript = Join-Path $projectDir "tray_app.py"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName' exists."
    }
    return
}

if (-not (Test-Path $trayScript)) {
    throw "Could not find tray_app.py at $trayScript"
}

$config = Join-Path $projectDir "config.toml"
if (-not (Test-Path $config)) {
    throw "config.toml not found. Copy config.example.toml to config.toml and set research_papers_root first."
}

$py = (Get-Command py -ErrorAction SilentlyContinue)
if (-not $py) {
    throw "The 'py' launcher was not found on PATH. Install Python or adjust this script."
}

# pythonw, not python: the tray must not carry a console window with it.
$pyDir = Split-Path -Parent (& $py.Source -c "import sys; print(sys.executable)")
$pythonw = Join-Path $pyDir "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $py.Source }

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$trayScript`"" `
    -WorkingDirectory $projectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Same reasoning as register_task.ps1: the underlying pipeline needs your
# interactive desktop session (saved Codex/Claude CLI logins), and so does
# opening a tray icon in the notification area at all — SYSTEM/service
# context has neither.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Replacing existing task '$TaskName'."
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Starts the Paper Review Automation tray icon and web server at logon." | Out-Null

Write-Host "Registered '$TaskName' to start at logon."
Write-Host "  Command:  $pythonw `"$trayScript`""
Write-Host "  Works in: $projectDir"
Write-Host ""
Write-Host "It will start now if you run it manually:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove it:     powershell -File scripts\register_tray_autostart.ps1 -Remove"
