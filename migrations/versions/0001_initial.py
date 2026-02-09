"""Initial schema for Teleco Churn.

Revision ID: 0001_initial
Revises: 
Create Date: 2026-02-08
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("industry", sa.String(length=120)),
        sa.Column("website", sa.String(length=255)),
        sa.Column("description", sa.Text()),
        sa.Column("contact_email", sa.String(length=255)),
        sa.Column("contact_phone", sa.String(length=50)),
        sa.Column("address", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organization.id"), nullable=False),
        sa.Column("first_name", sa.String(length=120)),
        sa.Column("last_name", sa.String(length=120)),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=120)),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_user_org_id", "user", ["org_id"])

    op.create_table(
        "prediction_record",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organization.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("client_reference", sa.String(length=120)),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("input_data", sa.Text(), nullable=False),
        sa.Column("probability", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=50), nullable=False),
        sa.Column("payment_method", sa.String(length=120)),
        sa.Column("contract", sa.String(length=120)),
        sa.Column("internet_service", sa.String(length=120)),
        sa.Column("tenure_months", sa.Integer()),
        sa.Column("monthly_charges", sa.Float()),
        sa.Column("risk_level", sa.String(length=20)),
        sa.Column("notes", sa.Text()),
        sa.Column("input_hash", sa.String(length=64)),
        sa.Column("batch_id", sa.String(length=36)),
        sa.Column("model_metadata", sa.Text()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )
    op.create_index("ix_prediction_record_org_id", "prediction_record", ["org_id"])
    op.create_index("ix_prediction_record_user_id", "prediction_record", ["user_id"])
    op.create_index("ix_prediction_record_decision", "prediction_record", ["decision"])
    op.create_index("ix_prediction_record_input_hash", "prediction_record", ["input_hash"])
    op.create_index("ix_prediction_record_batch_id", "prediction_record", ["batch_id"])
    op.create_index("ix_prediction_record_payment_method", "prediction_record", ["payment_method"])
    op.create_index("ix_prediction_record_risk_level", "prediction_record", ["risk_level"])
    op.create_index("ix_prediction_record_created_at", "prediction_record", ["created_at"])

    op.create_table(
        "prediction_batch_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("prediction_record.id"), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("was_saved", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_prediction_batch_item_batch_id", "prediction_batch_item", ["batch_id"])
    op.create_index("ix_prediction_batch_item_record_id", "prediction_batch_item", ["record_id"])

    op.create_table(
        "batch_prediction_summary",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.String(length=36), nullable=False, unique=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=255)),
        sa.Column("total_rows", sa.Integer(), nullable=True),
        sa.Column("valid_rows", sa.Integer(), nullable=True),
        sa.Column("invalid_rows", sa.Integer(), nullable=True),
        sa.Column("saved_rows", sa.Integer(), nullable=True),
        sa.Column("duplicate_rows", sa.Integer(), nullable=True),
        sa.Column("pct_high_risk", sa.Float(), nullable=True),
        sa.Column("pct_medium_risk", sa.Float(), nullable=True),
        sa.Column("pct_low_risk", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_batch_prediction_summary_batch_id", "batch_prediction_summary", ["batch_id"])
    op.create_index("ix_batch_prediction_summary_org_id", "batch_prediction_summary", ["org_id"])
    op.create_index("ix_batch_prediction_summary_user_id", "batch_prediction_summary", ["user_id"])
    op.create_index("ix_batch_prediction_summary_created_at", "batch_prediction_summary", ["created_at"])


def downgrade() -> None:
    op.drop_table("batch_prediction_summary")
    op.drop_table("prediction_batch_item")
    op.drop_table("prediction_record")
    op.drop_table("user")
    op.drop_table("organization")

