# Sócio IA — Fase 1

MVP multiempresa da Nova Web Studios que recebe mensagens da Evolution API,
resolve empresa e usuário, persiste conversas e memória no PostgreSQL e responde
pelo WhatsApp. A camada de transporte e a camada de IA são substituíveis.

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

