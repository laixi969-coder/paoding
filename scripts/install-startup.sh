#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
PM2_BIN="$(command -v pm2)"
pm2 start ecosystem.config.cjs
pm2 save
if [ "$(uname -s)" = "Darwin" ]; then
  export PAODING_STARTUP_PM2="$PM2_BIN"
  .venv/bin/python - <<'PY'
import os, plistlib, subprocess
from pathlib import Path
target=Path.home()/'Library'/'LaunchAgents'/'local.paoding.pm2.plist'
target.parent.mkdir(parents=True,exist_ok=True)
payload={'Label':'local.paoding.pm2','ProgramArguments':[os.environ['PAODING_STARTUP_PM2'],'resurrect'],
         'RunAtLoad':True,'WorkingDirectory':str(Path.cwd()),
         'EnvironmentVariables':{'PATH':os.environ['PATH']}}
with target.open('wb') as stream: plistlib.dump(payload,stream)
subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}',str(target)],capture_output=True)
subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(target)],check=True)
print(f'Installed login recovery: {target}')
PY
else
  pm2 startup
  echo 'Run the system-specific command printed by PM2 to complete startup registration.'
fi
