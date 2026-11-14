from datetime import UTC, datetime
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    # UTC-naive storage avoids SQLite timezone coercion differences; all APIs emit UTC.
    return datetime.now(UTC).replace(tzinfo=None)


def new_id():
    return str(uuid4())


json_type = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "paoding_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(40))
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    payload: Mapped[dict] = mapped_column(json_type)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(json_type)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (Index("ix_jobs_claim", "status", "available_at", "priority"),)


class WatchedAccount(Base):
    __tablename__ = "paoding_watched_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(40))
    profile_url: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(String(300))
    last_cursor: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="active")
    cold_started: Mapped[bool] = mapped_column(Boolean, default=False)
    persona_json: Mapped[dict | None] = mapped_column(json_type)
    persona_node_count: Mapped[int] = mapped_column(Integer, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeNode(Base):
    __tablename__ = "paoding_knowledge_nodes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("paoding_jobs.id"), unique=True)
    creator_id: Mapped[str | None] = mapped_column(
        ForeignKey("paoding_watched_accounts.id"), index=True
    )
    platform: Mapped[str] = mapped_column(String(40), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300))
    modality: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str] = mapped_column(Text)
    analysis: Mapped[dict] = mapped_column(json_type)
    cross_domain_dna: Mapped[dict] = mapped_column(json_type)
    media_paths: Mapped[list] = mapped_column(json_type)
    metrics: Mapped[dict] = mapped_column(json_type, default=dict)
    provenance: Mapped[dict] = mapped_column(json_type)
    utility_score: Mapped[float] = mapped_column(Float, default=0)
    vault_status: Mapped[str] = mapped_column(String(20), default="pending")
    vault_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class AtomElement(Base):
    __tablename__ = "paoding_atom_elements"
    __table_args__ = (
        Index(
            "ix_atoms_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ).ddl_if(dialect="postgresql"),
        Index("ix_atoms_tags_gin", "tags", postgresql_using="gin").ddl_if(dialect="postgresql"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("paoding_knowledge_nodes.id", ondelete="CASCADE"), index=True
    )
    atom_type: Mapped[str] = mapped_column(String(20), index=True)
    tags: Mapped[list] = mapped_column(json_type)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(JSON().with_variant(Vector(1536), "postgresql"))
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    evergreen: Mapped[bool] = mapped_column(Boolean, default=False)
    utility_score: Mapped[float] = mapped_column(Float, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_decay_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class EvolutionLog(Base):
    __tablename__ = "paoding_evolution_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("paoding_jobs.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    document: Mapped[dict] = mapped_column(json_type)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class UtilityFeedback(Base):
    __tablename__ = "paoding_utility_feedback"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    atom_id: Mapped[str] = mapped_column(ForeignKey("paoding_atom_elements.id"))
    adopted: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ModelRecord(Base):
    __tablename__ = "paoding_model_registry"
    id: Mapped[str] = mapped_column(String(250), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model_id: Mapped[str] = mapped_column(String(200))
    health: Mapped[str] = mapped_column(String(30), default="unknown")
    latency_ms: Mapped[float | None] = mapped_column(Float)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TaxonomyCategory(Base):
    __tablename__ = "paoding_taxonomy_categories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    canonical_key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    root: Mapped[str] = mapped_column(String(20), index=True)
    merged_into: Mapped[str | None] = mapped_column(ForeignKey("paoding_taxonomy_categories.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TaxonomyAlias(Base):
    __tablename__ = "paoding_taxonomy_aliases"
    root: Mapped[str] = mapped_column(String(20), primary_key=True)
    alias: Mapped[str] = mapped_column(String(120), primary_key=True)
    category_id: Mapped[str] = mapped_column(
        ForeignKey("paoding_taxonomy_categories.id"), index=True
    )


class NodeCategory(Base):
    __tablename__ = "paoding_node_categories"
    node_id: Mapped[str] = mapped_column(
        ForeignKey("paoding_knowledge_nodes.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("paoding_taxonomy_categories.id"), primary_key=True
    )
