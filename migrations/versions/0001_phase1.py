"""Create the explicit Phase 1 multi-tenant schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_phase1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("companies",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"))
    op.create_table("users",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "id", name="uq_user_company_id"))
    op.create_index("ix_users_company_id", "users", ["company_id"])
    op.create_table("phone_identities",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("phone_e164", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                                name="fk_phone_identity_user_tenant"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "id", name="uq_phone_identity_company_id"),
        sa.UniqueConstraint("phone_e164", name="uq_phone_identity_phone"))
    op.create_index("ix_phone_identities_company_id", "phone_identities", ["company_id"])
    op.create_index("ix_phone_identities_phone_e164", "phone_identities", ["phone_e164"])
    op.create_index("ix_phone_identities_user_id", "phone_identities", ["user_id"])
    op.create_table("conversations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("phone_identity_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "phone_identity_id"],
                                ["phone_identities.company_id", "phone_identities.id"],
                                name="fk_conversation_phone_tenant"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "id", name="uq_conversation_company_id"),
        sa.UniqueConstraint("company_id", "phone_identity_id", name="uq_conversation_phone"))
    op.create_index("ix_conversations_company_id", "conversations", ["company_id"])
    op.create_table("messages",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("external_id", sa.String(180), nullable=True),
        sa.Column("direction", sa.Enum("INBOUND", "OUTBOUND", name="messagedirection"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "conversation_id"],
                                ["conversations.company_id", "conversations.id"],
                                name="fk_message_conversation_tenant"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "external_id", name="uq_message_external_tenant"),
        sa.UniqueConstraint("company_id", "id", name="uq_message_company_id"))
    op.create_index("ix_messages_company_id", "messages", ["company_id"])
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_table("memories",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.String(36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "source_message_id"],
                                ["messages.company_id", "messages.id"],
                                name="fk_memory_source_message_tenant"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "key", name="uq_memory_tenant_key"))
    op.create_index("ix_memories_company_id", "memories", ["company_id"])
    op.create_table("jobs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "message_id"], ["messages.company_id", "messages.id"],
                                name="fk_job_message_tenant"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"))
    op.create_index("ix_jobs_company_id", "jobs", ["company_id"])


def downgrade():
    op.drop_index("ix_jobs_company_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_memories_company_id", table_name="memories")
    op.drop_table("memories")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_index("ix_messages_company_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_company_id", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_phone_identities_user_id", table_name="phone_identities")
    op.drop_index("ix_phone_identities_phone_e164", table_name="phone_identities")
    op.drop_index("ix_phone_identities_company_id", table_name="phone_identities")
    op.drop_table("phone_identities")
    op.drop_index("ix_users_company_id", table_name="users")
    op.drop_table("users")
    op.drop_table("companies")
    sa.Enum(name="messagedirection").drop(op.get_bind(), checkfirst=True)
