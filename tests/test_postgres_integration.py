import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from socioai.models import Company, Conversation, PhoneIdentity, User


POSTGRES_URL = os.getenv("TEST_DATABASE_URL")


@pytest.mark.postgres
@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_DATABASE_URL is not configured")
def test_postgres_migration_and_tenant_constraints(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    config = Config("alembic.ini")
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
    finally:
        engine.dispose()
        command.downgrade(config, "base")
