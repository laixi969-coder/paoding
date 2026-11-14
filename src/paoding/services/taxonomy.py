import hashlib
import json
import unicodedata
from collections import defaultdict

from sqlalchemy import delete, select, text, update

from paoding.core.errors import TransientError
from paoding.domain.schemas import TaxonomyGroup, TaxonomyPlan
from paoding.services.queue import enqueue
from paoding.storage.queries import insert_for
from paoding.storage.tables import (
    AtomElement,
    EvolutionLog,
    NodeCategory,
    TaxonomyAlias,
    TaxonomyCategory,
)


def normalize_tag(tag):
    return unicodedata.normalize("NFKC", tag).strip().casefold()[:120]


class TaxonomyService:
    def __init__(self, runtime):
        self.rt = runtime

    def candidates(self):
        by_tag = defaultdict(set)
        with self.rt.db.transaction() as session:
            for atom in session.scalars(select(AtomElement)):
                for tag in atom.tags:
                    if normalize_tag(tag):
                        by_tag[(atom.atom_type, normalize_tag(tag))].add(atom.node_id)
        return by_tag

    def plan(self, candidates):
        minimum = self.rt.settings.taxonomy_min_sources
        if self.rt.settings.demo_mode:
            return TaxonomyPlan(
                groups=[
                    TaxonomyGroup(
                        name=tag[:80],
                        parent=root,
                        aliases=[tag],
                        reason="DEMO: exact-tag category, no semantic model merge",
                        source_node_ids=sorted(ids)[:200],
                    )
                    for (root, tag), ids in candidates.items()
                    if len(ids) >= minimum
                ][:30]
            )
        groups = []
        # Keep each call bounded; root components never merge across their different semantics.
        for root in ("hook", "visual", "storyboard", "sop"):
            rows = [
                (tag, ids)
                for (kind, tag), ids in candidates.items()
                if kind == root and len(ids) >= 2
            ]
            rows.sort(key=lambda row: (-len(row[1]), row[0]))
            for offset in range(0, len(rows), 40):
                batch = rows[offset : offset + 40]
                if len(set().union(*(ids for _, ids in batch))) < minimum:
                    continue
                plan, _ = self.rt.gateway.generate(
                    TaxonomyPlan,
                    json.dumps(
                        {
                            "task": "合并同义原子标签，生成有证据的二级分类；不相关的标签不能合并。不足阈值留观察池。",
                            "root": root,
                            "minimum_distinct_sources": minimum,
                            "candidates": [
                                {"tag": tag, "source_node_ids": sorted(ids)[:200]}
                                for tag, ids in batch
                            ],
                            "rules": "aliases只能来自输入标签；parent必须匹配root；引用必须是真实输入节点。",
                        },
                        ensure_ascii=False,
                    ),
                )
                groups.extend(plan.groups)
        # TaxonomyPlan is bounded per model call; an accumulated plan may have more groups.
        return groups

    def validate(self, plan, candidates):
        groups = plan.groups if isinstance(plan, TaxonomyPlan) else plan
        used = set()
        for group in groups:
            aliases = list(dict.fromkeys(normalize_tag(alias) for alias in group.aliases))
            if not aliases or any((group.parent, alias) not in candidates for alias in aliases):
                raise TransientError("Taxonomy plan contains unobserved aliases")
            keys = {(group.parent, alias) for alias in aliases}
            if keys & used:
                raise TransientError("Taxonomy plan assigns the same alias to multiple categories")
            used.update(keys)
            eligible = set().union(*(candidates[key] for key in keys))
            references = set(group.source_node_ids)
            if (
                not references <= eligible
                or len(references) < self.rt.settings.taxonomy_min_sources
            ):
                raise TransientError(
                    "Taxonomy category lacks enough distinct, valid source references"
                )
            group.aliases = aliases
        return groups

    def attach_node(self, session, node_id, atom_type, tags):
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(74190202)"))
        ids = session.scalars(
            select(TaxonomyAlias.category_id).where(
                TaxonomyAlias.root == atom_type,
                TaxonomyAlias.alias.in_([normalize_tag(tag) for tag in tags]),
            )
        )
        for category_id in set(ids):
            session.execute(
                insert_for(session, NodeCategory)
                .values(node_id=node_id, category_id=category_id)
                .on_conflict_do_nothing()
            )

    def apply(self, job, groups, candidates):
        changes = []
        with self.rt.db.transaction() as session:
            self.rt.queue.guard(session, job)
            if session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(74190202)"))
            for group in groups:
                key = hashlib.sha256(
                    f"{group.parent}:{normalize_tag(group.name)}".encode()
                ).hexdigest()
                category = session.scalar(
                    select(TaxonomyCategory).where(TaxonomyCategory.canonical_key == key)
                )
                while category and category.merged_into:
                    category = session.get(TaxonomyCategory, category.merged_into)
                if not category:
                    category = TaxonomyCategory(
                        canonical_key=key, name=group.name, root=group.parent
                    )
                    session.add(category)
                    session.flush()
                existing = set(
                    session.scalars(
                        select(TaxonomyAlias.category_id).where(
                            TaxonomyAlias.root == group.parent,
                            TaxonomyAlias.alias.in_(group.aliases),
                        )
                    )
                )
                merged = []
                for old_id in existing - {category.id}:
                    old = session.get(TaxonomyCategory, old_id)
                    if not old or old.root != category.root:
                        raise TransientError("Taxonomy merge crossed component roots")
                    for node_id in session.scalars(
                        select(NodeCategory.node_id).where(NodeCategory.category_id == old_id)
                    ):
                        session.execute(
                            insert_for(session, NodeCategory)
                            .values(node_id=node_id, category_id=category.id)
                            .on_conflict_do_nothing()
                        )
                    session.execute(delete(NodeCategory).where(NodeCategory.category_id == old_id))
                    session.execute(
                        update(TaxonomyAlias)
                        .where(TaxonomyAlias.category_id == old_id)
                        .values(category_id=category.id)
                    )
                    old.merged_into = category.id
                    merged.append(old_id)
                for alias in group.aliases:
                    stmt = insert_for(session, TaxonomyAlias).values(
                        root=group.parent, alias=alias, category_id=category.id
                    )
                    session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["root", "alias"], set_={"category_id": category.id}
                        )
                    )
                    for node_id in candidates[(group.parent, alias)]:
                        session.execute(
                            insert_for(session, NodeCategory)
                            .values(node_id=node_id, category_id=category.id)
                            .on_conflict_do_nothing()
                        )
                changes.append(
                    {**group.model_dump(), "category_id": category.id, "merged_categories": merged}
                )
            document = {
                "title": "分类树自动演化",
                "summary": f"验证并应用 {len(changes)} 项分类变更",
                "rules": [c["reason"] for c in changes] or ["没有达到阈值的新类别"],
                "tags": ["taxonomy"],
                "source_node_ids": list(
                    dict.fromkeys(i for c in changes for i in c["source_node_ids"])
                ),
                "changes": changes,
                "demo": self.rt.settings.demo_mode,
            }
            record = EvolutionLog(job_id=job.id, kind="taxonomy", document=document)
            session.add(record)
            session.flush()
            export = enqueue(session, "export_taxonomy", {"record_id": record.id}, max_attempts=5)
            self.rt.queue.succeed(
                session,
                job,
                {
                    "evolution_id": record.id,
                    "export_job_id": export.id,
                    "category_changes": len(changes),
                },
            )

    def run(self, job):
        candidates = self.candidates()
        self.apply(job, self.validate(self.plan(candidates), candidates), candidates)

    def tree(self):
        with self.rt.db.transaction() as session:
            result = []
            for category in session.scalars(
                select(TaxonomyCategory)
                .where(TaxonomyCategory.merged_into.is_(None))
                .order_by(TaxonomyCategory.root, TaxonomyCategory.name)
            ):
                aliases = list(
                    session.scalars(
                        select(TaxonomyAlias.alias).where(TaxonomyAlias.category_id == category.id)
                    )
                )
                ids = list(
                    session.scalars(
                        select(NodeCategory.node_id).where(NodeCategory.category_id == category.id)
                    )
                )
                result.append(
                    {
                        "id": category.id,
                        "name": category.name,
                        "root": category.root,
                        "aliases": aliases,
                        "source_node_ids": ids,
                        "source_count": len(ids),
                    }
                )
            return result
