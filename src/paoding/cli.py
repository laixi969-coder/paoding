import json
import platform
import subprocess
import sys
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config

from paoding.core.config import Settings
from paoding.domain.schemas import IngestRequest
from paoding.services.runtime import Runtime

app = typer.Typer(help="庖丁：采集、解剖、入库、进化", no_args_is_help=True)
models = typer.Typer(help="动态模型注册与健康探针")
app.add_typer(models, name="models")
vault = typer.Typer(help="Obsidian 知识库导出管理")
app.add_typer(vault, name="vault")


@vault.command("rebuild")
def rebuild_vault():
    """从数据库重新排队导出全部卡片；不修改原始媒体。"""
    from sqlalchemy import select

    from paoding.services.queue import enqueue
    from paoding.storage.tables import EvolutionLog, KnowledgeNode, new_id

    runtime = Runtime()
    run_id = new_id()
    count = 0
    with runtime.db.transaction() as session:
        for node in session.scalars(select(KnowledgeNode)):
            node.vault_status = "pending"
            enqueue(
                session,
                "export_node",
                {"node_id": node.id},
                key=f"rebuild:{run_id}:{node.id}",
                max_attempts=5,
            )
            count += 1
        for record in session.scalars(select(EvolutionLog)):
            enqueue(
                session,
                "export_taxonomy" if record.kind == "taxonomy" else "export_evolution",
                {"record_id": record.id},
                key=f"rebuild:{run_id}:{record.id}",
                max_attempts=5,
            )
            count += 1
    output({"scheduled_exports": count})


def output(value):
    document = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    try:
        document.encode(getattr(sys.stdout, "encoding", None) or "utf-8")
    except UnicodeEncodeError:
        # A redirected Windows stream may use a code page that cannot represent the result.
        document = json.dumps(value, ensure_ascii=True, indent=2, default=str)
    typer.echo(document)


def migrate(settings):
    project = Path(__file__).resolve().parents[2]
    ini = project / "alembic.ini"
    config = Config(str(ini)) if ini.exists() else Config()
    if not ini.exists():
        config.set_main_option("script_location", str(Path(__file__).parent / "_migrations"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")


@app.command()
def init():
    """创建目录并执行数据库迁移。"""
    settings = Settings()
    settings.prepare_directories()
    if not settings.models_file.exists():
        import shutil

        settings.models_file.parent.mkdir(parents=True, exist_ok=True)
        bundled = Path(__file__).parent / "_defaults" / "models.yaml"
        if bundled.exists():
            shutil.copyfile(bundled, settings.models_file)
    if not settings.feeds_file.exists():
        settings.feeds_file.parent.mkdir(parents=True, exist_ok=True)
        settings.feeds_file.write_text("feeds: {}\n", encoding="utf-8")
    migrate(settings)
    Runtime(settings).vault.initialize()
    output({"status": "initialized", "vault": str(settings.vault_dir.resolve())})


@app.command()
def ingest(source: str, instruction: str = "", priority: str = "normal"):
    """投喂 URL 或本地文件。"""
    rt = Runtime()
    if source.startswith(("https://", "http://")):
        output(rt.ingest(IngestRequest(url=source, instruction=instruction, priority=priority)))
    else:
        output(rt.ingest_file(Path(source), instruction))


@app.command()
def worker(once: bool = False, drain: bool = False):
    """执行持久化队列；--drain 处理当前可领取的所有任务后退出。"""
    from paoding.services.worker import Worker

    instance = Worker(Runtime())
    if once:
        output({"processed": instance.run_once()})
    elif drain:
        count = 0
        while instance.run_once():
            count += 1
        output({"processed": count})
    else:
        instance.run()


@app.command()
def scheduler():
    """运行每日 02:00 巡检、每周一 03:00 进化调度。"""
    from paoding.services.scheduler import run_scheduler

    run_scheduler(Runtime())


@app.command("watch-drop")
def watch_drop_command():
    """监听本地拖拽目录（含 WorkBuddy 导出文件）。"""
    from paoding.ingestion.drop import watch_drop

    watch_drop(Runtime())


@app.command()
def serve():
    """启动 FastAPI 服务。"""
    from paoding.api.app import serve as run

    run()


@app.command("open")
def open_command():
    """打开本机知识库，冷启动时一并运行网页和收录 Worker。"""
    import httpx
    from filelock import Timeout

    from paoding.local_workspace import open_workspace

    try:
        open_workspace()
    except (RuntimeError, OSError, Timeout, httpx.HTTPError) as exc:
        typer.echo(f"打开工作台失败：{exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def mcp():
    """启动本地 MCP stdio 服务。"""
    from paoding.mcp import main

    main()


@models.command("sync")
def models_sync():
    output(Runtime().gateway.sync())


@models.command("test")
def models_test():
    """执行真实小额生成探针，可能产生模型费用。"""
    output(Runtime().gateway.test())


@models.command("list")
def models_list():
    output(Runtime().gateway.list_models())


@app.command("reindex")
def reindex(force: bool = False):
    """为缺失/旧模型向量排队回填；--force 重建全部。"""
    from sqlalchemy import or_, select

    from paoding.services.queue import enqueue
    from paoding.storage.tables import AtomElement, new_id

    rt = Runtime()
    if not rt.settings.embedding_model or not rt.settings.embedding_provider:
        raise typer.BadParameter("Configure embedding provider and model before reindexing")
    run_id, count = new_id(), 0
    with rt.db.transaction() as session:
        stmt = select(AtomElement)
        if not force:
            stmt = stmt.where(
                or_(
                    AtomElement.embedding.is_(None),
                    AtomElement.embedding_model.is_(None),
                    AtomElement.embedding_model != rt.settings.embedding_model,
                )
            )
        for atom in session.scalars(stmt):
            enqueue(
                session,
                "embed",
                {"atom_id": atom.id, "model": rt.settings.embedding_model},
                key=f"reindex:{run_id}:{atom.id}",
            )
            count += 1
    output({"scheduled": count, "model": rt.settings.embedding_model})


@app.command("taxonomy")
def taxonomy(run: bool = False):
    """查看分类树，或 --run 排队执行同义标签蒸馏。"""
    from paoding.services.queue import enqueue
    from paoding.storage.tables import new_id

    rt = Runtime()
    if not run:
        output(rt.taxonomy.tree())
    else:
        with rt.db.transaction() as session:
            job = enqueue(session, "taxonomy", {"manual_run": new_id()})
            output({"job_id": job.id})


@app.command("doctor")
def doctor():
    """检查密钥是否配置、可执行文件和数据库；不打印密钥、不发模型请求。"""
    import shutil

    from sqlalchemy import text

    rt = Runtime()
    checks = {
        "ffmpeg": bool(shutil.which(rt.settings.ffmpeg_command)),
        "ffprobe": bool(shutil.which(rt.settings.ffprobe_command)),
        "models": {
            name: bool(rt.gateway.key(provider))
            for name, provider in rt.gateway.config().providers.items()
        },
        "services": {
            name: bool(getattr(rt.settings, name).get_secret_value())
            for name in (
                "tikhub_token",
                "mineru_token",
                "telegram_bot_token",
                "feishu_app_secret",
                "wecom_corp_secret",
                "api_token",
            )
        },
    }
    try:
        with rt.db.transaction() as session:
            checks["database"] = bool(
                session.execute(text("SELECT 1 FROM alembic_version")).scalar()
            )
    except Exception:
        checks["database"] = False
    output(checks)


@app.command("bookmarklet")
def bookmarklet(base_url: str = "http://127.0.0.1:8010"):
    """输出浏览器书签代码；令牌只在庖丁页面填写，不进入书签 URL。"""
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username
        or parts.password
    ):
        raise typer.BadParameter("Use an HTTP(S) API origin without credentials")
    base = base_url.rstrip("/") + "/capture#"
    typer.echo(
        "javascript:(()=>{window.open("
        + json.dumps(base)
        + "+encodeURIComponent(JSON.stringify({url:location.href,title:document.title,auto:true})), '_blank', 'noopener,noreferrer')})()"
    )


@app.command()
def grab(url: str | None = None):
    """macOS 抓取 Safari/Chrome 当前页；其他平台可传 URL 或读取剪贴板。"""
    if not url and platform.system() == "Darwin":
        script = """tell application "System Events" to set frontApp to name of first application process whose frontmost is true
if frontApp is "Safari" then
    tell application "Safari" to return URL of current tab of front window
else if frontApp is "Google Chrome" then
    tell application "Google Chrome" to return URL of active tab of front window
else
    return ""
end if"""
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10
        )
        url = result.stdout.strip() if result.returncode == 0 else None
    if not url:
        import pyperclip

        url = pyperclip.paste().strip()
    output(Runtime().ingest(IngestRequest(url=url)))


@app.command()
def demo():
    """完全隔离的 SQLite/演示输出，跑通入库、解剖、导出闭环。"""
    root = Path("data/demo").resolve()
    settings = Settings(
        database_url=f"sqlite:///{root / 'paoding.db'}",
        data_dir=root,
        vault_dir=root / "PaoDing_Vault",
        drop_dir=root / "PaoDing_Drop",
        demo_mode=True,
    )
    settings.prepare_directories()
    migrate(settings)
    rt = Runtime(settings)
    example = root / "example.md"
    example.write_text(
        "# 一个可复用的内容结构\n\n先呈现用户的具体问题，再用三个步骤解释解决办法，最后给出验证标准。",
        encoding="utf-8",
    )
    result = rt.ingest_file(example)
    from paoding.services.worker import Worker
    from paoding.storage.tables import Job, KnowledgeNode

    instance = Worker(rt)
    while instance.run_once():
        pass
    with rt.db.transaction() as session:
        job = session.get(Job, result["job_id"])
        if job.status != "succeeded":
            output({"status": job.status, "error": job.error_message})
            raise typer.Exit(1)
        node = session.get(KnowledgeNode, job.result["node_id"])
        output(
            {
                "demo": True,
                "job_id": job.id,
                "node_id": node.id,
                "vault_status": node.vault_status,
                "vault_path": node.vault_path,
                "atoms": rt.library.fetch_atoms(limit=3),
            }
        )


if __name__ == "__main__":
    app()
