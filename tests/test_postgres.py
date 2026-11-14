import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import migrate_test_database
from sqlalchemy import select, text

from paoding.core.config import Settings
from paoding.domain.schemas import ANALYSIS_TYPES
from paoding.models.demo import demo_analysis
from paoding.services.queue import enqueue
from paoding.services.runtime import Runtime
from paoding.services.worker import Worker
from paoding.storage.tables import AtomElement, Base, Job

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(tmp_path):
    url = os.getenv("PAODING_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set PAODING_TEST_POSTGRES_URL to an empty disposable PostgreSQL database")
    # Require a clearly named test database; this fixture truncates ONLY this engine's tables.
    from sqlalchemy.engine import make_url

    assert "test" in make_url(url).database, "Integration database name must contain 'test'"
    settings = Settings(
        database_url=url,
        data_dir=tmp_path / "data",
        vault_dir=tmp_path / "vault",
        drop_dir=tmp_path / "drop",
        demo_mode=False,
        _env_file=None,
    )
    settings.prepare_directories()
    migrate_test_database(url)
    runtime = Runtime(settings)
    with runtime.db.transaction() as session:
        names = ", ".join(Base.metadata.tables)
        session.execute(text(f"TRUNCATE {names} CASCADE"))
    yield runtime
    runtime.db.engine.dispose()


def test_postgres_migration_pgvector_and_concurrent_claims(pg):
    with pg.db.transaction() as session:
        assert session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        ).scalar()
        for i in range(20):
            enqueue(session, "ingest", {"index": i})
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda _: pg.queue.claim(), range(20)))
    assert len({job.id for job in jobs if job}) == 20
    assert all(job.attempts == 1 for job in jobs)
    with pg.db.transaction() as session:
        column = session.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid='paoding_atom_elements'::regclass AND attname='embedding'"
            )
        ).scalar()
        assert column == "vector(1536)"


def test_postgres_concurrent_enqueue_and_model_health(pg):
    from paoding.models.gateway import Route
    from paoding.storage.tables import ModelRecord

    def add(_):
        with pg.db.transaction() as session:
            return enqueue(session, "ingest", {"url": "https://example.com/one"}).id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(add, range(20)))
    assert len(set(ids)) == 1
    route = Route(provider="test", model="model")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: pg.gateway.health(route, True, 0.1), range(20)))
    with pg.db.transaction() as session:
        assert session.get(ModelRecord, "test:model").health == "healthy"


def test_postgres_full_pipeline_vector_search_and_feedback_races(pg, source, monkeypatch):
    def analyze(content, instruction):
        return ANALYSIS_TYPES[content.modality].model_validate(demo_analysis(content)), {
            "demo": True
        }

    monkeypatch.setattr(pg.gateway, "analyze", analyze)
    pg.ingest_file(source)
    worker = Worker(pg)
    while worker.run_once():
        pass
    with pg.db.transaction() as session:
        atom = session.scalar(select(AtomElement))
        atom.embedding = [1.0] + [0.0] * 1535
        atom.embedding_model = "test-embedding"
        atom_id = atom.id
    pg.settings.embedding_model = "test-embedding"
    monkeypatch.setattr(pg.gateway, "embed", lambda _: [1.0] + [0.0] * 1535)
    result = pg.library.fetch_atoms(tags=["demo"], query="test")
    assert result[0]["id"] == atom_id and result[0]["retrieval_mode"] == "semantic"
    with ThreadPoolExecutor(max_workers=6) as pool:
        feedback = list(
            pool.map(lambda _: pg.library.feedback(atom_id, True, "same-event"), range(12))
        )
    assert sum(not row["duplicate"] for row in feedback) == 1
    with pg.db.transaction() as session:
        assert session.get(AtomElement, atom_id).utility_score == 1
        assert all(j.status == "succeeded" for j in session.scalars(select(Job)))


def test_postgres_taxonomy_migration_links_and_exports(pg, source, monkeypatch):
    from paoding.domain.schemas import TaxonomyGroup, TaxonomyPlan

    pg.settings.taxonomy_min_sources = 1000
    monkeypatch.setattr(
        pg.gateway,
        "analyze",
        lambda content, instruction: (
            ANALYSIS_TYPES[content.modality].model_validate(demo_analysis(content)),
            {"demo": True},
        ),
    )
    pg.ingest_file(source, "one")
    pg.ingest_file(source, "two")
    worker = Worker(pg)
    while worker.run_once():
        pass
    pg.settings.taxonomy_min_sources = 2
    candidates = pg.taxonomy.candidates()
    root, tag = next(iter(candidates))
    plan = TaxonomyPlan(
        groups=[
            TaxonomyGroup(
                name="分类",
                parent=root,
                aliases=[tag],
                reason="多个来源支持",
                source_node_ids=sorted(candidates[(root, tag)]),
            )
        ]
    )
    with pg.db.transaction() as session:
        enqueue(session, "taxonomy", {"manual": "test"})
    job = pg.queue.claim()
    pg.taxonomy.apply(job, pg.taxonomy.validate(plan, candidates), candidates)
    while worker.run_once():
        pass
    tree = pg.taxonomy.tree()
    assert tree[0]["source_count"] == 2
    assert len(pg.library.fetch_atoms(category=tree[0]["id"])) == 2
