$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path $python)) { throw 'Install the virtual environment first.' }
$config = Get-Content (Join-Path $PSScriptRoot 'settings.json') -Raw | ConvertFrom-Json
if ($config.enabled -ne $true) { throw 'Enable applications in settings.json first.' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"{0}" apply' -f (Join-Path $PSScriptRoot 'jobs.py')) -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 4)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'Recent Job Applications' -Action $action -Trigger $trigger -Settings $settings -Description 'Check configured sources every four hours while signed in.'
