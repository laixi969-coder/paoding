"""Add auditable taxonomy and source publication timestamps.

Revision ID: 0002_taxonomy
Revises: f4fd69307f82
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_taxonomy"
down_revision = "f4fd69307f82"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "paoding_knowledge_nodes", sa.Column("published_at", sa.DateTime(), nullable=True)
    )
    op.create_index(
        "ix_paoding_knowledge_nodes_published_at", "paoding_knowledge_nodes", ["published_at"]
    )
    op.create_table(
        "paoding_taxonomy_categories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("canonical_key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("root", sa.String(20), nullable=False),
        sa.Column("merged_into", sa.String(36), sa.ForeignKey("paoding_taxonomy_categories.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_paoding_taxonomy_categories_root", "paoding_taxonomy_categories", ["root"])
    op.create_table(
        "paoding_taxonomy_aliases",
        sa.Column("root", sa.String(20), primary_key=True),
        sa.Column("alias", sa.String(120), primary_key=True),
        sa.Column(
            "category_id",
            sa.String(36),
            sa.ForeignKey("paoding_taxonomy_categories.id"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_paoding_taxonomy_aliases_category_id", "paoding_taxonomy_aliases", ["category_id"]
    )
    op.create_table(
        "paoding_node_categories",
        sa.Column(
            "node_id",
            sa.String(36),
            sa.ForeignKey("paoding_knowledge_nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "category_id",
            sa.String(36),
            sa.ForeignKey("paoding_taxonomy_categories.id"),
            primary_key=True,
        ),
    )


def downgrade():
    op.drop_table("paoding_node_categories")
    op.drop_index("ix_paoding_taxonomy_aliases_category_id", table_name="paoding_taxonomy_aliases")
    op.drop_table("paoding_taxonomy_aliases")
    op.drop_index("ix_paoding_taxonomy_categories_root", table_name="paoding_taxonomy_categories")
    op.drop_table("paoding_taxonomy_categories")
    op.drop_index("ix_paoding_knowledge_nodes_published_at", table_name="paoding_knowledge_nodes")
    op.drop_column("paoding_knowledge_nodes", "published_at")
