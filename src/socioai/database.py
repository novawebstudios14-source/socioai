from collections.abc import Generator

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


def build_engine(settings: Settings):
    kwargs = {"connect_args": {"check_same_thread": False}} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, pool_pre_ping=True, **kwargs)
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def build_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def assert_schema_current(engine, config_path: str = "alembic.ini") -> None:
    """Fail startup when the database has not been migrated to Alembic head."""
    expected = ScriptDirectory.from_config(Config(config_path)).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != expected:
        raise RuntimeError(
            f"Database schema is not current (database={current!r}, expected={expected!r}). "
            "Run 'alembic upgrade head' before starting the application."
        )


def session_dependency(factory) -> Generator[Session, None, None]:
    with factory() as session:
        yield session
