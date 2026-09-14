import re
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .identity import normalize_phone
from .llm import LLMProvider
from .media import MediaStore
from .models import Company, Conversation, DataSubjectRequest, Document, Job, Memory, Message, MessageDirection, PhoneIdentity, Plan, Subscription, UsageEvent, User
from .orchestrator import ActionOrchestrator
from .product import (AccessPolicy, OnboardingService, OpportunityEngine, UsagePolicy,
                      ensure_product_records)
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
                 max_messages_per_day: int = 200, require_onboarding: bool = True,
                 terms_version: str = "2026-09", privacy_version: str = "2026-09",
                 recommendation_confidence: float = 0.75, recommendation_cooldown_days: int = 30):
        self.llm, self.transport = llm, transport
        self.media_store = media_store
        self.max_messages_per_day = max_messages_per_day
        self.require_onboarding = require_onboarding
        self.tools = ToolExecutor()
        self.orchestrator = ActionOrchestrator(self.tools)
        self.access = AccessPolicy()
        self.usage = UsagePolicy()
        self.onboarding = OnboardingService(terms_version, privacy_version)
        self.opportunities = OpportunityEngine()
        self.recommendation_confidence = recommendation_confidence
        self.recommendation_cooldown_days = recommendation_cooldown_days

    def handle(self, db: Session, inbound: NormalizedInbound) -> WebhookResult:
        phone = normalize_phone(inbound.phone)
        identity = db.scalar(select(PhoneIdentity).where(PhoneIdentity.phone_e164 == phone))
        if not identity:
            company = Company()
            if not self.require_onboarding:
                company.onboarding_step, company.access_status = "completed", "trial"
            db.add(company); db.flush()
            user = User(company_id=company.id, display_name=inbound.sender_name)
            db.add(user); db.flush()
            identity = PhoneIdentity(company_id=company.id, user_id=user.id, phone_e164=phone)
            db.add(identity); db.flush()
            ensure_product_records(db, company)
        else:
            user = db.get(User, identity.user_id)
            if user is None or user.company_id != identity.company_id:
                raise RuntimeError("phone identity has an invalid tenant relationship")
            company = db.get(Company, user.company_id)
            if company is None:
                raise RuntimeError("phone identity references a missing company")
        conversation = db.scalar(select(Conversation).where(
            Conversation.company_id == company.id, Conversation.phone_identity_id == identity.id))
        if not conversation:
            conversation = Conversation(company_id=company.id, phone_identity_id=identity.id)
            db.add(conversation); db.flush()
        existing = db.scalar(select(Message).where(
            Message.company_id == company.id, Message.external_id == inbound.event_id))
        if existing:
            return WebhookResult(status="duplicate", message_id=existing.id)
        decision = self.access.decide(db, company)
        if decision.allowed:
            usage = self.usage.check(db, company.id, "inbound_message")
            if not usage.allowed: raise UsageLimitExceeded(usage.reason)
            day_start = datetime.now(timezone.utc) - timedelta(days=1)
            daily_used = db.scalar(select(func.coalesce(func.sum(UsageEvent.units), 0)).where(
                UsageEvent.company_id == company.id, UsageEvent.kind == "inbound_message",
                UsageEvent.created_at >= day_start))
            if daily_used >= self.max_messages_per_day:
                raise UsageLimitExceeded("Você atingiu o limite diário de mensagens do seu plano.")
        content = inbound.text or f"[{inbound.message_type}] {inbound.filename or ''}".strip()
        message = Message(company_id=company.id, conversation_id=conversation.id,
                          external_id=inbound.event_id, direction=MessageDirection.INBOUND,
                          content=content)
        db.add(message); db.flush()
        if decision.allowed:
            db.add(UsageEvent(company_id=company.id, kind="inbound_message"))
        job = Job(company_id=company.id, message_id=message.id, kind="inbound_message",
                  idempotency_key=f"inbound:{inbound.event_id}")
        db.add(job); db.commit()
        try:
            if decision.reason == "onboarding":
                reply = self.onboarding.handle(db, company, user, identity, message)
                self._finish_reply(db, company.id, conversation.id, job, inbound.instance,
                                   identity.phone_e164, reply)
            elif not decision.allowed:
                self._finish_reply(db, company.id, conversation.id, job, inbound.instance,
                                   identity.phone_e164, decision.reason or "Acesso indisponível.")
            elif inbound.message_type == "text":
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
        privacy_type = next((kind for phrase, kind in (
            ("excluir meus dados", "deletion"), ("excluir minha conta", "deletion"),
            ("exportar meus dados", "export"), ("corrigir meus dados", "correction")) if phrase in inbound.text.casefold()), None)
        if privacy_type:
            db.add(DataSubjectRequest(company_id=company.id, user_id=identity.user_id,
                                      request_type=privacy_type))
            action_reply = "Solicitação registrada. A equipe responsável dará continuidade com segurança."
        else:
            action_reply = self.orchestrator.execute(db, company, identity, message, inbound)
        memories = {m.key: m.value for m in db.scalars(select(Memory).where(Memory.company_id == company.id))}
        rows = db.scalars(select(Message).where(Message.company_id == company.id,
            Message.conversation_id == conversation.id).order_by(Message.created_at.desc()).limit(8)).all()
        history = [("user" if row.direction == MessageDirection.INBOUND else "assistant", row.content) for row in reversed(rows[:-1])]
        if action_reply:
            reply = action_reply
        else:
            reply = self.llm.reply(inbound.text, memories, history)
            db.add(UsageEvent(company_id=company.id, kind="llm_call", units=1,
                              metadata_json={"estimated_tokens": max(1, len(inbound.text + reply) // 4)}))
            db.add(UsageEvent(company_id=company.id, kind="estimated_tokens",
                              units=max(1, len(inbound.text + reply) // 4)))
            db.add(UsageEvent(company_id=company.id, kind="estimated_cost_microunits", units=0))
        opportunity = self.opportunities.analyze(db, company.id)
        if opportunity:
            recommendation = self.opportunities.recommendation(db, opportunity,
                self.recommendation_confidence, self.recommendation_cooldown_days)
            if recommendation: reply = f"{reply}\n\n{recommendation}"
        self._finish_reply(db, company.id, conversation.id, job, inbound.instance,
                           normalize_phone(inbound.phone), reply)

    def _enqueue_media(self, db, company, identity, conversation, message, job, inbound):
        job.status = "processing"
        path, content = self.media_store.save(company.id, inbound)
        if inbound.message_type == "document":
            allowance = self.usage.check(db, company.id, "document_upload")
            subscription = db.scalar(select(Subscription).where(Subscription.company_id == company.id))
            plan = db.get(Plan, subscription.plan_code) if subscription else None
            if not allowance.allowed:
                raise UsageLimitExceeded(allowance.reason)
            if plan and len(content) > plan.limits.get("document_size_bytes", len(content)):
                raise UsageLimitExceeded("Este documento excede o tamanho permitido pelo seu plano.")
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
            allowance = self.usage.check(db, company.id, "audio_minute")
            if not allowance.allowed: raise UsageLimitExceeded(allowance.reason)
            db.add(Job(company_id=company.id, kind="transcribe_audio", message_id=message.id,
                idempotency_key=f"audio:{inbound.event_id}", payload={"storage_path": path,
                    "mimetype": inbound.media_mimetype, "instance": inbound.instance,
                    "phone": identity.phone_e164}))
            reply = "Áudio recebido. Vou transcrevê-lo e processar o pedido."
        else:
            raise ValueError("unsupported media type")
        metric = "document_upload" if inbound.message_type == "document" else "audio_minute"
        db.add(UsageEvent(company_id=company.id, kind=metric, units=1))
        self._finish_reply(db, company.id, conversation.id, job, inbound.instance,
                           identity.phone_e164, reply)

    def _finish_reply(self, db, company_id, conversation_id, job, instance, phone, reply):
        db.add(Message(company_id=company_id, conversation_id=conversation_id,
                       direction=MessageDirection.OUTBOUND, content=reply))
        self.transport.send_text(instance, phone, reply)
        db.add(UsageEvent(company_id=company_id, kind="outbound_message", units=1))
        job.status, job.completed_at = "completed", datetime.now(timezone.utc)
        db.commit()
