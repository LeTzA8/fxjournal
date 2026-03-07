"""add signup email allowlist table

Revision ID: 20260307_0003
Revises: 20260307_0002
Create Date: 2026-03-07 01:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260307_0003"
down_revision = "20260307_0002"
branch_labels = None
depends_on = None


DEFAULT_ALLOWED_DOMAINS = [
    ("gmail.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("outlook.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("hotmail.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("live.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("msn.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("yahoo.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("yahoo.co.uk", "Seeded from legacy hardcoded signup allowlist", True),
    ("yahoo.ca", "Seeded from legacy hardcoded signup allowlist", True),
    ("ymail.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("icloud.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("me.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("mac.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("aol.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("proton.me", "Seeded from legacy hardcoded signup allowlist", True),
    ("protonmail.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("pm.me", "Seeded from legacy hardcoded signup allowlist", True),
    ("gmx.com", "Seeded from legacy hardcoded signup allowlist", True),
    ("mail.com", "Seeded from legacy hardcoded signup allowlist", True),
]


def upgrade() -> None:
    op.create_table(
        "allowed_signup_email_domains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain"),
    )
    op.create_index(
        op.f("ix_allowed_signup_email_domains_domain"),
        "allowed_signup_email_domains",
        ["domain"],
        unique=False,
    )

    allowlist = sa.table(
        "allowed_signup_email_domains",
        sa.column("domain", sa.String(length=255)),
        sa.column("notes", sa.String(length=255)),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        allowlist,
        [
            {"domain": domain, "notes": notes, "is_active": is_active}
            for domain, notes, is_active in DEFAULT_ALLOWED_DOMAINS
        ],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_allowed_signup_email_domains_domain"),
        table_name="allowed_signup_email_domains",
    )
    op.drop_table("allowed_signup_email_domains")
