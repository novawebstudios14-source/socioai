import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    whatsapp_instance: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("company_id", "id", name="uq_user_company_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PhoneIdentity(Base):
    __tablename__ = "phone_identities"
    __table_args__ = (UniqueConstraint("company_id", "phone_e164", name="uq_phone_company"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    phone_e164: Mapped[str] = mapped_column(String(20), index=True)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("company_id", "phone_identity_id", name="uq_conversation_phone"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    phone_identity_id: Mapped[str] = mapped_column(ForeignKey("phone_identities.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("company_id", "external_id", name="uq_message_external_tenant"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(180))
    direction: Mapped[MessageDirection] = mapped_column(Enum(MessageDirection))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_memory_tenant_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

