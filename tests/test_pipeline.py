from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from paoding.core.errors import Conflict, LeaseLost
from paoding.services.queue import enqueue
from paoding.services.worker import Worker
from paoding.storage.tables import AtomElement, Job, KnowledgeNode, now


def drain(rt):
    worker = Worker(rt)
    for _ in range(100):
        if not worker.run_once():
            return
    raise AssertionError("Worker did not drain")


def test_full_ingest_snapshot_export_retrieval_feedback(rt, source):
    first = rt.ingest_file(source)
    assert rt.ingest_file(source)["job_id"] == first["job_id"]
    source.unlink()  # Queue input survives deletion of the source file.
    drain(rt)
    with rt.db.transaction() as session:
        job = session.get(Job, first["job_id"])
        assert job.status == "succeeded"
        node = session.get(KnowledgeNode, job.result["node_id"])
        assert node.vault_status == "synced"
        assert Path(node.vault_path).exists()
        assert "cross_domain_dna" in Path(node.vault_path).read_text()
        assert node.analysis["evidence"]["basis"] == "demo"
        assert session.scalar(select(func.count()).select_from(KnowledgeNode)) == 1
    atoms = rt.library.fetch_atoms("sop", ["demo"], "xiaohongshu", 10)
    assert len(atoms) == 1
    assert "首图" in atoms[0]["transfer_advice"]
    first_feedback = rt.library.feedback(atoms[0]["id"], True, "event-1")
    assert first_feedback["utility_score"] == 1
    assert rt.library.feedback(atoms[0]["id"], True, "event-1")["duplicate"]
    with pytest.raises(Conflict):
        rt.library.feedback(atoms[0]["id"], False, "event-1")
    assert rt.library.fetch_atoms(tags=["not-found"]) == []
    assert rt.library.fetch_atoms(query="%") == []  # SQL wildcard is literal.


def test_export_failure_is_recoverable_without_duplicate_node(rt, source, monkeypatch):
    result = rt.ingest_file(source)
    worker = Worker(rt)
    assert worker.run_once()
    original = rt.vault.write_node
    monkeypatch.setattr(
        rt.vault, "write_node", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
    )
    assert worker.run_once()
    with rt.db.transaction() as session:
        node = session.scalar(select(KnowledgeNode))
        assert node.vault_status == "pending"
        export = session.scalar(select(Job).where(Job.kind == "export_node"))
        assert export.status == "retrying"
        export.available_at = now() - timedelta(seconds=1)
    monkeypatch.setattr(rt.vault, "write_node", original)
    drain(rt)
    with rt.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(KnowledgeNode)) == 1
        assert session.scalar(select(KnowledgeNode)).vault_status == "synced"
        assert session.get(Job, result["job_id"]).status == "succeeded"


def test_expired_lease_reclaimed_and_stale_writer_fenced(rt):
    with rt.db.transaction() as session:
        queued = enqueue(session, "ingest", {"url": "https://example.com"})
    old = rt.queue.claim()
    with rt.db.transaction() as session:
        session.get(Job, queued.id).lease_until = now() - timedelta(seconds=1)
    current = rt.queue.claim()
    assert old.id == current.id and old.lease_token != current.lease_token
    with pytest.raises(LeaseLost), rt.db.transaction() as session:
        rt.queue.succeed(session, old, {"bad": True})
    with rt.db.transaction() as session:
        rt.queue.succeed(session, current, {"ok": True})
    assert rt.queue.claim() is None


def test_retry_budget_and_priority(rt):
    with rt.db.transaction() as session:
        enqueue(session, "ingest", {"a": 1}, priority=0)
        high = enqueue(session, "ingest", {"b": 2}, priority=2, max_attempts=1)
    claim = rt.queue.claim()
    assert claim.id == high.id
    rt.queue.fail(claim, "network", "retry", True)
    with rt.db.transaction() as session:
        assert session.get(Job, claim.id).status == "failed"
    rt.queue.retry(claim.id)
    assert rt.queue.claim().id == high.id


def test_missing_models_fail_explicitly_not_demo(rt, source, monkeypatch):
    rt.settings.demo_mode = False
    for name in ["DEEPSEEK_API_KEY", "QWEN_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(rt.gateway, "key", lambda p: "")
    result = rt.ingest_file(source)
    Worker(rt).run_once()
    with rt.db.transaction() as session:
        job = session.get(Job, result["job_id"])
        assert job.status == "failed"
        assert job.error_code == "configuration_required"
        assert session.scalar(select(func.count()).select_from(AtomElement)) == 0


def test_pixel_palette_and_image_pipeline(rt, tmp_path):
    from PIL import Image

    image = Image.new("RGB", (100, 100), (255, 0, 0))
    path = tmp_path / "red.png"
    image.save(path)
    result = rt.ingest_file(path)
    drain(rt)
    with rt.db.transaction() as session:
        job = session.get(Job, result["job_id"])
        assert job.status == "succeeded"
        node = session.get(KnowledgeNode, job.result["node_id"])
        assert node.analysis["color_system"]["palette"][0]["hex"] == "#FF0000"
        assert node.analysis["color_system"]["palette"][0]["proportion"] == 1
    assert rt.library.fetch_atoms("visual")
