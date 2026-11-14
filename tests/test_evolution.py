from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from paoding.domain.schemas import CollectorItem, CollectorPage, IngestRequest
from paoding.services.library import rank_trends
from paoding.services.queue import enqueue
from paoding.services.worker import Worker, schedule_daily, schedule_weekly
from paoding.storage.tables import (
    AtomElement,
    EvolutionLog,
    Job,
    KnowledgeNode,
    WatchedAccount,
    now,
)


def test_trend_percentile_is_platform_local_and_missing_metrics_excluded():
    nodes = [SimpleNamespace(platform="a", metrics={"engagement_rate": i}) for i in range(20)]
    nodes += [
        SimpleNamespace(platform="b", metrics={"engagement_rate": 0.01}),
        SimpleNamespace(platform="c", metrics={}),
    ]
    ranked = rank_trends(nodes)
    assert len(ranked) == 3
    assert {n.metrics["engagement_rate"] for n in ranked} == {19, 18, 0.01}


def test_watcher_cold_start_cursor_and_daily_dedupe(rt, monkeypatch):
    result = rt.ingest(IngestRequest(url="https://youtube.com/@demo"))
    calls = []

    def items(url, cursor, cold_start):
        calls.append((cursor, cold_start))
        return CollectorPage(
            items=[CollectorItem(url="https://youtube.com/watch?v=1", metrics={"likes": 10})],
            next_cursor="c1",
        )

    monkeypatch.setattr(rt.normalizer.collector, "account_items", items)
    assert Worker(rt).run_once()
    with rt.db.transaction() as session:
        account = session.get(WatchedAccount, result["account_id"])
        assert account.cold_started and account.last_cursor == "c1"
        assert (
            session.scalar(select(func.count()).select_from(Job).where(Job.kind == "ingest")) == 1
        )
    schedule_daily(rt, "2026-11-14")
    schedule_daily(rt, "2026-11-14")
    with rt.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "watch")) == 2
    assert calls == [(None, True)]


def test_failed_watcher_does_not_advance_cursor(rt, monkeypatch):
    from paoding.core.errors import TransientError

    result = rt.ingest(IngestRequest(url="https://youtube.com/@demo"))
    monkeypatch.setattr(
        rt.normalizer.collector,
        "account_items",
        lambda *a: (_ for _ in ()).throw(TransientError("offline")),
    )
    Worker(rt).run_once()
    with rt.db.transaction() as session:
        assert session.get(WatchedAccount, result["account_id"]).last_cursor is None
        assert session.get(Job, result["job_id"]).status == "retrying"


def test_stale_continuation_cannot_regress_cursor(rt, monkeypatch):
    with rt.db.transaction() as session:
        account = WatchedAccount(
            profile_url="https://x.com/demo",
            platform="x",
            name="demo",
            last_cursor="new",
            cold_started=True,
        )
        session.add(account)
        session.flush()
        account_id = account.id
        job = enqueue(session, "watch", {"account_id": account_id, "cursor": "old"})
        job_id = job.id
    monkeypatch.setattr(
        rt.normalizer.collector,
        "account_items",
        lambda *a: (_ for _ in ()).throw(AssertionError("Must not call collector")),
    )
    Worker(rt).run_once()
    with rt.db.transaction() as session:
        assert session.get(Job, job_id).result == {"skipped": "stale_pagination"}
        assert session.get(WatchedAccount, account_id).last_cursor == "new"


def test_tag_trends_without_baseline_do_not_invent_growth():
    from paoding.services.library import compare_tags

    current = [SimpleNamespace(analysis={"tags": ["new", "new"]})]
    assert compare_tags(current, [])[0]["share_change"] is None
    prior = [SimpleNamespace(analysis={"tags": ["old"]})]
    assert compare_tags(current, prior)[0]["share_change"] == 1
    assert compare_tags(current, prior)[0]["current_count"] == 1


def test_persona_threshold_and_evergreen_decay(rt, source):
    with rt.db.transaction() as session:
        account = WatchedAccount(profile_url="https://x.com/demo", platform="x", name="demo")
        session.add(account)
        session.flush()
        account_id = account.id
    worker = Worker(rt)
    for i in range(5):
        result = rt.ingest_file(source, instruction=f"版本{i}")
        with rt.db.transaction() as session:
            job = session.get(Job, result["job_id"])
            job.payload = {**job.payload, "creator_id": account_id}
    for _ in range(50):
        if not worker.run_once():
            break
    with rt.db.transaction() as session:
        account = session.get(WatchedAccount, account_id)
        assert account.persona_node_count == 5
        assert account.persona_json["system_prompt"]
        assert session.scalar(select(func.count()).select_from(EvolutionLog)) == 1
        atoms = session.scalars(select(AtomElement)).all()
        for atom in atoms:
            atom.created_at = now() - timedelta(days=190)
        atoms[0].evergreen = True
        evergreen_id = atoms[0].id
    schedule_weekly(rt, "2026-W46")
    schedule_weekly(rt, "2026-W46")
    with rt.db.transaction() as session:
        for atom in session.scalars(select(AtomElement)):
            assert atom.utility_score == (0 if atom.id == evergreen_id else -0.1)


def test_playbook_map_reduce_preserves_source_references(rt, source):
    worker = Worker(rt)
    for i in range(21):
        rt.ingest_file(source, instruction=f"版本{i}")
    for _ in range(100):
        if not worker.run_once():
            break
    with rt.db.transaction() as session:
        playbooks = session.scalars(
            select(EvolutionLog).where(EvolutionLog.kind == "playbook")
        ).all()
        assert len(playbooks) == 2  # Both shared demo tags crossed the threshold.
        for playbook in playbooks:
            assert len(playbook.document["source_node_ids"]) >= 20
            assert all(
                session.get(KnowledgeNode, node_id)
                for node_id in playbook.document["source_node_ids"]
            )
    assert rt.library.query_playbook("demo", "xiaohongshu")
