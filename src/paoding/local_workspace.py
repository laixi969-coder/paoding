"""Interactive local launcher. The standalone serve and worker commands stay independent."""

import signal
import subprocess
import sys
import time
import webbrowser
from contextlib import ExitStack

import httpx
from filelock import FileLock

from paoding.core.config import Settings


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def api_running(client):
    try:
        response = client.get("/health/live")
    except httpx.ConnectError:
        return False
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code == 200 and isinstance(data, dict) and data.get("status") == "ok":
        if data.get("service") == "paoding":
            return True
        # Older releases exposed only the version in /health/live.
        if "service" not in data and data.get("version"):
            try:
                info = client.get("/openapi.json").json()["info"]
                if info["title"] == "庖丁 PaoDing Engine":
                    return True
            except (ValueError, KeyError, TypeError):
                pass
    raise RuntimeError("配置的端口已有其他服务。请检查 .env 中的 PAODING_PORT。")


def open_workspace():
    settings = Settings()
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(settings.host, settings.host)
    base = httpx.URL(scheme="http", host=host, port=settings.port)
    url = str(base.join("/library"))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(settings.data_dir / f".workspace-{settings.port}.lock", timeout=90)

    with httpx.Client(base_url=base, timeout=5, trust_env=False) as client, ExitStack() as owned:
        processes = []

        def stop_workspace(signum, frame):
            raise SystemExit(0)

        for signum in {signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)}:
            previous = signal.signal(signum, stop_workspace)
            owned.callback(signal.signal, signum, previous)

        def start(command):
            process = subprocess.Popen([sys.executable, "-m", "paoding.cli", command])
            owned.callback(stop_child, process)
            processes.append(process)
            return process

        # A second double-click during cold startup waits for the first copy.
        with lock:
            if not api_running(client):
                print("正在启动本机工作台…", flush=True)
                server = start("serve")
                deadline = time.monotonic() + 75
                while not api_running(client):
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("网页服务未能启动，请检查上方日志和 .env 中的端口。")
                    time.sleep(0.25)
            try:
                ready = client.get("/health/ready").status_code == 200
            except httpx.TimeoutException:
                ready = False
            if not ready:
                raise RuntimeError(
                    "数据库尚未就绪。使用项目自带数据库时，请先运行 "
                    "docker compose up -d --wait postgres。首次使用还需运行 uv run paoding init。"
                )
            if processes:
                start("worker")

        print(f"知识库已就绪：{url}", flush=True)
        if not webbrowser.open_new_tab(url):
            print("请在浏览器中打开上方地址。", flush=True)
        if not processes:
            print("已复用现有服务。单独运行 serve 时，可用 uv run paoding worker 处理收录。")
            return
        print("此窗口负责网页和收录处理。可最小化窗口，按 Ctrl+C 结束。", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        raise RuntimeError("工作台进程已退出。请查看上方日志，修复后重新打开。")
