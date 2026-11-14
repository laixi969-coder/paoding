import json
import logging
import threading
import time
from datetime import timedelta

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError

from paoding.core.errors import LeaseLost, PaodingError
from paoding.domain.schemas import Modality
from paoding.services.library import rank_trends
from paoding.services.queue import enqueue, fingerprint
from paoding.storage.queries import has_tag
from paoding.storage.tables import (
    AtomElement,
    EvolutionLog,
    Job,
    KnowledgeNode,
    WatchedAccount,
    now,
)

logger = logging.getLogger(__name__)


def extract_atoms(analysis):
    data = analysis.model_dump(mode="json")
    if analysis.modality == Modality.TEXT:
        pieces = [
            (
                "sop",
                {
                    k: data[k]
                    for k in (
                        "core_pain_point",
                        "consumer_insight",
                        "big_idea",
                        "super_symbol",
                        "execution_sop",
                    )
                },
            )
        ]
    elif analysis.modality == Modality.IMAGE:
        pieces = [
            (
                "visual",
                {
                    k: data[k]
                    for k in ("color_system", "layout_and_grid", "typography", "reusable_mj_prompt")
                },
            )
        ]
    else:
        pieces = [
            ("hook", data["golden_hook_0_to_3s"]),
            (
                "storyboard",
                {k: data[k] for k in ("pacing_metrics", "beat_sheet", "reusable_script_formula")},
            ),
        ]
    return [(kind, json.dumps(content, ensure_ascii=False, indent=2)) for kind, content in pieces]


class Worker:
    def __init__(self, runtime):
        self.rt = runtime
        from pathlib import Path

        self._settings_stamp = Path(".env").stat().st_mtime_ns if Path(".env").exists() else None

    def run_once(self):
        from pathlib import Path

        from paoding.core.settings_panel import refresh_settings

        stamp = Path(".env").stat().st_mtime_ns if Path(".env").exists() else None
        if stamp != self._settings_stamp:
            refresh_settings(self.rt.settings)
            self.rt.bots.cache.clear()
            self._settings_stamp = stamp
        job = self.rt.queue.claim()
        if not job:
            return False
        stop = threading.Event()

        def renew():
            while not stop.wait(self.rt.settings.lease_seconds / 3):
                try:
                    if not self.rt.queue.heartbeat(job):
                        return
                except OperationalError:
                    logger.warning("Lease renewal temporarily unavailable for job %s", job.id)

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            handlers = {
                "ingest": self.ingest,
                "export_node": self.export_node,
                "watch": self.watch,
                "evolve": self.evolve,
                "export_evolution": self.export_evolution,
                "embed": self.embed,
                "notify": self.notify,
                "bot_ingest": self.bot_ingest,
                "models_sync": self.models_sync,
                "taxonomy": self.rt.taxonomy.run,
                "export_taxonomy": self.export_taxonomy,
            }
            if job.kind not in handlers:
                raise ValueError("Unknown job kind")
            handlers[job.kind](job)
        except LeaseLost:
            logger.warning("Discarded stale worker result for job %s", job.id)
        except Exception as exc:
            known = isinstance(exc, PaodingError)
            retryable = (
                exc.retryable
                if known
                else isinstance(exc, (OperationalError, OSError, httpx.HTTPError))
            )
            code = exc.code if known else type(exc).__name__
            # Never expose upstream raw responses, request headers or credential-bearing URLs.
            message = (
                str(exc) if known else f"{type(exc).__name__}; inspect local service configuration"
            )
            self.rt.queue.fail(job, code, message, retryable)
            target = job.payload.get("notification") or (
                job.payload.get("target") if job.kind == "bot_ingest" else None
            )
            if target:
                with self.rt.db.transaction() as session:
                    failed = session.get(Job, job.id)
                    if failed and failed.status == "failed":
                        enqueue(
                            session,
                            "notify",
                            {
                                "target": target,
                                "text": f"庖丁处理失败（{code}），任务 {job.id}。配置修复后可重试。",
                            },
                            key=f"failed-notice:{job.id}:{job.lease_token}",
                        )
            logger.warning("Job %s failed (%s)", job.id, code)
        finally:
            stop.set()
            heartbeat.join(timeout=2)
        return True

    def run(self):
        while True:
            if not self.run_once():
                time.sleep(1)

    def ingest(self, job):
        content = self.rt.normalizer.normalize(job.payload)
        if content.audio_paths and (
            content.modality == Modality.TEXT or self.rt.settings.transcription_model
        ):
            transcript = self.rt.gateway.transcribe(content.audio_paths)
            content.text = (
                content.text.replace("[待转写音频]", "") + "\n\n音频转写：\n" + transcript
            )[: self.rt.settings.max_text_chars]
            content.limitations.append(
                "Speech text is machine-transcribed; verify quotes against the original audio"
            )
        if job.payload.get("title"):
            content.title = job.payload["title"]
        content.creator_id = job.payload.get("creator_id")
        if job.payload.get("metrics"):
            content.metrics.update(job.payload["metrics"])
        analysis, provenance = self.rt.gateway.analyze(content, job.payload.get("instruction", ""))
        published_at = None
        if content.published_at:
            from datetime import UTC, datetime

            try:
                published_at = (
                    datetime.fromisoformat(content.published_at.replace("Z", "+00:00"))
                    .astimezone(UTC)
                    .replace(tzinfo=None)
                )
            except ValueError:
                pass
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            node = KnowledgeNode(
                job_id=job.id,
                creator_id=content.creator_id,
                platform=content.platform,
                source_url=content.source_url,
                title=analysis.title,
                modality=analysis.modality,
                summary=analysis.summary,
                analysis=analysis.model_dump(mode="json"),
                cross_domain_dna=analysis.cross_domain_dna.model_dump(),
                media_paths=list(
                    dict.fromkeys(
                        str(p)
                        for p in [
                            *content.media_paths,
                            *content.original_media_paths,
                            *content.audio_paths,
                        ]
                    )
                ),
                metrics=content.metrics,
                provenance=provenance,
                published_at=published_at,
            )
            session.add(node)
            session.flush()
            for kind, text in extract_atoms(analysis):
                atom = AtomElement(
                    node_id=node.id,
                    atom_type=kind,
                    tags=analysis.tags,
                    content=text,
                    evergreen=analysis.evergreen,
                )
                session.add(atom)
                session.flush()
                self.rt.taxonomy.attach_node(session, node.id, kind, analysis.tags)
                if self.rt.settings.embedding_model:
                    enqueue(session, "embed", {"atom_id": atom.id})
            payload = {"node_id": node.id}
            if job.payload.get("notification"):
                payload["notification"] = job.payload["notification"]
            export = enqueue(session, "export_node", payload, max_attempts=5)
            self.rt.queue.succeed(session, job, {"node_id": node.id, "export_job_id": export.id})
            self.schedule_thresholds(session, node)

    def export_node(self, job):
        # Fencing lock is held throughout filesystem writes to prevent a reclaimed worker from racing.
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            node = session.get(KnowledgeNode, job.payload["node_id"])
            atoms = session.scalars(select(AtomElement).where(AtomElement.node_id == node.id)).all()
            path = self.rt.vault.write_node(node, atoms)
            node.vault_status, node.vault_path = "synced", path
            self.rt.queue.succeed(session, job, {"vault_path": path})
            if job.payload.get("notification"):
                enqueue(
                    session,
                    "notify",
                    {
                        "target": job.payload["notification"],
                        "card": {"title": node.title, "node_id": node.id},
                        "text": f"🔪 庖丁已完成：{node.title}\n\n{node.summary[:1000]}\n\n入库：{path}",
                    },
                )

    def embed(self, job):
        requested_model = job.payload.get("model")
        if requested_model and requested_model != self.rt.settings.embedding_model:
            with self.rt.db.transaction() as session:
                self.rt.queue.succeed(session, job, {"skipped": "embedding_model_changed"})
            return
        with self.rt.db.transaction() as session:
            atom = session.get(AtomElement, job.payload["atom_id"])
            content = atom.content
        vector = self.rt.gateway.embed(content)
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            atom = session.get(AtomElement, job.payload["atom_id"])
            atom.embedding = vector
            atom.embedding_model = self.rt.settings.embedding_model if vector else None
            self.rt.queue.succeed(session, job, {"embedded": vector is not None})

    def watch(self, job):
        with self.rt.db.transaction() as session:
            account = session.get(WatchedAccount, job.payload["account_id"])
            if account.status != "active":
                self.rt.queue.succeed(session, job, {"skipped": "account_paused"})
                return
            cursor = job.payload.get("cursor", account.last_cursor)
            if "cursor" in job.payload and cursor != account.last_cursor:
                self.rt.queue.succeed(session, job, {"skipped": "stale_pagination"})
                return
            cold = not account.cold_started
            original_cursor = account.last_cursor
        page = self.rt.normalizer.collector.account_items(account.profile_url, cursor, cold)
        if page.has_more and (not page.next_cursor or page.next_cursor == cursor):
            raise ValueError("Collector pagination must advance its cursor")
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            account = session.get(WatchedAccount, account.id, with_for_update=True)
            if account.last_cursor != original_cursor:
                self.rt.queue.succeed(
                    session, job, {"skipped": "cursor_advanced_by_another_worker"}
                )
                return
            if account.status != "active":
                self.rt.queue.succeed(session, job, {"skipped": "account_paused"})
                return
            jobs = []
            for item in page.items[:20] if cold else page.items:
                payload = {
                    "url": str(item.url),
                    "instruction": job.payload.get("instruction", ""),
                    "creator_id": account.id,
                    "metrics": item.metrics,
                }
                if item.resolved:
                    payload["collected_item"] = item.model_dump(mode="json")
                # The stable source identity excludes mutable interaction counts.
                key = "watch-item:" + fingerprint([account.id, str(item.url)])
                existing = session.scalar(select(Job).where(Job.dedupe_key == key))
                item_job = existing or enqueue(session, "ingest", payload, key)
                jobs.append(item_job.id)
            account.cold_started = True
            account.last_polled_at = now()
            if page.next_cursor:
                account.last_cursor = page.next_cursor
            if page.has_more and not cold:
                enqueue(
                    session,
                    "watch",
                    {**job.payload, "cursor": page.next_cursor},
                    key="watch-page:" + fingerprint([job.id, page.next_cursor]),
                )
            self.rt.queue.succeed(
                session,
                job,
                {
                    "account_id": account.id,
                    "enqueued_jobs": jobs,
                    "next_cursor": page.next_cursor,
                    "scanned_count": page.scanned_count,
                    "scan_complete": page.scan_complete,
                    "limitations": page.limitations,
                },
            )
            if job.payload.get("notification"):
                enqueue(
                    session,
                    "notify",
                    {
                        "target": job.payload["notification"],
                        "text": f"🔪 已订阅账号 {account.name}，本轮入队 {len(jobs)} 条作品。\n"
                        + "\n".join(page.limitations),
                    },
                    key=f"watch-notice:{job.id}",
                )

    def schedule_thresholds(self, session, node):
        # Serialize only this short count/enqueue stage, not collection or inference.
        # Prevent two concurrent inserts at the threshold from both observing N-1.
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(74190201)"))
        total = session.scalar(select(func.count()).select_from(KnowledgeNode))
        if total >= self.rt.settings.taxonomy_min_sources:
            bucket = total // self.rt.settings.taxonomy_min_sources
            enqueue(session, "taxonomy", {"generation": bucket}, key=f"taxonomy:{bucket}")
        if node.creator_id:
            count = session.scalar(
                select(func.count())
                .select_from(KnowledgeNode)
                .where(KnowledgeNode.creator_id == node.creator_id)
            )
            if count >= 5:
                enqueue(
                    session,
                    "evolve",
                    {"kind": "persona", "creator_id": node.creator_id, "generation": count // 5},
                    key=f"persona:{node.creator_id}:{count // 5}",
                )
        # Atom tags are portable JSON; count distinct nodes, not duplicated hook/storyboard atoms.
        for tag in node.analysis["tags"]:
            count = session.scalar(
                select(func.count(func.distinct(AtomElement.node_id))).where(
                    has_tag(session, AtomElement.tags, tag)
                )
            )
            if count >= 20:
                payload = {"kind": "playbook", "tag": tag, "generation": count // 20}
                enqueue(session, "evolve", payload, key="playbook:" + fingerprint(payload))

    def evolve(self, job):
        kind = job.payload["kind"]
        if kind not in {"persona", "playbook", "trend"}:
            raise ValueError("Unknown evolution kind")
        with self.rt.db.transaction() as session:
            stmt = select(KnowledgeNode).order_by(KnowledgeNode.created_at.desc())
            if kind == "persona":
                stmt = stmt.where(KnowledgeNode.creator_id == job.payload["creator_id"])
            elif kind == "trend":
                stmt = stmt.where(
                    func.coalesce(KnowledgeNode.published_at, KnowledgeNode.created_at)
                    >= now() - timedelta(days=7),
                    KnowledgeNode.creator_id.is_not(None),
                )
            nodes = session.scalars(stmt).all()
            if kind == "playbook":
                nodes = [n for n in nodes if job.payload["tag"] in n.analysis.get("tags", [])]
            source_count = len(nodes)
            if kind == "trend":
                nodes = rank_trends(nodes)
            # Bounded Map stage: summarize each batch, then Reduce across those summaries.
            nodes = nodes[:200]
        if not nodes:
            with self.rt.db.transaction() as session:
                self.rt.queue.succeed(session, job, {"skipped": "no_eligible_sources"})
            return
        context = None
        if kind == "trend":
            comparison = self.rt.library.scan_trends(7)
            context = {
                key: comparison[key]
                for key in ("sample_size", "previous_sample_size", "tag_changes")
            }
        document = self.reduce(nodes, kind, context)
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            record = EvolutionLog(
                job_id=job.id,
                kind=kind,
                document={
                    **document.model_dump(),
                    "sample_size": len(nodes),
                    "eligible_source_count": source_count,
                    "demo": self.rt.settings.demo_mode,
                    "creator_id": job.payload.get("creator_id"),
                    "trend_comparison": context,
                },
            )
            session.add(record)
            session.flush()
            if kind == "persona":
                account = session.get(
                    WatchedAccount, job.payload["creator_id"], with_for_update=True
                )
                if source_count >= account.persona_node_count:
                    account.persona_json, account.persona_node_count = (
                        {**record.document, "evolution_id": record.id},
                        source_count,
                    )
            export = enqueue(session, "export_evolution", {"record_id": record.id}, max_attempts=5)
            self.rt.queue.succeed(
                session, job, {"evolution_id": record.id, "export_job_id": export.id}
            )

    def reduce(self, nodes, kind, context=None):
        if len(nodes) <= 10:
            return self.rt.gateway.evolve(nodes, kind, context)
        from types import SimpleNamespace

        mapped, ids = [], {}
        for i in range(0, len(nodes), 10):
            batch = nodes[i : i + 10]
            doc = self.rt.gateway.evolve(batch, kind, context)
            representative = batch[0].id
            ids[representative] = doc.source_node_ids
            mapped.append(
                SimpleNamespace(
                    id=representative,
                    summary=doc.summary,
                    cross_domain_dna={"rules": doc.rules},
                    analysis=doc.model_dump(),
                )
            )
        reduced = self.rt.gateway.evolve(mapped, kind, context)
        reduced.source_node_ids = list(
            dict.fromkeys(
                source
                for representative in reduced.source_node_ids
                for source in ids[representative]
            )
        )
        return reduced

    def export_evolution(self, job):
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            record = session.get(EvolutionLog, job.payload["record_id"])
            path = self.rt.vault.write_evolution(record)
            if record.kind == "persona" and record.document.get("creator_id"):
                account = session.get(WatchedAccount, record.document["creator_id"])
                if account and account.persona_json.get("evolution_id") == record.id:
                    self.rt.vault.write_persona(account, record)
            self.rt.queue.succeed(session, job, {"vault_path": path})

    def export_taxonomy(self, job):
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            if session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(74190202)"))
            tree = self.rt.taxonomy.tree()
            from paoding.storage.tables import TaxonomyCategory

            redirects = [
                {"id": c.id, "root": c.root, "merged_into": c.merged_into}
                for c in session.scalars(
                    select(TaxonomyCategory).where(TaxonomyCategory.merged_into.is_not(None))
                )
            ]
            record = session.get(EvolutionLog, job.payload["record_id"])
            path = self.rt.vault.write_taxonomy(record, tree, redirects)
            self.rt.queue.succeed(session, job, {"vault_path": path})

    def notify(self, job):
        self.rt.bots.send(
            job.payload["target"], job.payload["text"], job.id, job.payload.get("card")
        )
        with self.rt.db.transaction() as session:
            self.rt.queue.succeed(session, job, {"sent": True})

    def models_sync(self, job):
        result = self.rt.gateway.sync()
        with self.rt.db.transaction() as session:
            self.rt.queue.succeed(session, job, {"providers": result})

    def bot_ingest(self, job):
        import re
        from pathlib import Path

        from paoding.domain.schemas import IngestRequest
        from paoding.ingestion.normalizer import SUPPORTED_EXTENSIONS

        message = job.payload
        children = []
        urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>]+", message["text"])))[:10]
        for index, url in enumerate(urls):
            result = self.rt.ingest(
                IngestRequest(url=url.rstrip("。），,)"), instruction=message["text"][:2000]),
                f"bot-child:{job.id}:url:{index}",
                message["target"],
            )
            children.append(result["job_id"])
        attachments = []
        for index, attachment in enumerate(message["attachments"]):
            data, mime, name = self.rt.bots.download(message["provider"], attachment)
            suffix = Path(name).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                suffix = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "video/mp4": ".mp4",
                    "audio/ogg": ".ogg",
                    "audio/amr": ".amr",
                    "application/pdf": ".pdf",
                }.get(mime.split(";")[0])
            if not suffix:
                raise ValueError("Unsupported bot attachment format")
            snapshot = self.rt.normalizer._store(data, suffix)
            attachments.append(
                {
                    "local_path": str(snapshot),
                    "source_url": f"{message['provider']}:message:{message['event_id']}:{index}",
                    "title": name,
                    "instruction": message["text"][:2000],
                    "notification": message["target"],
                }
            )
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            if len(attachments) > 1 and all(
                item.get("kind") == "image" for item in message["attachments"]
            ):
                attachments = [
                    {
                        "local_paths": [item["local_path"] for item in attachments],
                        "inline_text": message["text"],
                        "source_url": f"{message['provider']}:message:{message['event_id']}",
                        "platform": message["provider"],
                        "title": "转发图文",
                        "notification": message["target"],
                    }
                ]
            for index, payload in enumerate(attachments):
                child = enqueue(session, "ingest", payload, key=f"bot-child:{job.id}:file:{index}")
                children.append(child.id)
            if not urls and not attachments and message["text"].strip():
                child = enqueue(
                    session,
                    "ingest",
                    {
                        "inline_text": message["text"],
                        "source_url": f"{message['provider']}:message:{message['event_id']}",
                        "platform": message["provider"],
                        "title": "转发图文",
                        "notification": message["target"],
                    },
                    key=f"bot-child:{job.id}:text",
                )
                children.append(child.id)
            self.rt.queue.succeed(session, job, {"enqueued_jobs": children})


def schedule_daily(runtime, date_key):
    with runtime.db.transaction() as session:
        enqueue(
            session, "models_sync", {"date": date_key}, key=f"models-sync:{date_key}", priority=0
        )
        for account in session.scalars(
            select(WatchedAccount).where(WatchedAccount.status == "active")
        ):
            enqueue(
                session, "watch", {"account_id": account.id}, key=f"watch:{account.id}:{date_key}"
            )


def schedule_weekly(runtime, week_key):
    with runtime.db.transaction() as session:
        enqueue(session, "taxonomy", {"week": week_key}, key=f"taxonomy-week:{week_key}")
        enqueue(session, "evolve", {"kind": "trend", "week": week_key}, key=f"trend:{week_key}")
        cutoff = now() - timedelta(days=180)
        session.execute(
            update(AtomElement)
            .where(
                AtomElement.evergreen.is_(False),
                AtomElement.created_at < cutoff,
                (AtomElement.last_used_at.is_(None)) | (AtomElement.last_used_at < cutoff),
                (AtomElement.last_decay_at.is_(None))
                | (AtomElement.last_decay_at < now() - timedelta(days=7)),
            )
            .values(utility_score=AtomElement.utility_score - 0.1, last_decay_at=now())
        )
