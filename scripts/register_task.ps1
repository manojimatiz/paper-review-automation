<#
.SYNOPSIS
    Register the nightly research-paper automation with Windows Task Scheduler.

.DESCRIPTION
    Creates a daily trigger that runs `py run.py` from the project directory.
    Task Scheduler fires in the MACHINE's local time, while the pipeline decides
    which month folder is current using the `timezone` in config.toml
    (default Asia/Kolkata). If this machine is in a different zone, pass -At with
    the local time that corresponds to 09:00 in your configured zone.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -At 06:30 -TaskName "Papers"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$TaskName = "ResearchPaperAutomation",
    [string]$At = "09:00",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$runScript = Join-Path $projectDir "run.py"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName' exists."
    }
    return
}

if (-not (Test-Path $runScript)) {
    throw "Could not find run.py at $runScript"
}

$config = Join-Path $projectDir "config.toml"
if (-not (Test-Path $config)) {
    throw "config.toml not found. Copy config.example.toml to config.toml and set research_papers_root first."
}

$py = (Get-Command py -ErrorAction SilentlyContinue)
if (-not $py) {
    throw "The 'py' launcher was not found on PATH. Install Python or adjust this script."
}

# -NoProfile keeps startup deterministic; the working directory is pinned so
# relative paths in config.toml resolve the same way they do interactively.
$action = New-ScheduledTaskAction -Execute $py.Source `
    -Argument "`"$runScript`"" `
    -WorkingDirectory $projectDir

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)

# Run whether or not the user is logged on would require a stored password, and
# the CLIs need the user's own session for their saved logins. So: interactive.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Replacing existing task '$TaskName'."
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Nightly research-paper review (Codex) and revision (Claude)." | Out-Null

Write-Host "Registered '$TaskName' to run daily at $At."
Write-Host "  Command:  $($py.Source) `"$runScript`""
Write-Host "  Works in: $projectDir"
Write-Host ""
Write-Host "Verify with:   Get-ScheduledTask -TaskName $TaskName"
Write-Host "Run it now:    Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove it:     powershell -File scripts\register_task.ps1 -Remove"
