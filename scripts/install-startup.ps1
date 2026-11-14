$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$pm2Path = (Get-Command pm2.cmd).Source
& $pm2Path start ecosystem.config.cjs
if ($LASTEXITCODE -ne 0) { throw "PM2 startup failed" }
& $pm2Path save
if ($LASTEXITCODE -ne 0) { throw "PM2 save failed" }
$action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument ('/d /s /c ""' + $pm2Path + '" resurrect"') -WorkingDirectory (Get-Location).Path
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "PaoDing-PM2-Recovery" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
Write-Host "Installed login recovery. Docker Desktop must also start at login."
