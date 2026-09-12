from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import Base, build_engine, build_session_factory, session_dependency
from .evolution import EvolutionWhatsAppTransport, normalize_evolution
from .llm import DeterministicLLM, OpenAICompatibleLLM
from .service import InboundService


def create_app(settings: Settings | None = None, transport=None, llm=None) -> FastAPI:
    settings = settings or get_settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    transport = transport or EvolutionWhatsAppTransport(settings.evolution_base_url, settings.evolution_api_key)
    llm = llm or (OpenAICompatibleLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
                  if settings.llm_provider == "openai-compatible" else DeterministicLLM())
    service = InboundService(llm, transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(engine)
        yield
        engine.dispose()

    app = FastAPI(title="Sócio IA", version="0.1.0", lifespan=lifespan)

    def get_db():
        yield from session_dependency(factory)

    @app.get("/health")
    def health():
        return {"status": "ok"}

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
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    app.state.engine = engine
    return app


app = create_app()

