"""Extend upgrade waitlist intent tracking fields

Revision ID: 20260510_0057
Revises: 20260510_0056
Create Date: 2026-05-11

Production deploy target: PostgreSQL. This migration uses PostgreSQL-style
duplicate cleanup and constraint DDL; local SQLite databases should be rebuilt
from metadata or handled with a SQLite-specific batch migration if needed.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260510_0057"
down_revision = "20260510_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "upgrade_waitlist_entries",
        sa.Column("source", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "upgrade_waitlist_entries",
        sa.Column("feature_interest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "upgrade_waitlist_entries",
        sa.Column("cta_context", sa.String(length=96), nullable=True),
    )

    op.execute(
        "UPDATE upgrade_waitlist_entries "
        "SET source = 'pricing_page' "
        "WHERE source IS NULL"
    )
    op.execute(
        "UPDATE upgrade_waitlist_entries "
        "SET feature_interest = CASE "
        "    WHEN COALESCE(tier_intent, '') = 'pro' THEN 'multi_timeframe_replay' "
        "    ELSE 'advanced_replay' "
        "END "
        "WHERE feature_interest IS NULL"
    )

    op.alter_column("upgrade_waitlist_entries", "source", nullable=False)
    op.alter_column("upgrade_waitlist_entries", "feature_interest", nullable=False)

    # Keep earliest row for any pre-existing duplicates before adding constraint.
    op.execute(
        "DELETE FROM upgrade_waitlist_entries newer "
        "USING upgrade_waitlist_entries older "
        "WHERE newer.id > older.id "
        "  AND newer.email = older.email "
        "  AND COALESCE(newer.tier_intent, '') = COALESCE(older.tier_intent, '') "
        "  AND newer.source = older.source "
        "  AND newer.feature_interest = older.feature_interest"
    )

    op.create_index(
        "ix_upgrade_waitlist_entries_source",
        "upgrade_waitlist_entries",
        ["source"],
    )
    op.create_index(
        "ix_upgrade_waitlist_entries_feature_interest",
        "upgrade_waitlist_entries",
        ["feature_interest"],
    )
    op.create_unique_constraint(
        "uq_upgrade_waitlist_email_tier_source_feature",
        "upgrade_waitlist_entries",
        ["email", "tier_intent", "source", "feature_interest"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_upgrade_waitlist_email_tier_source_feature",
        "upgrade_waitlist_entries",
        type_="unique",
    )
    op.drop_index("ix_upgrade_waitlist_entries_feature_interest", table_name="upgrade_waitlist_entries")
    op.drop_index("ix_upgrade_waitlist_entries_source", table_name="upgrade_waitlist_entries")
    op.drop_column("upgrade_waitlist_entries", "cta_context")
    op.drop_column("upgrade_waitlist_entries", "feature_interest")
    op.drop_column("upgrade_waitlist_entries", "source")
