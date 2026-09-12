import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fastapi.testclient import TestClient
from socioai.config import Settings
from socioai.main import create_app
from socioai.models import Company, Conversation, PhoneIdentity, User


POSTGRES_URL = os.getenv("TEST_DATABASE_URL")


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send_text(self, instance, phone, text):
        self.sent.append((instance, phone, text))


@pytest.mark.postgres
@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_DATABASE_URL is not configured")
def test_postgres_migration_and_tenant_constraints():
    config = Config("alembic.ini")
    config.attributes["database_url"] = POSTGRES_URL
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(POSTGRES_URL)
    try:
        with Session(engine) as db:
            company_a, company_b = Company(), Company()
            db.add_all([company_a, company_b]); db.flush()
            user_a = User(company_id=company_a.id)
            db.add(user_a); db.flush()
            identity_a = PhoneIdentity(
                company_id=company_a.id, user_id=user_a.id, phone_e164="+5511999991111"
            )
            db.add(identity_a); db.commit()
            db.add(Conversation(company_id=company_b.id, phone_identity_id=identity_a.id))
            with pytest.raises(IntegrityError):
                db.commit()
        transport = FakeTransport()
        with TestClient(create_app(Settings(database_url=POSTGRES_URL), transport=transport)) as client:
            for event_id, phone, company in (
                ("pg-a", "11999991111", "Alfa"),
                ("pg-b", "11999992222", "Beta"),
            ):
                response = client.post("/webhooks/evolution", json={
                    "event": "messages.upsert", "instance": "shared-socio-ia", "data": {
                        "key": {"id": event_id, "remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False},
                        "message": {"conversation": f"Minha empresa se chama {company}."},
                    },
                })
                assert response.status_code == 200
    finally:
        engine.dispose()
        command.downgrade(config, "base")
