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
from socioai.media import deterministic_embedding
from socioai.models import Company, Conversation, Document, DocumentChunk, PhoneIdentity, User
from socioai.tools import ToolExecutor


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
        with TestClient(create_app(Settings(database_url=POSTGRES_URL, require_onboarding=False),
                                   transport=transport)) as client:
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
        with Session(engine) as db:
            identities = db.scalars(select(PhoneIdentity).order_by(PhoneIdentity.phone_e164)).all()
            company_a, company_b = identities[0].company_id, identities[1].company_id
            document = Document(company_id=company_a, filename="contrato.pdf", mimetype="application/pdf",
                sha256="a" * 64, storage_path="/tmp/contract", status="ready")
            db.add(document); db.flush()
            db.add(DocumentChunk(company_id=company_a, document_id=document.id, position=0,
                content="O prazo de pagamento é de 30 dias.",
                embedding=deterministic_embedding("prazo pagamento 30 dias")))
            db.commit()
            result = ToolExecutor().search_documents(db, company_a, "qual o prazo de pagamento")
            assert result.data["items"][0]["document_id"] == document.id
            db.add(DocumentChunk(company_id=company_b, document_id=document.id, position=1,
                content="tentativa cruzada", embedding=deterministic_embedding("tentativa")))
            with pytest.raises(IntegrityError):
                db.commit()
    finally:
        engine.dispose()
        command.downgrade(config, "base")
