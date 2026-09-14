import hashlib
import hmac
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from socioai.config import Settings
from socioai.evolution import normalize_evolution
from socioai.main import create_app
from socioai.models import (Company, CompanyFact, Consent, Opportunity, PaymentEvent,
                            Plan, RecommendationLog, Subscription)


class FakeTransport:
    def __init__(self): self.sent = []
    def send_text(self, instance, phone, text): self.sent.append((instance, phone, text))


def setup(tmp_path: Path, **updates):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'phase3.db'}",
                        data_dir=str(tmp_path / "data"), admin_api_key="admin-secret",
                        payment_webhook_secret="payment-secret", **updates)
    config = Config("alembic.ini"); config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    transport = FakeTransport(); app = create_app(settings, transport=transport)
    return settings, transport, app


def payload(event_id, phone, text):
    return {"event": "messages.upsert", "instance": "shared", "data": {
        "key": {"id": event_id, "remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False},
        "message": {"conversation": text}}}


def onboard(client, phone="11999991111", prefix="on"):
    answers = ("Oi", "Lucas", "Nova Web Studios", "Tecnologia", "Automatizar processos", "ACEITO")
    for index, answer in enumerate(answers):
        response = client.post("/webhooks/evolution", json=payload(f"{prefix}-{index}", phone, answer))
        assert response.status_code == 200


def test_progressive_onboarding_consent_and_profile(tmp_path):
    _, transport, app = setup(tmp_path)
    with TestClient(app) as client:
        onboard(client)
        assert "período de teste" in transport.sent[-1][2]
    with Session(app.state.engine) as db:
        company = db.scalar(select(Company))
        assert company.access_status == "trial" and company.onboarding_step == "completed"
        consent = db.scalar(select(Consent))
        assert consent and "commercial_analysis" in consent.accepted_scopes
        facts = db.scalars(select(CompanyFact).where(CompanyFact.company_id == company.id)).all()
        assert {x.field for x in facts} == {"company_name", "segment", "primary_objective"}
        assert {x.provenance for x in facts} == {"USER_CONFIRMED"}


def test_access_policy_and_blocked_user(tmp_path):
    _, transport, app = setup(tmp_path)
    with TestClient(app) as client:
        onboard(client)
        with Session(app.state.engine) as db:
            company = db.scalar(select(Company)); company.access_status = "blocked"; db.commit()
        response = client.post("/webhooks/evolution", json=payload("blocked-1", "11999991111", "oi"))
        assert response.status_code == 200
        assert "acesso está bloqueado" in transport.sent[-1][2]


def test_plan_usage_limit_preserves_data(tmp_path):
    _, _, app = setup(tmp_path, max_messages_per_day=1000)
    with TestClient(app) as client:
        onboard(client)
        with Session(app.state.engine) as db:
            plan = db.get(Plan, "starter"); plan.limits = {**plan.limits, "messages_month": 1}; db.commit()
        assert client.post("/webhooks/evolution", json=payload(
            "limit-ok", "11999991111", "primeira mensagem")).status_code == 200
        assert client.post("/webhooks/evolution", json=payload(
            "limit-no", "11999991111", "segunda mensagem")).status_code == 429
    with Session(app.state.engine) as db:
        assert db.scalar(select(Company)).name == "Nova Web Studios"


def test_payment_webhook_is_signed_idempotent_and_changes_access(tmp_path):
    _, _, app = setup(tmp_path)
    with TestClient(app) as client:
        onboard(client)
        with Session(app.state.engine) as db: company_id = db.scalar(select(Company)).id
        event = {"event_id": "pay-1", "event_type": "payment_approved", "company_id": company_id,
                 "plan_code": "starter", "subscription_external_id": "sub-123"}
        body = json.dumps(event, separators=(",", ":")).encode()
        signature = hmac.new(b"payment-secret", body, hashlib.sha256).hexdigest()
        headers = {"x-signature": signature, "content-type": "application/json"}
        first = client.post("/webhooks/payments/generic", content=body, headers=headers)
        second = client.post("/webhooks/payments/generic", content=body, headers=headers)
        assert first.status_code == 200 and not first.json()["duplicate"]
        assert second.json()["duplicate"]
    with Session(app.state.engine) as db:
        assert db.scalar(select(Subscription)).status == "active"
        assert db.scalar(select(Company)).access_status == "active"
        assert db.scalar(select(func.count()).select_from(PaymentEvent)) == 1


def test_opportunity_requires_evidence_and_recommendation_has_cooldown(tmp_path):
    _, transport, app = setup(tmp_path)
    with TestClient(app) as client:
        onboard(client)
        client.post("/webhooks/evolution", json=payload(
            "pain-1", "11999991111", "Fazemos os orçamentos manual e isso demora."))
        client.post("/webhooks/evolution", json=payload(
            "pain-2", "11999991111", "O processo manual de orçamento toma muito tempo."))
        assert "Nova Web Studios pode ajudar" in transport.sent[-1][2]
        client.post("/webhooks/evolution", json=payload(
            "pain-3", "11999991111", "Ainda fazemos esse processo manual."))
        onboard(client, "11999992222", "other")
        client.post("/webhooks/evolution", json=payload("no-pain", "11999992222", "Tudo funciona bem."))
    with Session(app.state.engine) as db:
        companies = db.scalars(select(Company).order_by(Company.id)).all()
        opportunities = db.scalars(select(Opportunity)).all()
        assert len(opportunities) == 1 and len(opportunities[0].evidence) >= 2
        assert db.scalar(select(func.count()).select_from(RecommendationLog)) == 1
        assert sum(db.scalar(select(func.count()).select_from(Opportunity).where(
            Opportunity.company_id == company.id)) for company in companies) == 1


def test_internal_api_is_protected(tmp_path):
    _, _, app = setup(tmp_path)
    with TestClient(app) as client:
        assert client.get("/internal/opportunities").status_code == 401
        assert client.get("/internal/metrics", headers={"x-admin-key": "wrong"}).status_code == 401
        assert client.get("/internal/opportunities", headers={"x-admin-key": "admin-secret"}).status_code == 200


def test_staging_rejects_test_provider_and_missing_credentials():
    settings = Settings(app_environment="staging")
    with pytest.raises(RuntimeError, match="Invalid real-runtime configuration"):
        settings.validate_runtime()


def test_owner_company_lookup_and_manual_activation(tmp_path):
    _, _, app = setup(tmp_path)
    with TestClient(app) as client:
        onboard(client)
        headers = {"x-admin-key": "admin-secret"}
        found = client.get("/internal/companies", params={"phone": "55 11 99999-1111"},
                           headers=headers)
        assert found.status_code == 200
        assert found.json()["access_status"] == "trial"
        company_id = found.json()["company_id"]
        activated = client.patch(f"/internal/companies/{company_id}/access",
                                 json={"status": "active"}, headers=headers)
        assert activated.status_code == 200
        assert activated.json()["status"] == "active"
        assert client.get("/health/providers").status_code == 401


def test_evolution_message_level_base64_is_normalized():
    inbound = normalize_evolution({
        "event": "messages.upsert", "instance": "socio-ia",
        "data": {"key": {"id": "audio-real", "remoteJid": "5511999991111@s.whatsapp.net",
                         "fromMe": False},
                 "message": {"audioMessage": {"mimetype": "audio/ogg; codecs=opus"},
                             "base64": "YWJj"}}
    })
    assert inbound is not None
    assert inbound.message_type == "audio"
    assert inbound.media_base64 == "YWJj"


def test_local_real_runtime_needs_no_domain_but_rejects_deterministic():
    settings = Settings(
        app_environment="local",
        database_url="postgresql+psycopg://user:pass@postgres/db",
        evolution_base_url="http://evolution:8080",
        evolution_api_key="evolution-key",
        evolution_instance="socio-ia",
        evolution_webhook_secret="webhook-key",
        llm_provider="openai-compatible",
        llm_base_url="https://api.groq.com/openai/v1",
        llm_api_key="groq-key",
        llm_model="model",
        transcription_base_url="https://api.groq.com/openai/v1",
        transcription_api_key="groq-key",
        transcription_model="whisper-model",
        admin_api_key="admin-key",
        public_base_url="",
    )
    settings.validate_runtime()
    settings.llm_provider = "deterministic"
    with pytest.raises(RuntimeError, match="LLM_PROVIDER=openai-compatible"):
        settings.validate_runtime()
