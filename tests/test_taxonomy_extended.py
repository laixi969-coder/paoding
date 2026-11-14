import pytest
from sqlalchemy import select

from paoding.core.errors import TransientError
from paoding.domain.schemas import ANALYSIS_TYPES, TaxonomyGroup, TaxonomyPlan
from paoding.models.demo import demo_analysis
from paoding.services.queue import enqueue
from paoding.services.worker import Worker
from paoding.storage.tables import EvolutionLog, KnowledgeNode, TaxonomyCategory


def seed(rt, source, monkeypatch):
    rt.settings.taxonomy_min_sources = 1000

    def analyze(content, instruction):
        result = demo_analysis(content)
        result["tags"] = [instruction.split(":")[0]]
        return ANALYSIS_TYPES[content.modality].model_validate(result), {"demo": True}

    monkeypatch.setattr(rt.gateway, "analyze", analyze)
    for tag in ["留白", "负空间"]:
        for i in range(2):
            rt.ingest_file(source, f"{tag}:{i}")
    worker = Worker(rt)
    while worker.run_once():
        pass
    rt.settings.taxonomy_min_sources = 2
    return rt.taxonomy.candidates()


def apply(rt, plan, candidates, key):
    with rt.db.transaction() as session:
        enqueue(session, "taxonomy", {"test": key})
    job = rt.queue.claim()
    assert job.kind == "taxonomy"
    rt.taxonomy.apply(job, rt.taxonomy.validate(plan, candidates), candidates)
    worker = Worker(rt)
    while worker.run_once():
        pass


def test_taxonomy_merge_keeps_aliases_sources_history_and_redirects(rt, source, monkeypatch):
    candidates = seed(rt, source, monkeypatch)
    groups = [
        TaxonomyGroup(
            name=tag,
            parent=root,
            aliases=[tag],
            reason="观察到多次使用",
            source_node_ids=sorted(ids),
        )
        for (root, tag), ids in candidates.items()
    ]
    apply(rt, TaxonomyPlan(groups=groups), candidates, "first")
    before = rt.taxonomy.tree()
    assert len(before) == 2
    merged = TaxonomyGroup(
        name="空间层次",
        parent="sop",
        aliases=["留白", "负空间"],
        reason="两种表述描述同一规律",
        source_node_ids=sorted(set().union(*candidates.values())),
    )
    apply(rt, TaxonomyPlan(groups=[merged]), candidates, "merge")
    after = rt.taxonomy.tree()
    assert len(after) == 1 and after[0]["source_count"] == 4
    assert set(after[0]["aliases"]) == {"留白", "负空间"}
    assert len(rt.library.fetch_atoms(tags=["留白"])) == 4
    assert len(rt.library.fetch_atoms(category=before[0]["id"])) == 4
    with rt.db.transaction() as session:
        assert all(
            node.analysis["tags"] in [["留白"], ["负空间"]]
            for node in session.scalars(select(KnowledgeNode))
        )
        assert session.get(TaxonomyCategory, before[0]["id"]).merged_into == after[0]["id"]
        logs = session.scalars(select(EvolutionLog).where(EvolutionLog.kind == "taxonomy")).all()
        assert len(logs) == 2
        assert len(logs[-1].document["changes"][0]["merged_categories"]) == 2
    index = rt.settings.vault_dir / "00_Meta_Playbooks_跨界元规则" / "00_分类索引.md"
    assert "空间层次" in index.read_text()
    old_file = index.parent / "sop" / before[0]["id"] / "index.md"
    assert "已合并" in old_file.read_text()


@pytest.mark.parametrize("invalid", ["alias", "reference", "count", "double"])
def test_taxonomy_rejects_unsupported_model_proposals(rt, source, monkeypatch, invalid):
    candidates = seed(rt, source, monkeypatch)
    refs = sorted(candidates[("sop", "留白")])
    group = TaxonomyGroup(
        name="标签",
        parent="sop",
        aliases=["missing" if invalid == "alias" else "留白"],
        reason="reason",
        source_node_ids=["not-a-node"]
        if invalid == "reference"
        else refs[:1]
        if invalid == "count"
        else refs,
    )
    plan = TaxonomyPlan(groups=[group, group] if invalid == "double" else [group])
    with pytest.raises(TransientError):
        rt.taxonomy.validate(plan, candidates)
    assert rt.taxonomy.tree() == []


def test_new_nodes_attach_to_existing_category(rt, source, monkeypatch):
    candidates = seed(rt, source, monkeypatch)
    group = TaxonomyGroup(
        name="留白",
        parent="sop",
        aliases=["留白"],
        reason="stable category",
        source_node_ids=sorted(candidates[("sop", "留白")]),
    )
    apply(rt, TaxonomyPlan(groups=[group]), candidates, "first")
    rt.settings.taxonomy_min_sources = 1000
    rt.ingest_file(source, "留白:3")
    worker = Worker(rt)
    while worker.run_once():
        pass
    assert rt.taxonomy.tree()[0]["source_count"] == 3
