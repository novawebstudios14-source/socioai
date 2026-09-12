import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .identity import normalize_phone
from .llm import LLMProvider
from .models import Company, Conversation, Job, Memory, Message, MessageDirection, PhoneIdentity, User
from .schemas import NormalizedInbound, WebhookResult
from .evolution import WhatsAppTransport


COMPANY_PATTERNS = (
    re.compile(r"(?:minha|a) empresa (?:se chama|é)\s+(.+?)[.!?]*$", re.I),
    re.compile(r"nome da (?:minha )?empresa (?:é|:)\s*(.+?)[.!?]*$", re.I),
)


class InboundService:
    def __init__(self, llm: LLMProvider, transport: WhatsAppTransport):
        self.llm, self.transport = llm, transport

    def handle(self, db: Session, inbound: NormalizedInbound) -> WebhookResult:
        phone = normalize_phone(inbound.phone)
        company = db.scalar(select(Company).where(Company.whatsapp_instance == inbound.instance))
        if not company:
            company = Company(whatsapp_instance=inbound.instance)
            db.add(company); db.flush()
        identity = db.scalar(select(PhoneIdentity).where(
            PhoneIdentity.company_id == company.id, PhoneIdentity.phone_e164 == phone))
        if not identity:
            user = User(company_id=company.id, display_name=inbound.sender_name)
            db.add(user); db.flush()
            identity = PhoneIdentity(company_id=company.id, user_id=user.id, phone_e164=phone)
            db.add(identity); db.flush()
        conversation = db.scalar(select(Conversation).where(
            Conversation.company_id == company.id, Conversation.phone_identity_id == identity.id))
        if not conversation:
            conversation = Conversation(company_id=company.id, phone_identity_id=identity.id)
            db.add(conversation); db.flush()
        existing = db.scalar(select(Message).where(
            Message.company_id == company.id, Message.external_id == inbound.event_id))
        if existing:
            return WebhookResult(status="duplicate", message_id=existing.id)
        message = Message(company_id=company.id, conversation_id=conversation.id,
                          external_id=inbound.event_id, direction=MessageDirection.INBOUND,
                          content=inbound.text)
        db.add(message); db.flush()
        job = Job(company_id=company.id, message_id=message.id)
        db.add(job); db.commit()
        try:
            self._process(db, company, conversation, message, job, inbound)
        except Exception as exc:
            job.status, job.error = "failed", str(exc)
            db.commit()
            raise
        return WebhookResult(status="processed", message_id=message.id)

    def _process(self, db, company, conversation, message, job, inbound):
        job.status = "processing"
        for pattern in COMPANY_PATTERNS:
            match = pattern.search(inbound.text.strip())
            if match:
                value = match.group(1).strip()
                memory = db.scalar(select(Memory).where(Memory.company_id == company.id, Memory.key == "company.name"))
                if memory:
                    memory.value, memory.source_message_id = value, message.id
                else:
                    db.add(Memory(company_id=company.id, key="company.name", value=value, source_message_id=message.id))
                company.name = value
                break
        db.flush()
        memories = {m.key: m.value for m in db.scalars(select(Memory).where(Memory.company_id == company.id))}
        rows = db.scalars(select(Message).where(Message.company_id == company.id,
            Message.conversation_id == conversation.id).order_by(Message.created_at.desc()).limit(8)).all()
        history = [("user" if row.direction == MessageDirection.INBOUND else "assistant", row.content) for row in reversed(rows[:-1])]
        reply = self.llm.reply(inbound.text, memories, history)
        outbound = Message(company_id=company.id, conversation_id=conversation.id,
                           direction=MessageDirection.OUTBOUND, content=reply)
        db.add(outbound)
        self.transport.send_text(inbound.instance, normalize_phone(inbound.phone), reply)
        job.status = "completed"
        db.commit()
