const path = require("node:path");
const root = __dirname;
const executable = path.join(root, ".venv", process.platform === "win32" ? "Scripts" : "bin", process.platform === "win32" ? "paoding.exe" : "paoding");

module.exports = {
  apps: [
    ["paoding-server", "serve"],
    ["paoding-worker", "worker"],
    ["paoding-scheduler", "scheduler"],
    ["paoding-drop", "watch-drop"],
  ].map(([name, args]) => ({
    name, cwd: root, script: executable, args, interpreter: "none",
    instances: 1, autorestart: true, restart_delay: 3000,
    max_restarts: 20, kill_timeout: 10000, time: true,
    env: { PYTHONUNBUFFERED: "1" },
  })),
};
