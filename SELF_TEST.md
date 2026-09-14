# Sócio IA — real self-test

## 1. One-time staging setup

Use a Linux host with Docker Compose, ports 80/443 open, and a DNS A/AAAA record
for `PUBLIC_HOSTNAME` pointing to it. HTTPS is issued automatically by Caddy.

```bash
git clone https://github.com/novawebstudios14-source/socioai.git
cd socioai
cp .env.example .env
```

Fill every blank staging value. Recommended low-cost compatible setup:

- `LLM_BASE_URL=https://api.groq.com/openai/v1`
- `LLM_MODEL=llama-3.1-8b-instant`
- `TRANSCRIPTION_BASE_URL=https://api.groq.com/openai/v1`
- `TRANSCRIPTION_MODEL=whisper-large-v3-turbo`
- the same Groq API key may be used for both provider keys
- `DATABASE_URL=postgresql+psycopg://socioai:<password>@postgres:5432/socioai`
- `EVOLUTION_DATABASE_URL=postgresql://socioai:<password>@postgres:5432/evolution`
- `PUBLIC_BASE_URL=https://<your-domain>`

Generate independent random values for `POSTGRES_PASSWORD`,
`EVOLUTION_API_KEY`, `EVOLUTION_WEBHOOK_SECRET`, and `ADMIN_API_KEY`.
Do not commit `.env`.

```bash
docker compose pull
docker compose build
docker compose up -d
docker compose ps
curl -fsS https://<your-domain>/health
```

The API container applies `alembic upgrade head` before starting. PostgreSQL,
documents, Evolution sessions, Redis jobs/cache and Caddy certificates use named
volumes and survive restarts.

## 2. Pair the shared WhatsApp number

Create the instance:

```bash
curl -fsS -X POST http://127.0.0.1:8080/instance/create \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d '{"instanceName":"socio-ia","integration":"WHATSAPP-BAILEYS","qrcode":true}'
```

Open the returned QR code (or call
`GET /instance/connect/socio-ia`) and pair the dedicated Sócio IA WhatsApp.
Then configure only the inbound message event, including media as base64:

```bash
curl -fsS -X POST http://127.0.0.1:8080/webhook/set/socio-ia \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d '{"webhook":{"enabled":true,"url":"https://<your-domain>/webhooks/evolution","webhookByEvents":false,"webhookBase64":true,"headers":{"x-api-key":"<EVOLUTION_WEBHOOK_SECRET>"},"events":["MESSAGES_UPSERT"]}}'
```

Confirm connection and an outbound transport test:

```bash
curl -fsS -H "apikey: $EVOLUTION_API_KEY" \
  http://127.0.0.1:8080/instance/connectionState/socio-ia
curl -fsS -X POST http://127.0.0.1:8080/message/sendText/socio-ia \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d '{"number":"55DDDNUMERO","text":"Sócio IA conectado."}'
```

## 3. First owner activation

From your personal WhatsApp, message the dedicated number and answer, one reply
at a time: your name; `Nova Web Studios`; your segment; your main objective;
and finally `ACEITO`. The company is created from your normalized sender phone
and enters `trial`.

Resolve its server-generated ID and activate it through the protected API:

```bash
curl -fsS -G https://<your-domain>/internal/companies \
  -H "x-admin-key: <ADMIN_API_KEY>" --data-urlencode "phone=55DDDNUMERO"

curl -fsS -X PATCH https://<your-domain>/internal/companies/<COMPANY_ID>/access \
  -H "x-admin-key: <ADMIN_API_KEY>" -H "Content-Type: application/json" \
  -d '{"status":"active"}'
```

Never expose the admin key in a browser, webhook, client application or chat.

## 4. Acceptance messages

### Test 1 — memory

Send: `Minha empresa se chama Nova Web Studios.`

Later send: `Qual é o nome da minha empresa?`

Expected: the persistent answer is Nova Web Studios.

### Test 2 — explicit memory

Send: `Guarda que meu contador se chama Carlos.`

Then: `Quem é meu contador?`

Expected: Carlos.

### Test 3 — task and agenda

Send: `Preciso falar com o Carlos amanhã.`

Then: `O que tenho para amanhã?`

Expected: the task appears.

### Test 4 — reminder

Send: `Me lembra daqui a 5 minutos de testar o lembrete.`

Expected: one real WhatsApp reminder approximately five minutes later.

### Test 5 — audio

Send a voice note: `Me lembra amanhã de revisar uma proposta.`

Expected: real transcription followed by reminder creation.

### Test 6 — PDF

Send a text-based PDF. When processing completes, send `Resume esse PDF.` and
then a factual question whose answer is present in it.

Expected: an answer grounded in that tenant's document.

### Test 7 — restart

```bash
docker compose restart api worker
```

Ask again for the company name. Expected: the memory still exists.

## 5. Operational gate

Run after the worker has been up for at least 30 seconds:

```bash
curl -fsS https://<your-domain>/health/operational
curl -fsS https://<your-domain>/health/providers -H "x-admin-key: <ADMIN_API_KEY>"
docker compose exec api alembic current
docker compose exec postgres pg_isready -U socioai -d socioai
docker compose exec postgres psql -U socioai -d socioai \
  -c "SELECT extname FROM pg_extension WHERE extname='vector';"
docker compose exec api sh -c 'test -w /app/data'
docker compose ps
```

Do not declare the self-test ready until health, provider probes, Evolution
connection, outbound WhatsApp, worker heartbeat, schema head, pgvector and
writable storage all pass.
