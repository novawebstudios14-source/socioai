from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from socioai.config import Settings
from socioai.main import create_app
from socioai.models import Company, Memory, Message


class FakeTransport:
    def __init__(self): self.sent = []
    def send_text(self, instance, phone, text): self.sent.append((instance, phone, text))


def payload(event_id, phone, text, instance="tenant-a"):
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
        body = payload("same-id", "11999999999", "Minha empresa se chama Alfa.")
        assert client.post("/webhooks/evolution", json=body).json()["status"] == "processed"
        assert client.post("/webhooks/evolution", json=body).json()["status"] == "duplicate"
        body_b = payload("same-id", "11999999999", "Minha empresa se chama Beta.", "tenant-b")
        assert client.post("/webhooks/evolution", json=body_b).json()["status"] == "processed"
    with Session(app.state.engine) as db:
        companies = db.scalars(select(Company)).all()
        assert {c.name for c in companies} == {"Alfa", "Beta"}
        assert len(db.scalars(select(Memory)).all()) == 2
        assert len(db.scalars(select(Message)).all()) == 4


def test_rejects_invalid_secret(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'auth.db'}", evolution_webhook_secret="right")
    with TestClient(create_app(settings, transport=FakeTransport())) as client:
        response = client.post("/webhooks/evolution", headers={"x-api-key": "wrong"},
                               json=payload("1", "11999999999", "oi"))
        assert response.status_code == 401

