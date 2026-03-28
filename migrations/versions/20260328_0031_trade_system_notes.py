"""split system import notes from user trade notes

Revision ID: 20260328_0031
Revises: 20260328_0030
Create Date: 2026-03-28 23:59:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260328_0031"
down_revision = "20260328_0030"
branch_labels = None
depends_on = None


TABLE_NAME = "trades"
SYSTEM_NOTE_COLUMN = "system_trade_note"
KNOWN_SYSTEM_NOTES = (
    "Auto-imported via MT5 sync",
    "Imported from MT5 Positions",
    "Imported from Tradovate Performance CSV",
)


def upgrade() -> None:
    op.add_column(TABLE_NAME, sa.Column(SYSTEM_NOTE_COLUMN, sa.Text(), nullable=True))

    for note_text in KNOWN_SYSTEM_NOTES:
        op.execute(
            sa.text(
                f"""
                UPDATE {TABLE_NAME}
                SET {SYSTEM_NOTE_COLUMN} = trade_note,
                    trade_note = NULL
                WHERE trade_note = :note_text
                """
            ).bindparams(note_text=note_text)
        )


def downgrade() -> None:
    for note_text in KNOWN_SYSTEM_NOTES:
        op.execute(
            sa.text(
                f"""
                UPDATE {TABLE_NAME}
                SET trade_note = {SYSTEM_NOTE_COLUMN}
                WHERE trade_note IS NULL
                  AND {SYSTEM_NOTE_COLUMN} = :note_text
                """
            ).bindparams(note_text=note_text)
        )

    op.drop_column(TABLE_NAME, SYSTEM_NOTE_COLUMN)
