"""Add Phase 2 actions, scheduling, documents and usage tracking."""
from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa

revision = "0002_phase2_actions"
down_revision = "0001_phase1"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("timezone", sa.String(64), nullable=False,
                                   server_default="America/Sao_Paulo"))
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("timezone", sa.String(64), nullable=True))
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("uq_job_message_id", type_="unique")
        batch.alter_column("message_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("kind", sa.String(40), nullable=False, server_default="inbound_message"))
        batch.add_column(sa.Column("idempotency_key", sa.String(180), nullable=True))
        batch.add_column(sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
        batch.add_column(sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False,
                                   server_default=sa.text("CURRENT_TIMESTAMP")))
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_job_company_id", ["company_id", "id"])
    op.execute("UPDATE jobs SET idempotency_key = 'legacy:' || id WHERE idempotency_key IS NULL")
    with op.batch_alter_table("jobs") as batch:
        batch.alter_column("idempotency_key", existing_type=sa.String(180), nullable=False)
        batch.create_unique_constraint("uq_job_idempotency_tenant", ["company_id", "idempotency_key"])
        batch.create_index("ix_jobs_kind", ["kind"])
        batch.create_index("ix_jobs_scheduled_for", ["scheduled_for"])

    op.create_table("tasks",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="open"),
        sa.Column("due_at", sa.DateTime(timezone=True)), sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "id", name="uq_task_company_id"))
    op.create_index("ix_tasks_company_id", "tasks", ["company_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_due_at", "tasks", ["due_at"])

    op.create_table("reminders",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("phone_identity_id", sa.String(36), nullable=False),
        sa.Column("transport_instance", sa.String(120), nullable=False),
        sa.Column("text", sa.String(1000), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "phone_identity_id"],
                                ["phone_identities.company_id", "phone_identities.id"],
                                name="fk_reminder_phone_tenant"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "id", name="uq_reminder_company_id"),
        sa.UniqueConstraint("company_id", "idempotency_key", name="uq_reminder_idempotency_tenant"))
    op.create_index("ix_reminders_company_id", "reminders", ["company_id"])
    op.create_index("ix_reminders_due_at", "reminders", ["due_at"])
    op.create_index("ix_reminders_status", "reminders", ["status"])

    op.create_table("documents",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("filename", sa.String(300), nullable=False), sa.Column("mimetype", sa.String(120), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False), sa.Column("storage_path", sa.String(800), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "id", name="uq_document_company_id"),
        sa.UniqueConstraint("company_id", "sha256", name="uq_document_sha_tenant"))
    op.create_index("ix_documents_company_id", "documents", ["company_id"])
    op.create_index("ix_documents_status", "documents", ["status"])

    op.create_table("document_chunks",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False), sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False), sa.Column("embedding", Vector(384)),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "document_id"], ["documents.company_id", "documents.id"],
                                name="fk_chunk_document_tenant"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("document_id", "position", name="uq_chunk_position"))
    op.create_index("ix_document_chunks_company_id", "document_chunks", ["company_id"])
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    if bind.dialect.name == "postgresql":
        op.execute("CREATE INDEX ix_document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops)")

    op.create_table("usage_events",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_usage_events_company_id", "usage_events", ["company_id"])
    op.create_index("ix_usage_events_kind", "usage_events", ["kind"])
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"])


def downgrade():
    op.drop_table("usage_events")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("reminders")
    op.drop_table("tasks")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_scheduled_for")
        batch.drop_index("ix_jobs_kind")
        batch.drop_constraint("uq_job_idempotency_tenant", type_="unique")
        batch.drop_constraint("uq_job_company_id", type_="unique")
        for column in ("completed_at", "scheduled_for", "max_attempts", "attempts", "payload",
                       "idempotency_key", "kind"):
            batch.drop_column(column)
        batch.alter_column("message_id", existing_type=sa.String(36), nullable=False)
        batch.create_unique_constraint("uq_job_message_id", ["message_id"])
    with op.batch_alter_table("users") as batch:
        batch.drop_column("timezone")
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("timezone")
