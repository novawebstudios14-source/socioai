import enum
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="America/Sao_Paulo")
    segment: Mapped[str | None] = mapped_column(String(200))
    primary_objective: Mapped[str | None] = mapped_column(String(500))
    onboarding_step: Mapped[str] = mapped_column(String(40), default="name")
    access_status: Mapped[str] = mapped_column(String(40), default="pending_onboarding", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("company_id", "id", name="uq_user_company_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PhoneIdentity(Base):
    __tablename__ = "phone_identities"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_phone_identity_company_id"),
        UniqueConstraint("phone_e164", name="uq_phone_identity_phone"),
        ForeignKeyConstraint(
            ["company_id", "user_id"], ["users.company_id", "users.id"],
            name="fk_phone_identity_user_tenant",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    phone_e164: Mapped[str] = mapped_column(String(20), index=True)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_conversation_company_id"),
        UniqueConstraint("company_id", "phone_identity_id", name="uq_conversation_phone"),
        ForeignKeyConstraint(
            ["company_id", "phone_identity_id"],
            ["phone_identities.company_id", "phone_identities.id"],
            name="fk_conversation_phone_tenant",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    phone_identity_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_message_company_id"),
        UniqueConstraint("company_id", "external_id", name="uq_message_external_tenant"),
        ForeignKeyConstraint(
            ["company_id", "conversation_id"],
            ["conversations.company_id", "conversations.id"],
            name="fk_message_conversation_tenant",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    external_id: Mapped[str | None] = mapped_column(String(180))
    direction: Mapped[MessageDirection] = mapped_column(Enum(MessageDirection))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_memory_tenant_key"),
        ForeignKeyConstraint(
            ["company_id", "source_message_id"],
            ["messages.company_id", "messages.id"],
            name="fk_memory_source_message_tenant",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_job_company_id"),
        UniqueConstraint("company_id", "idempotency_key", name="uq_job_idempotency_tenant"),
        ForeignKeyConstraint(
            ["company_id", "message_id"],
            ["messages.company_id", "messages.id"],
            name="fk_job_message_tenant",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    message_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(40), default="inbound_message", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("company_id", "id", name="uq_task_company_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_reminder_company_id"),
        UniqueConstraint("company_id", "idempotency_key", name="uq_reminder_idempotency_tenant"),
        ForeignKeyConstraint(["company_id", "phone_identity_id"],
                             ["phone_identities.company_id", "phone_identities.id"],
                             name="fk_reminder_phone_tenant"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    phone_identity_id: Mapped[str] = mapped_column(String(36))
    transport_instance: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(String(1000))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_document_company_id"),
        UniqueConstraint("company_id", "sha256", name="uq_document_sha_tenant"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    mimetype: Mapped[str] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(String(800))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "document_id"],
                             ["documents.company_id", "documents.id"],
                             name="fk_chunk_document_tenant"),
        UniqueConstraint("document_id", "position", name="uq_chunk_position"),
        Index("ix_document_chunks_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    position: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(Vector(384))


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    units: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Consent(Base):
    __tablename__ = "consents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36))
    phone_identity_id: Mapped[str] = mapped_column(String(36))
    terms_version: Mapped[str] = mapped_column(String(40))
    privacy_version: Mapped[str] = mapped_column(String(40))
    accepted_scopes: Mapped[list] = mapped_column(JSON)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                             name="fk_consent_user_tenant"),
        ForeignKeyConstraint(["company_id", "phone_identity_id"],
                             ["phone_identities.company_id", "phone_identities.id"],
                             name="fk_consent_phone_tenant"),
    )


class Plan(Base):
    __tablename__ = "plans"
    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    limits: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), unique=True, index=True)
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"))
    status: Mapped[str] = mapped_column(String(40), default="trial", index=True)
    provider: Mapped[str | None] = mapped_column(String(60))
    external_id: Mapped[str | None] = mapped_column(String(180))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    __table_args__ = (UniqueConstraint("provider", "external_event_id", name="uq_payment_provider_event"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(60), index=True)
    external_event_id: Mapped[str] = mapped_column(String(180))
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), default="received", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CompanyFact(Base):
    __tablename__ = "company_facts"
    __table_args__ = (UniqueConstraint("company_id", "field", name="uq_company_fact_field"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    field: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String(40))
    source_type: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Opportunity(Base):
    __tablename__ = "opportunities"
    __table_args__ = (UniqueConstraint("company_id", "id", name="uq_opportunity_company_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    category: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(300))
    problem: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON)
    business_impact: Mapped[str] = mapped_column(Text)
    suggested_solution: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    priority: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), default="detected", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class RecommendationLog(Base):
    __tablename__ = "recommendation_logs"
    __table_args__ = (ForeignKeyConstraint(["company_id", "opportunity_id"],
                                           ["opportunities.company_id", "opportunities.id"],
                                           name="fk_recommendation_opportunity_tenant"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    opportunity_id: Mapped[str] = mapped_column(String(36), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DataSubjectRequest(Base):
    __tablename__ = "data_subject_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36))
    request_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(40), default="requested", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(["company_id", "user_id"],
                                           ["users.company_id", "users.id"],
                                           name="fk_data_request_user_tenant"),)
