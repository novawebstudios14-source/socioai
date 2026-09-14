"""Add Phase 3 product, consent, billing and opportunity schema."""
from alembic import op
import sqlalchemy as sa

revision = "0003_phase3_product"
down_revision = "0002_phase2_actions"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("segment", sa.String(200)))
        batch.add_column(sa.Column("primary_objective", sa.String(500)))
        batch.add_column(sa.Column("onboarding_step", sa.String(40), nullable=False, server_default="completed"))
        batch.add_column(sa.Column("access_status", sa.String(40), nullable=False, server_default="trial"))
        batch.create_index("ix_companies_access_status", ["access_status"])
    with op.batch_alter_table("usage_events") as batch:
        batch.add_column(sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"))

    op.create_table("plans",
        sa.Column("code", sa.String(60), primary_key=True), sa.Column("name", sa.String(120), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False), sa.Column("active", sa.Boolean(), nullable=False,
                                                                  server_default=sa.true()))
    op.bulk_insert(sa.table("plans", sa.column("code", sa.String), sa.column("name", sa.String),
                            sa.column("limits", sa.JSON), sa.column("active", sa.Boolean)), [{
        "code": "starter", "name": "Sócio IA Inicial", "active": True,
        "limits": {"messages_month": 500, "llm_calls_month": 300, "documents_month": 20,
                   "document_size_bytes": 20971520, "audio_minutes_month": 120,
                   "active_reminders": 100, "storage_bytes": 524288000, "memory_items": 500}}])
    op.create_table("subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id"), nullable=False, unique=True),
        sa.Column("plan_code", sa.String(60), sa.ForeignKey("plans.code"), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="trial"),
        sa.Column("provider", sa.String(60)), sa.Column("external_id", sa.String(180)),
        sa.Column("current_period_end", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_subscriptions_company_id", "subscriptions", ["company_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    op.create_table("consents",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False), sa.Column("phone_identity_id", sa.String(36), nullable=False),
        sa.Column("terms_version", sa.String(40), nullable=False),
        sa.Column("privacy_version", sa.String(40), nullable=False),
        sa.Column("accepted_scopes", sa.JSON(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                                name="fk_consent_user_tenant"),
        sa.ForeignKeyConstraint(["company_id", "phone_identity_id"],
                                ["phone_identities.company_id", "phone_identities.id"],
                                name="fk_consent_phone_tenant"))
    op.create_index("ix_consents_company_id", "consents", ["company_id"])

    op.create_table("payment_events",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("external_event_id", sa.String(180), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id")),
        sa.Column("event_type", sa.String(80), nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="received"),
        sa.Column("error", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "external_event_id", name="uq_payment_provider_event"))
    for column in ("provider", "company_id", "status"):
        op.create_index(f"ix_payment_events_{column}", "payment_events", [column])

    op.create_table("company_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("field", sa.String(120), nullable=False), sa.Column("value", sa.Text(), nullable=False),
        sa.Column("provenance", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False), sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "field", name="uq_company_fact_field"))
    op.create_index("ix_company_facts_company_id", "company_facts", ["company_id"])

    op.create_table("opportunities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("category", sa.String(60), nullable=False), sa.Column("title", sa.String(300), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False), sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("business_impact", sa.Text(), nullable=False),
        sa.Column("suggested_solution", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False), sa.Column("priority", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="detected"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "id", name="uq_opportunity_company_id"))
    for column in ("company_id", "category", "priority", "status"):
        op.create_index(f"ix_opportunities_{column}", "opportunities", [column])

    op.create_table("recommendation_logs",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("opportunity_id", sa.String(36), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "opportunity_id"],
                                ["opportunities.company_id", "opportunities.id"],
                                name="fk_recommendation_opportunity_tenant"))
    op.create_index("ix_recommendation_logs_company_id", "recommendation_logs", ["company_id"])
    op.create_index("ix_recommendation_logs_opportunity_id", "recommendation_logs", ["opportunity_id"])

    op.create_table("data_subject_requests",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("company_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False), sa.Column("request_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="requested"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                                name="fk_data_request_user_tenant"))
    op.create_index("ix_data_subject_requests_company_id", "data_subject_requests", ["company_id"])
    op.create_index("ix_data_subject_requests_request_type", "data_subject_requests", ["request_type"])
    op.create_index("ix_data_subject_requests_status", "data_subject_requests", ["status"])


def downgrade():
    for table in ("data_subject_requests", "recommendation_logs", "opportunities", "company_facts",
                  "payment_events", "consents", "subscriptions", "plans"):
        op.drop_table(table)
    with op.batch_alter_table("usage_events") as batch:
        batch.drop_column("metadata_json")
    with op.batch_alter_table("companies") as batch:
        batch.drop_index("ix_companies_access_status")
        batch.drop_column("access_status")
        batch.drop_column("onboarding_step")
        batch.drop_column("primary_objective")
        batch.drop_column("segment")
