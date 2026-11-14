from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from paoding.core.config import Settings
from paoding.core.errors import SourceError
from paoding.domain.schemas import IngestRequest
from paoding.ingestion.normalizer import SUPPORTED_EXTENSIONS, Normalizer
from paoding.ingestion.urls import identify_url
from paoding.models.gateway import ModelGateway
from paoding.services.library import Library
from paoding.services.queue import JobQueue, enqueue, fingerprint
from paoding.storage.db import Database
from paoding.storage.tables import WatchedAccount
from paoding.storage.vault import Vault


class Runtime:
    def __init__(self, settings=None, db=None, gateway=None, normalizer=None):
        self.settings = settings or Settings()
        self.db = db or Database(self.settings)
        self.gateway = gateway or ModelGateway(self.settings, self.db)
        self.normalizer = normalizer or Normalizer(self.settings)
        self.queue = JobQueue(self.db, self.settings)
        self.vault = Vault(self.settings.vault_dir)
        self.library = Library(self.db, self.gateway)
        from paoding.integrations.bots import BotGateway

        self.bots = BotGateway(self.settings)
        from paoding.services.taxonomy import TaxonomyService

        self.taxonomy = TaxonomyService(self)

    def ingest(self, request: IngestRequest, idempotency_key=None, notification=None):
        source = identify_url(str(request.url))
        from paoding.ingestion.collectors import FeedCollector
        from paoding.ingestion.urls import Source

        if source.url in FeedCollector(self.settings).mappings():
            source = Source(source.url, source.platform, "creator")
        payload = {"url": source.url, "instruction": request.instruction}
        if request.title:
            payload["title"] = request.title
        if notification:
            payload["notification"] = notification
        key = "ingest:" + fingerprint(idempotency_key if idempotency_key else payload)
        priority = {"low": 0, "normal": 1, "high": 2}[request.priority]
        with self.db.transaction() as session:
            if source.kind == "creator":
                account = session.scalar(
                    select(WatchedAccount).where(WatchedAccount.profile_url == source.url)
                )
                if not account:
                    try:
                        with session.begin_nested():
                            account = WatchedAccount(
                                platform=source.platform,
                                profile_url=source.url,
                                name=source.url.rstrip("/").rsplit("/", 1)[-1],
                            )
                            session.add(account)
                            session.flush()
                    except IntegrityError:
                        account = session.scalar(
                            select(WatchedAccount).where(WatchedAccount.profile_url == source.url)
                        )
                payload["account_id"] = account.id
                job = enqueue(session, "watch", payload, key, priority, self.settings.max_attempts)
                return {
                    "kind": "creator",
                    "account_id": account.id,
                    "job_id": job.id,
                    "status": job.status,
                }
            job = enqueue(session, "ingest", payload, key, priority, self.settings.max_attempts)
            return {"kind": "item", "job_id": job.id, "status": job.status}

    def accept_bot(self, message, queued_ack=False):
        with self.db.transaction() as session:
            job = enqueue(
                session,
                "bot_ingest",
                message,
                key="bot:" + fingerprint([message["provider"], message["event_id"]]),
                max_attempts=self.settings.max_attempts,
            )
            if queued_ack:
                enqueue(
                    session,
                    "notify",
                    {"target": message["target"], "text": f"🔪 庖丁已接单\n任务：{job.id}"},
                    key=f"bot-ack:{job.id}",
                    priority=10,
                )
            return job.id

    def ingest_file(self, path: Path, instruction=""):
        if path.name.endswith(".paoding.json"):
            return self.ingest_export(path, instruction)
        # Snapshot before queueing so later user edits/removals cannot change a queued job's bytes.
        path = path.resolve()
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise SourceError("Unsupported local file")
        if path.stat().st_size > self.settings.max_download_bytes:
            raise SourceError("Local file exceeds configured size limit")
        snapshot = self.normalizer._store(path.read_bytes(), path.suffix.lower())
        payload = {
            "local_path": str(snapshot),
            "source_url": path.as_uri(),
            "title": path.name,
            "instruction": instruction,
        }
        with self.db.transaction() as session:
            job = enqueue(session, "ingest", payload, max_attempts=self.settings.max_attempts)
            return {"kind": "item", "job_id": job.id, "status": job.status}

    def ingest_export(self, manifest: Path, instruction=""):
        import json

        manifest = manifest.resolve()
        if manifest.stat().st_size > 1024 * 1024:
            raise SourceError("WorkBuddy manifest exceeds 1 MiB")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        files = data.get("files", [])
        if not isinstance(files, list) or len(files) > 8:
            raise SourceError("Export manifest accepts at most eight files")
        snapshots = []
        for file in files:
            path = (manifest.parent / file).resolve()
            if not path.is_relative_to(manifest.parent) or not path.is_file():
                raise SourceError("Export files must stay inside the manifest directory")
            if (
                path.suffix.lower() not in SUPPORTED_EXTENSIONS
                or path.stat().st_size > self.settings.max_download_bytes
            ):
                raise SourceError("Unsupported or oversized export attachment")
            snapshots.append(str(self.normalizer._store(path.read_bytes(), path.suffix.lower())))
        source = identify_url(data["url"]) if data.get("url") else None
        payload = {
            "local_paths": snapshots,
            "inline_text": str(data.get("text", ""))[: self.settings.max_text_chars],
            "source_url": source.url if source else manifest.as_uri(),
            "platform": source.platform if source else "local",
            "title": str(data.get("title", manifest.stem))[:300],
            "instruction": instruction or str(data.get("instruction", ""))[:2000],
        }
        if not snapshots and not payload["inline_text"]:
            raise SourceError("Export has neither text nor files")
        with self.db.transaction() as session:
            job = enqueue(session, "ingest", payload, max_attempts=self.settings.max_attempts)
            return {"kind": "item", "job_id": job.id, "status": job.status}
