import hashlib
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from paoding.core.errors import ConfigurationError, Conflict
from paoding.storage.tables import (
    AtomElement,
    EvolutionLog,
    KnowledgeNode,
    NodeCategory,
    TaxonomyAlias,
    TaxonomyCategory,
    UtilityFeedback,
    WatchedAccount,
    now,
)

TRANSFER_FIELDS = {
    "douyin": "transfer_to_short_video",
    "wechat_video": "transfer_to_short_video",
    "youtube": "transfer_to_short_video",
    "short_video": "transfer_to_short_video",
    "xiaohongshu": "transfer_to_xiaohongshu",
    "wechat": "transfer_to_wechat_article",
}
ATOM_TYPES = {"hook", "visual", "storyboard", "sop"}


class Library:
    def __init__(self, db, gateway):
        self.db, self.gateway = db, gateway

    def fetch_atoms(
        self, atom_type=None, tags=None, target_platform=None, limit=20, query=None, category=None
    ):
        if atom_type and atom_type not in ATOM_TYPES:
            raise ValueError("Unknown atom_type")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if target_platform and target_platform not in TRANSFER_FIELDS:
            raise ValueError("Unsupported target platform")
        with self.db.transaction() as session:
            stmt = select(AtomElement, KnowledgeNode).join(
                KnowledgeNode, AtomElement.node_id == KnowledgeNode.id
            )
            if atom_type:
                stmt = stmt.where(AtomElement.atom_type == atom_type)
            if category:
                cat = session.get(TaxonomyCategory, category)
                while cat and cat.merged_into:
                    cat = session.get(TaxonomyCategory, cat.merged_into)
                if not cat:
                    raise LookupError("Category not found")
                stmt = stmt.where(
                    AtomElement.atom_type == cat.root,
                    AtomElement.node_id.in_(
                        select(NodeCategory.node_id).where(NodeCategory.category_id == cat.id)
                    ),
                )
            # Portable SQL tag matching; escape LIKE metacharacters. Postgres uses native JSONB containment.
            for tag in tags or []:
                from paoding.services.taxonomy import normalize_tag
                from paoding.storage.queries import has_tag

                aliases = select(TaxonomyAlias.category_id).where(
                    TaxonomyAlias.alias == normalize_tag(tag)
                )
                if atom_type:
                    aliases = aliases.where(TaxonomyAlias.root == atom_type)
                category_ids = list(session.scalars(aliases))
                clauses = [has_tag(session, AtomElement.tags, tag)]
                for id_ in category_ids:
                    cat = session.get(TaxonomyCategory, id_)
                    clauses.append(
                        (AtomElement.atom_type == cat.root)
                        & AtomElement.node_id.in_(
                            select(NodeCategory.node_id).where(NodeCategory.category_id == id_)
                        )
                    )
                stmt = stmt.where(or_(*clauses))
            mode = "filters"
            if query:
                vector = self.gateway.embed(query)
                if vector is not None:
                    if session.bind.dialect.name != "postgresql":
                        raise ConfigurationError("Semantic search requires PostgreSQL/pgvector")
                    stmt = stmt.where(
                        AtomElement.embedding.is_not(None),
                        AtomElement.embedding_model == self.gateway.settings.embedding_model,
                    )
                    from pgvector.sqlalchemy import Vector
                    from sqlalchemy import cast

                    stmt = stmt.order_by(
                        cast(AtomElement.embedding, Vector(1536)).cosine_distance(vector)
                    )
                    mode = "semantic"
                else:
                    stmt = stmt.where(AtomElement.content.icontains(query, autoescape=True))
                    mode = "keyword"
            stmt = stmt.order_by(
                AtomElement.utility_score.desc(), AtomElement.created_at.desc()
            ).limit(limit)
            return [
                {
                    "id": atom.id,
                    "node_id": node.id,
                    "atom_type": atom.atom_type,
                    "tags": atom.tags,
                    "content": atom.content,
                    "utility_score": atom.utility_score,
                    "source_url": node.source_url,
                    "retrieval_mode": mode,
                    "transfer_advice": node.cross_domain_dna.get(
                        TRANSFER_FIELDS.get(target_platform, "universal_principle")
                    ),
                    "demo": node.provenance.get("demo", False),
                }
                for atom, node in session.execute(stmt)
            ]

    def query_playbook(self, domain, target_platform=None):
        if target_platform and target_platform not in TRANSFER_FIELDS:
            raise ValueError("Unsupported target platform")
        with self.db.transaction() as session:
            records = session.scalars(
                select(EvolutionLog)
                .where(EvolutionLog.kind == "playbook")
                .order_by(EvolutionLog.created_at.desc())
            ).all()
            matches = []
            for record in records:
                document = record.document
                if (
                    domain.casefold()
                    not in (document["title"] + " ".join(document.get("tags", []))).casefold()
                ):
                    continue
                advice = []
                for node_id in document["source_node_ids"]:
                    node = session.get(KnowledgeNode, node_id)
                    if node:
                        advice.append(
                            node.cross_domain_dna.get(
                                TRANSFER_FIELDS.get(target_platform, "universal_principle")
                            )
                        )
                matches.append(
                    {
                        "id": record.id,
                        **document,
                        "target_platform": target_platform,
                        "transfer_advice": list(dict.fromkeys(advice)),
                    }
                )
                if len(matches) == 20:
                    break
            return matches

    def scan_trends(self, days=7, platforms=None):
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        with self.db.transaction() as session:
            boundary = now() - timedelta(days=days)
            published = func.coalesce(KnowledgeNode.published_at, KnowledgeNode.created_at)
            stmt = select(KnowledgeNode).where(
                published >= boundary,
                KnowledgeNode.creator_id.is_not(None),
            )
            if platforms:
                stmt = stmt.where(KnowledgeNode.platform.in_(platforms))
            nodes = session.scalars(stmt).all()
            prior_stmt = select(KnowledgeNode).where(
                published < boundary,
                published >= boundary - timedelta(days=days),
                KnowledgeNode.creator_id.is_not(None),
            )
            if platforms:
                prior_stmt = prior_stmt.where(KnowledgeNode.platform.in_(platforms))
            prior = session.scalars(prior_stmt).all()
            ranked = rank_trends(nodes)
            reports = session.scalars(
                select(EvolutionLog)
                .where(
                    EvolutionLog.kind == "trend",
                    EvolutionLog.created_at >= now() - timedelta(days=days),
                )
                .order_by(EvolutionLog.created_at.desc())
                .limit(10)
            ).all()
            return {
                "days": days,
                "sample_size": len(nodes),
                "ranked_sample_size": len(ranked),
                "previous_sample_size": len(prior),
                "tag_changes": compare_tags(nodes, prior),
                "ranking": "Within-platform top 10% by engagement_rate; missing rates excluded",
                "items": [
                    {
                        "node_id": n.id,
                        "title": n.title,
                        "platform": n.platform,
                        "engagement_rate": n.metrics["engagement_rate"],
                        "summary": n.summary,
                    }
                    for n in ranked
                ],
                "reports": [
                    {"id": r.id, **r.document}
                    for r in reports
                    if not platforms
                    or any(
                        session.get(KnowledgeNode, i).platform in platforms
                        for i in r.document["source_node_ids"]
                        if session.get(KnowledgeNode, i)
                    )
                ],
            }

    def summon_creator(self, name):
        with self.db.transaction() as session:
            accounts = session.scalars(
                select(WatchedAccount).where(
                    (WatchedAccount.name == name) | (WatchedAccount.id == name)
                )
            ).all()
            if not accounts:
                raise LookupError("Creator not found")
            if len(accounts) > 1:
                raise Conflict("Several creators share this name; use the account UUID")
            account = accounts[0]
            examples = session.scalars(
                select(KnowledgeNode)
                .where(KnowledgeNode.creator_id == account.id)
                .order_by(KnowledgeNode.utility_score.desc())
                .limit(3)
            ).all()
            return {
                "id": account.id,
                "name": account.name,
                "status": account.status,
                "persona": account.persona_json,
                "persona_node_count": account.persona_node_count,
                "examples": [
                    {"id": n.id, "title": n.title, "summary": n.summary} for n in examples
                ],
            }

    def feedback(self, atom_id, adopted, event_id):
        id_ = hashlib.sha256(event_id.encode()).hexdigest()
        with self.db.transaction() as session:
            atom = session.get(AtomElement, atom_id)
            if not atom:
                raise LookupError("Atom not found")
            old = session.get(UtilityFeedback, id_)
            if old:
                if old.atom_id != atom_id or old.adopted != adopted:
                    raise Conflict("Feedback event_id already belongs to another event")
                return {"atom_id": atom_id, "utility_score": atom.utility_score, "duplicate": True}
            try:
                with session.begin_nested():
                    session.add(UtilityFeedback(id=id_, atom_id=atom_id, adopted=adopted))
                    session.flush()
            except IntegrityError:
                old = session.get(UtilityFeedback, id_)
                if not old or old.atom_id != atom_id or old.adopted != adopted:
                    raise Conflict("Feedback event_id conflict") from None
                session.refresh(atom)
                return {"atom_id": atom_id, "utility_score": atom.utility_score, "duplicate": True}
            delta = 1.0 if adopted else -0.25
            session.execute(
                update(AtomElement)
                .where(AtomElement.id == atom_id)
                .values(utility_score=AtomElement.utility_score + delta, last_used_at=now())
            )
            session.execute(
                update(KnowledgeNode)
                .where(KnowledgeNode.id == atom.node_id)
                .values(utility_score=KnowledgeNode.utility_score + delta)
            )
            session.refresh(atom)
            return {"atom_id": atom_id, "utility_score": atom.utility_score, "duplicate": False}


def rank_trends(nodes):
    import math

    groups = {}
    for node in nodes:
        rate = node.metrics.get("engagement_rate")
        if isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0:
            groups.setdefault(node.platform, []).append(node)
    result = []
    for group in groups.values():
        result.extend(
            sorted(group, key=lambda n: n.metrics["engagement_rate"], reverse=True)[
                : max(1, math.ceil(len(group) * 0.1))
            ]
        )
    return result


def compare_tags(current, previous):
    from collections import Counter

    current_counts = Counter(tag for n in current for tag in set(n.analysis.get("tags", [])))
    previous_counts = Counter(tag for n in previous for tag in set(n.analysis.get("tags", [])))
    result = []
    for tag, count in current_counts.items():
        current_share = count / len(current)
        previous_share = previous_counts[tag] / len(previous) if previous else 0
        result.append(
            {
                "tag": tag,
                "current_count": count,
                "previous_count": previous_counts[tag],
                "share_change": round(current_share - previous_share, 4) if previous else None,
                "baseline_available": bool(previous),
            }
        )
    return sorted(
        result, key=lambda item: (item["share_change"] or 0, item["current_count"]), reverse=True
    )[:50]
