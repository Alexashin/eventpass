"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

userrole = sa.Enum("OWNER", "ADMIN", "CONTROLLER", name="userrole")
ticketstatus = sa.Enum("ACTIVE", "USED", "CANCELLED", name="ticketstatus")
ticketsource = sa.Enum("MANUAL", "TELEGRAM", name="ticketsource")
purchasestatus = sa.Enum("PENDING", "APPROVED", "REJECTED", name="purchasestatus")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", userrole, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "purchases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guest_name", sa.String(length=200), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=40), nullable=False),
        sa.Column("telegram_username", sa.String(length=120), nullable=True),
        sa.Column("telegram_display_name", sa.String(length=200), nullable=True),
        sa.Column("receipt_path", sa.String(length=500), nullable=False),
        sa.Column("receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("receipt_dhash", sa.String(length=32), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("parsed_amount", sa.Integer(), nullable=True),
        sa.Column("parsed_datetime", sa.String(length=100), nullable=True),
        sa.Column("parsed_operation_id", sa.String(length=160), nullable=True),
        sa.Column("checks_text", sa.Text(), nullable=True),
        sa.Column("status", purchasestatus, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by_label", sa.String(length=120), nullable=True),
    )
    op.create_index("ix_purchases_guest_name", "purchases", ["guest_name"], unique=False)
    op.create_index("ix_purchases_telegram_user_id", "purchases", ["telegram_user_id"], unique=False)
    op.create_index("ix_purchases_receipt_sha256", "purchases", ["receipt_sha256"], unique=False)
    op.create_index("ix_purchases_parsed_operation_id", "purchases", ["parsed_operation_id"], unique=False)
    op.create_index("ix_purchases_status", "purchases", ["status"], unique=False)

    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("number", sa.String(length=32), nullable=True),
        sa.Column("guest_name", sa.String(length=200), nullable=False),
        sa.Column("token", sa.String(length=120), nullable=False),
        sa.Column("status", ticketstatus, nullable=False),
        sa.Column("source", ticketsource, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("used_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("used_by_label", sa.String(length=120), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("purchase_id", sa.Integer(), sa.ForeignKey("purchases.id"), nullable=True),
        sa.UniqueConstraint("purchase_id"),
    )
    op.create_index("ix_tickets_number", "tickets", ["number"], unique=True)
    op.create_index("ix_tickets_guest_name", "tickets", ["guest_name"], unique=False)
    op.create_index("ix_tickets_token", "tickets", ["token"], unique=True)
    op.create_index("ix_tickets_status", "tickets", ["status"], unique=False)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_label", sa.String(length=120), nullable=True),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.String(length=80), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("tickets")
    op.drop_table("purchases")
    op.drop_table("users")
