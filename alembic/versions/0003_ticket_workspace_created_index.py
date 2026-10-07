"""index tickets by workspace and created_at

Revision ID: 0003_ticket_workspace_created_index
Revises: 0002_workspace_tickets
Create Date: 2026-10-07

"""

from alembic import op

revision: str = "0003_ticket_workspace_created_index"
down_revision: str | None = "0002_workspace_tickets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index("ix_tickets_workspace_id", table_name="tickets")
    op.create_index(
        "ix_tickets_workspace_created_at",
        "tickets",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tickets_workspace_created_at", table_name="tickets")
    op.create_index("ix_tickets_workspace_id", "tickets", ["workspace_id"])
