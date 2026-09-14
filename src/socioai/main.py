from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from pydantic import BaseModel

from .config import Settings, get_settings
from .database import assert_schema_current, build_engine, build_session_factory, session_dependency
from .evolution import EvolutionWhatsAppTransport, normalize_evolution
from .llm import DeterministicLLM, OpenAICompatibleLLM
from .media import MediaStore, OpenAICompatibleTranscriber
from .models import Company, Job, Opportunity, PaymentEvent, PhoneIdentity, Subscription, UsageEvent, User
from .observability import configure_logging
from .payments import GenericHmacPaymentProvider, PaymentService
from .service import InboundService, UsageLimitExceeded
from .worker import PersistentWorker

configure_logging()


class AccessUpdate(BaseModel):
    status: str


class OpportunityUpdate(BaseModel):
    status: str


def create_app(settings: Settings | None = None, transport=None, llm=None, transcriber=None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_runtime()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    transport = transport or EvolutionWhatsAppTransport(settings.evolution_base_url, settings.evolution_api_key)
    llm = llm or (OpenAICompatibleLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
                  if settings.llm_provider == "openai-compatible" else DeterministicLLM())
    media_store = MediaStore(settings.data_dir)
    service = InboundService(llm, transport, media_store, settings.max_messages_per_day,
        settings.require_onboarding, settings.terms_version, settings.privacy_version,
        settings.recommendation_confidence, settings.recommendation_cooldown_days)
    transcriber = transcriber or OpenAICompatibleTranscriber(settings.transcription_base_url,
                                                              settings.transcription_api_key,
                                                              settings.transcription_model)
    worker = PersistentWorker(factory, transport, service, transcriber)
    payment_provider = GenericHmacPaymentProvider(settings.payment_webhook_secret)
    payment_service = PaymentService()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        assert_schema_current(engine)
        yield
        engine.dispose()

    app = FastAPI(title="Sócio IA", version="0.1.0", lifespan=lifespan)

    def get_db():
        yield from session_dependency(factory)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/health/operational")
    def operational_health(db: Session = Depends(get_db)):
        queued = db.scalar(select(func.count()).select_from(Job).where(Job.status == "queued"))
        failed = db.scalar(select(func.count()).select_from(Job).where(Job.status == "failed"))
        failed_payments = db.scalar(select(func.count()).select_from(PaymentEvent).where(
            PaymentEvent.status == "failed"))
        heartbeat = Path(settings.data_dir) / "worker.heartbeat"
        worker_alive = heartbeat.exists() and (
            datetime.now(timezone.utc).timestamp() - heartbeat.stat().st_mtime < 30
        )
        storage_writable = False
        try:
            Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
            probe = Path(settings.data_dir) / ".write-probe"
            probe.write_text("ok")
            probe.unlink()
            storage_writable = True
        except OSError:
            pass
        degraded = bool(failed) or not storage_writable or (settings.is_deployed and not worker_alive)
        return {"status": "degraded" if degraded else "ok",
                "database": "ok", "schema": "head",
                "storage": "ok" if storage_writable else "failed",
                "worker": "alive" if worker_alive else "not_observed",
                "queue": {"queued": queued, "failed": failed},
                "providers": {"llm": "configured" if settings.llm_api_key else "deterministic",
                              "transcription": "configured" if settings.transcription_api_key else "degraded",
                              "evolution": "configured" if settings.evolution_api_key else "degraded",
                              "payment": "configured" if settings.payment_webhook_secret else "not_required"},
                "failed_payment_events": failed_payments}

    @app.get("/health/providers")
    def provider_health():
        """Actively verify staging dependencies without exposing credentials."""
        checks = {}
        targets = {
            "llm": (f"{settings.llm_base_url.rstrip('/')}/models",
                    {"Authorization": f"Bearer {settings.llm_api_key}"}),
            "transcription": (f"{settings.transcription_base_url.rstrip('/')}/models",
                              {"Authorization": f"Bearer {settings.transcription_api_key}"}),
            "evolution": (f"{settings.evolution_base_url.rstrip('/')}/instance/connectionState/{settings.evolution_instance}",
                          {"apikey": settings.evolution_api_key}),
        }
        for name, (url, headers) in targets.items():
            try:
                response = httpx.get(url, headers=headers, timeout=10)
                checks[name] = {"ok": response.is_success, "status_code": response.status_code}
            except httpx.HTTPError:
                checks[name] = {"ok": False, "status_code": None}
        return {"status": "ok" if all(item["ok"] for item in checks.values()) else "degraded",
                "providers": checks}

    @app.post("/webhooks/payments/{provider_name}")
    async def payment_webhook(provider_name: str, request: Request,
                              x_signature: str | None = Header(default=None),
                              db: Session = Depends(get_db)):
        if provider_name != settings.payment_provider:
            raise HTTPException(404, "payment provider is not configured")
        body = await request.body()
        try:
            payload = await request.json()
            parsed = payment_provider.validate_and_parse(body, x_signature or "", payload)
            event, duplicate = payment_service.process(db, payment_provider, parsed, payload)
            return {"status": event.status, "duplicate": duplicate, "event_id": event.id}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def require_admin(x_admin_key: str | None = Header(default=None)):
        if not settings.admin_api_key or x_admin_key != settings.admin_api_key:
            raise HTTPException(401, "invalid admin credential")

    @app.get("/internal/opportunities", dependencies=[Depends(require_admin)])
    def list_opportunities(db: Session = Depends(get_db)):
        rows = db.execute(select(Opportunity, Company).join(Company, Company.id == Opportunity.company_id)
                          .order_by(Opportunity.created_at.desc()).limit(200)).all()
        return [{"id": opportunity.id, "company": company.name, "category": opportunity.category,
                 "title": opportunity.title, "problem": opportunity.problem,
                 "evidence": opportunity.evidence, "suggested_solution": opportunity.suggested_solution,
                 "confidence": opportunity.confidence, "priority": opportunity.priority,
                 "status": opportunity.status, "created_at": opportunity.created_at}
                for opportunity, company in rows]

    @app.get("/internal/metrics", dependencies=[Depends(require_admin)])
    def metrics(db: Session = Depends(get_db)):
        usage = db.execute(select(UsageEvent.kind, func.sum(UsageEvent.units)).group_by(UsageEvent.kind)).all()
        by_company = db.execute(select(UsageEvent.company_id, UsageEvent.kind, func.sum(UsageEvent.units))
                                .group_by(UsageEvent.company_id, UsageEvent.kind)).all()
        by_plan = db.execute(select(Subscription.plan_code, UsageEvent.kind, func.sum(UsageEvent.units))
            .join(UsageEvent, UsageEvent.company_id == Subscription.company_id)
            .group_by(Subscription.plan_code, UsageEvent.kind)).all()
        return {"active_companies": db.scalar(select(func.count()).select_from(Company).where(
                    Company.access_status.in_(("trial", "active")))),
                "active_users": db.scalar(select(func.count()).select_from(User).join(
                    Company, Company.id == User.company_id).where(Company.access_status.in_(("trial", "active")))),
                "usage": {kind: units for kind, units in usage},
                "usage_by_company": [{"company_id": company_id, "kind": kind, "units": units}
                                     for company_id, kind, units in by_company],
                "usage_by_plan": [{"plan": plan, "kind": kind, "units": units}
                                  for plan, kind, units in by_plan],
                "opportunities": db.scalar(select(func.count()).select_from(Opportunity)),
                "failed_jobs": db.scalar(select(func.count()).select_from(Job).where(Job.status == "failed"))}

    @app.get("/internal/companies", dependencies=[Depends(require_admin)])
    def find_company(phone: str, db: Session = Depends(get_db)):
        normalized = "".join(character for character in phone if character.isdigit())
        identity = db.scalar(select(PhoneIdentity).where(PhoneIdentity.phone_e164 == f"+{normalized}"))
        if not identity:
            raise HTTPException(404, "phone identity not found")
        company = db.get(Company, identity.company_id)
        return {"company_id": company.id, "name": company.name,
                "onboarding_step": company.onboarding_step, "access_status": company.access_status}

    @app.patch("/internal/companies/{company_id}/access", dependencies=[Depends(require_admin)])
    def update_access(company_id: str, update: AccessUpdate, db: Session = Depends(get_db)):
        if update.status not in {"trial", "active", "past_due", "cancelled", "blocked"}:
            raise HTTPException(422, "invalid access status")
        company = db.get(Company, company_id)
        if not company: raise HTTPException(404, "company not found")
        company.access_status = update.status
        subscription = db.scalar(select(Subscription).where(Subscription.company_id == company.id))
        if subscription: subscription.status = update.status
        db.commit(); return {"company_id": company.id, "status": company.access_status}

    @app.patch("/internal/opportunities/{opportunity_id}", dependencies=[Depends(require_admin)])
    def update_opportunity(opportunity_id: str, update: OpportunityUpdate,
                           db: Session = Depends(get_db)):
        if update.status not in {"detected", "review", "qualified", "contacted", "won", "lost", "ignored"}:
            raise HTTPException(422, "invalid opportunity status")
        opportunity = db.get(Opportunity, opportunity_id)
        if not opportunity: raise HTTPException(404, "opportunity not found")
        opportunity.status = update.status; db.commit()
        return {"id": opportunity.id, "status": opportunity.status}

    @app.post("/webhooks/evolution")
    def evolution_webhook(payload: dict[str, Any], db: Session = Depends(get_db),
                          x_api_key: str | None = Header(default=None)):
        if settings.evolution_webhook_secret and x_api_key != settings.evolution_webhook_secret:
            raise HTTPException(401, "invalid webhook credential")
        inbound = normalize_evolution(payload)
        if inbound is None:
            return {"status": "ignored"}
        try:
            return service.handle(db, inbound)
        except UsageLimitExceeded as exc:
            raise HTTPException(429, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    app.state.engine = engine
    app.state.session_factory = factory
    app.state.worker = worker
    app.state.inbound_service = service
    return app


app = create_app()
