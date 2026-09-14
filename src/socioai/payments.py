import hashlib
import hmac
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Company, PaymentEvent, Subscription


class ParsedPayment(BaseModel):
    event_id: str
    event_type: str
    company_id: str
    plan_code: str = "starter"
    subscription_external_id: str | None = None


class PaymentProvider(Protocol):
    name: str
    def validate_and_parse(self, body: bytes, signature: str, payload: dict) -> ParsedPayment: ...


class GenericHmacPaymentProvider:
    """Documented contract used until a real provider and credentials are selected."""
    name = "generic"

    def __init__(self, secret: str): self.secret = secret

    def validate_and_parse(self, body: bytes, signature: str, payload: dict) -> ParsedPayment:
        if not self.secret: raise ValueError("payment webhook secret is not configured")
        expected = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature or ""): raise ValueError("invalid signature")
        return ParsedPayment(**payload)


class PaymentService:
    TRANSITIONS = {"payment_approved": "active", "payment_past_due": "past_due",
                   "subscription_cancelled": "cancelled", "payment_refunded": "cancelled"}

    def process(self, db: Session, provider: PaymentProvider, parsed: ParsedPayment,
                raw: dict) -> tuple[PaymentEvent, bool]:
        existing = db.scalar(select(PaymentEvent).where(PaymentEvent.provider == provider.name,
            PaymentEvent.external_event_id == parsed.event_id))
        if existing: return existing, True
        company = db.get(Company, parsed.company_id)
        if not company:
            db.add(PaymentEvent(provider=provider.name, external_event_id=parsed.event_id,
                company_id=None, event_type=parsed.event_type, payload=raw, status="failed",
                error="payment company not found"))
            db.commit(); raise ValueError("payment company not found")
        subscription = db.scalar(select(Subscription).where(Subscription.company_id == company.id))
        event = PaymentEvent(provider=provider.name, external_event_id=parsed.event_id,
            company_id=company.id, event_type=parsed.event_type, payload=raw)
        db.add(event); db.flush()
        if not subscription:
            event.status, event.error = "failed", "subscription not found"
            db.commit(); raise ValueError("subscription not found")
        target = self.TRANSITIONS.get(parsed.event_type)
        if not target:
            event.status = "ignored"
        else:
            subscription.status = company.access_status = target
            subscription.plan_code = parsed.plan_code
            subscription.provider = provider.name
            subscription.external_id = parsed.subscription_external_id
            event.status = "processed"
        db.commit(); return event, False
