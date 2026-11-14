$ErrorActionPreference = "Stop"
Unregister-ScheduledTask -TaskName "PaoDing-PM2-Recovery" -Confirm:$false
Write-Host "Removed login recovery. Existing PM2 processes are unchanged."
