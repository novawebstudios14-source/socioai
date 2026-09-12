from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from socioai.config import Settings
from socioai.main import create_app
from socioai.models import Company, Conversation, Memory, Message, PhoneIdentity, User


class FakeTransport:
    def __init__(self): self.sent = []
    def send_text(self, instance, phone, text): self.sent.append((instance, phone, text))


def payload(event_id, phone, text, instance="shared-socio-ia"):
    return {"event": "messages.upsert", "instance": instance, "data": {
        "key": {"id": event_id, "remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False},
        "pushName": "Lucas", "message": {"conversation": text}}}


def test_remembers_company_after_restart(tmp_path: Path):
    db_url = f"sqlite:///{tmp_path / 'acceptance.db'}"
    settings = Settings(database_url=db_url, evolution_webhook_secret="secret")
    transport = FakeTransport()
    with TestClient(create_app(settings, transport=transport)) as client:
        response = client.post("/webhooks/evolution", headers={"x-api-key": "secret"},
                               json=payload("1", "11999999999", "Minha empresa se chama Nova Web Studios."))
        assert response.status_code == 200
    transport2 = FakeTransport()
    with TestClient(create_app(settings, transport=transport2)) as client:
        response = client.post("/webhooks/evolution", headers={"x-api-key": "secret"},
                               json=payload("2", "11999999999", "Qual é o nome da minha empresa?"))
        assert response.status_code == 200
        assert transport2.sent[-1][2] == "O nome da sua empresa é Nova Web Studios."


def test_deduplicates_and_isolates_tenants(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'isolation.db'}")
    transport = FakeTransport()
    app = create_app(settings, transport=transport)
    with TestClient(app) as client:
        phone_a, phone_b = "11999991111", "11999992222"
        body_a = payload("a-1", phone_a, "Minha empresa se chama Alfa.")
        assert client.post("/webhooks/evolution", json=body_a).json()["status"] == "processed"
        assert client.post("/webhooks/evolution", json=body_a).json()["status"] == "duplicate"
        assert client.post("/webhooks/evolution", json=payload(
            "b-1", phone_b, "Minha empresa se chama Beta.")).json()["status"] == "processed"
        assert client.post("/webhooks/evolution", json=payload(
            "a-2", phone_a, "Qual é o nome da minha empresa?")).json()["status"] == "processed"
        assert transport.sent[-1] == ("shared-socio-ia", "+5511999991111", "O nome da sua empresa é Alfa.")
        assert client.post("/webhooks/evolution", json=payload(
            "b-2", phone_b, "Qual é o nome da minha empresa?")).json()["status"] == "processed"
        assert transport.sent[-1] == ("shared-socio-ia", "+5511999992222", "O nome da sua empresa é Beta.")
    with Session(app.state.engine) as db:
        companies = db.scalars(select(Company)).all()
        assert {c.name for c in companies} == {"Alfa", "Beta"}
        assert len(db.scalars(select(Memory)).all()) == 2
        assert len(db.scalars(select(Message)).all()) == 8


def test_database_rejects_cross_tenant_conversation(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'constraints.db'}")
    app = create_app(settings, transport=FakeTransport())
    with TestClient(app):
        pass
    with Session(app.state.engine) as db:
        company_a, company_b = Company(), Company()
        db.add_all([company_a, company_b]); db.flush()
        user_a = User(company_id=company_a.id)
        db.add(user_a); db.flush()
        phone_a = PhoneIdentity(company_id=company_a.id, user_id=user_a.id, phone_e164="+5511111111111")
        db.add(phone_a); db.commit()
        db.add(Conversation(company_id=company_b.id, phone_identity_id=phone_a.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("cross-tenant foreign key was accepted")


def test_rejects_invalid_secret(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'auth.db'}", evolution_webhook_secret="right")
    with TestClient(create_app(settings, transport=FakeTransport())) as client:
        response = client.post("/webhooks/evolution", headers={"x-api-key": "wrong"},
                               json=payload("1", "11999999999", "oi"))
        assert response.status_code == 401
