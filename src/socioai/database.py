from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def build_engine(settings: Settings):
    kwargs = {"connect_args": {"check_same_thread": False}} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, pool_pre_ping=True, **kwargs)


def build_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_dependency(factory) -> Generator[Session, None, None]:
    with factory() as session:
        yield session

