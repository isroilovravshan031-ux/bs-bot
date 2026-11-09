# Windows'ga kirganda botni avtomatik ishga tushiradigan vazifa yaratadi.
# Olib tashlash: Unregister-ScheduledTask -TaskName NakladnoyBot -Confirm:$false
$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\start.ps1`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'NakladnoyBot' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host 'NakladnoyBot vazifasi yaratildi.'
