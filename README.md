# Sócio IA — Fase 2

MVP multiempresa da Nova Web Studios que recebe mensagens da Evolution API,
resolve empresa e usuário, persiste conversas e memória no PostgreSQL e responde
pelo WhatsApp. A camada de transporte e a camada de IA são substituíveis.

Além da memória conversacional, a Fase 2 inclui ferramentas tenant-scoped,
memória explícita, tarefas, agenda, lembretes persistentes, worker com retries,
PDF com pgvector e áudio com provedor de transcrição substituível.

## Executar

```bash
cp .env.example .env
docker compose up --build
```

API: `http://localhost:8000` · documentação: `/docs` · saúde: `/health`

Configure na Evolution API o webhook `POST /webhooks/evolution`. Quando
`EVOLUTION_WEBHOOK_SECRET` estiver definido, envie-o no header `x-api-key`.

## Testar

```bash
pip install -e '.[dev]'
pytest
```

O teste de aceitação envia “Minha empresa se chama Nova Web Studios.”, recria a
aplicação e confirma que a resposta posterior ainda recupera esse nome.

### Limite de integração PostgreSQL

Use um banco PostgreSQL descartável e execute:

```bash
docker run --rm -d --name socioai-postgres-test -e POSTGRES_PASSWORD=test \
  -e POSTGRES_USER=socioai -e POSTGRES_DB=socioai_test -p 55432:5432 pgvector/pgvector:pg16
TEST_DATABASE_URL=postgresql+psycopg://socioai:test@localhost:55432/socioai_test \
  pytest -m postgres
docker stop socioai-postgres-test
```

O teste aplica a migração Alembic real e valida no PostgreSQL as restrições
compostas que impedem referências entre empresas.

O processo `worker` do Docker Compose consome jobs persistidos. Ele pode ser
reiniciado sem perder lembretes, PDFs ou áudios pendentes.

## Arquitetura (avaliação concisa)

**REUSABLE:** FastAPI/SQLAlchemy, Docker Compose, persistência de mensagens e
injeção de dependências da referência.

**REMOVE:** dashboard e domínio financeiro, Baileys, RAG/PDF/áudio e ferramentas
que pertencem às fases seguintes.

**CHANGE:** identidade passa a ser `PhoneIdentity → User → Company`; WhatsApp e
LLM ficam atrás de interfaces; webhook é validado, normalizado e deduplicado.

**TARGET ARCHITECTURE:** Evolution → webhook fino → serviço de ingestão → banco →
orquestrador → memória/LLM → transporte Evolution.

**PHASE 1 PLAN:** banco multi-tenant, webhook/identidade/deduplicação, memória
persistente, LLM substituível e fluxo completo, cobertos por testes.
