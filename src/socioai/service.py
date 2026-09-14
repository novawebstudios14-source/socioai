import re
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .identity import normalize_phone
from .llm import LLMProvider
from .media import MediaStore
from .models import Company, Conversation, Document, Job, Memory, Message, MessageDirection, PhoneIdentity, UsageEvent, User
from .orchestrator import ActionOrchestrator
from .tools import ToolExecutor
from .schemas import NormalizedInbound, WebhookResult
from .evolution import WhatsAppTransport


COMPANY_PATTERNS = (
    re.compile(r"(?:minha|a) empresa (?:se chama|é)\s+(.+?)[.!?]*$", re.I),
    re.compile(r"nome da (?:minha )?empresa (?:é|:)\s*(.+?)[.!?]*$", re.I),
)


class UsageLimitExceeded(RuntimeError):
    pass


class InboundService:
    def __init__(self, llm: LLMProvider, transport: WhatsAppTransport, media_store: MediaStore,
                 max_messages_per_day: int = 200):
        self.llm, self.transport = llm, transport
        self.media_store = media_store
        self.max_messages_per_day = max_messages_per_day
        self.tools = ToolExecutor()
        self.orchestrator = ActionOrchestrator(self.tools)

    def handle(self, db: Session, inbound: NormalizedInbound) -> WebhookResult:
        phone = normalize_phone(inbound.phone)
        identity = db.scalar(select(PhoneIdentity).where(PhoneIdentity.phone_e164 == phone))
        if not identity:
            company = Company()
            db.add(company); db.flush()
            user = User(company_id=company.id, display_name=inbound.sender_name)
            db.add(user); db.flush()
            identity = PhoneIdentity(company_id=company.id, user_id=user.id, phone_e164=phone)
            db.add(identity); db.flush()
        else:
            user = db.get(User, identity.user_id)
            if user is None or user.company_id != identity.company_id:
                raise RuntimeError("phone identity has an invalid tenant relationship")
            company = db.get(Company, user.company_id)
            if company is None:
                raise RuntimeError("phone identity references a missing company")
        start = datetime.now(timezone.utc) - timedelta(days=1)
        used = db.scalar(select(func.coalesce(func.sum(UsageEvent.units), 0)).where(
            UsageEvent.company_id == company.id, UsageEvent.kind == "inbound_message",
            UsageEvent.created_at >= start))
        if used >= self.max_messages_per_day:
            raise UsageLimitExceeded("daily usage limit reached")
        conversation = db.scalar(select(Conversation).where(
            Conversation.company_id == company.id, Conversation.phone_identity_id == identity.id))
        if not conversation:
            conversation = Conversation(company_id=company.id, phone_identity_id=identity.id)
            db.add(conversation); db.flush()
        existing = db.scalar(select(Message).where(
            Message.company_id == company.id, Message.external_id == inbound.event_id))
        if existing:
            return WebhookResult(status="duplicate", message_id=existing.id)
        content = inbound.text or f"[{inbound.message_type}] {inbound.filename or ''}".strip()
        message = Message(company_id=company.id, conversation_id=conversation.id,
                          external_id=inbound.event_id, direction=MessageDirection.INBOUND,
                          content=content)
        db.add(message); db.flush()
        db.add(UsageEvent(company_id=company.id, kind="inbound_message"))
        job = Job(company_id=company.id, message_id=message.id, kind="inbound_message",
                  idempotency_key=f"inbound:{inbound.event_id}")
        db.add(job); db.commit()
        try:
            if inbound.message_type == "text":
                self._process(db, company, identity, conversation, message, job, inbound)
            else:
                self._enqueue_media(db, company, identity, conversation, message, job, inbound)
        except Exception as exc:
            job.status, job.error = "failed", str(exc)
            db.commit()
            raise
        return WebhookResult(status="processed", message_id=message.id)

    def _process(self, db, company, identity, conversation, message, job, inbound):
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
        action_reply = self.orchestrator.execute(db, company, identity, message, inbound)
        memories = {m.key: m.value for m in db.scalars(select(Memory).where(Memory.company_id == company.id))}
        rows = db.scalars(select(Message).where(Message.company_id == company.id,
            Message.conversation_id == conversation.id).order_by(Message.created_at.desc()).limit(8)).all()
        history = [("user" if row.direction == MessageDirection.INBOUND else "assistant", row.content) for row in reversed(rows[:-1])]
        reply = action_reply or self.llm.reply(inbound.text, memories, history)
        outbound = Message(company_id=company.id, conversation_id=conversation.id,
                           direction=MessageDirection.OUTBOUND, content=reply)
        db.add(outbound)
        self.transport.send_text(inbound.instance, normalize_phone(inbound.phone), reply)
        job.status, job.completed_at = "completed", datetime.now(timezone.utc)
        db.commit()

    def _enqueue_media(self, db, company, identity, conversation, message, job, inbound):
        job.status = "processing"
        path, content = self.media_store.save(company.id, inbound)
        if inbound.message_type == "document":
            if inbound.media_mimetype not in (None, "application/pdf") or not content.startswith(b"%PDF"):
                raise ValueError("only valid PDF documents are supported")
            digest = hashlib.sha256(content).hexdigest()
            document = db.scalar(select(Document).where(Document.company_id == company.id,
                                                         Document.sha256 == digest))
            if not document:
                document = Document(company_id=company.id, filename=inbound.filename or "documento.pdf",
                    mimetype="application/pdf", sha256=digest, storage_path=path)
                db.add(document); db.flush()
                db.add(Job(company_id=company.id, kind="process_document", message_id=message.id,
                    idempotency_key=f"document:{digest}", payload={"document_id": document.id}))
            reply = f"Documento recebido: {document.filename}. Vou processá-lo."
        elif inbound.message_type == "audio":
            db.add(Job(company_id=company.id, kind="transcribe_audio", message_id=message.id,
                idempotency_key=f"audio:{inbound.event_id}", payload={"storage_path": path,
                    "mimetype": inbound.media_mimetype, "instance": inbound.instance,
                    "phone": identity.phone_e164}))
            reply = "Áudio recebido. Vou transcrevê-lo e processar o pedido."
        else:
            raise ValueError("unsupported media type")
        db.add(Message(company_id=company.id, conversation_id=conversation.id,
                       direction=MessageDirection.OUTBOUND, content=reply))
        self.transport.send_text(inbound.instance, identity.phone_e164, reply)
        job.status, job.completed_at = "completed", datetime.now(timezone.utc)
        db.commit()
