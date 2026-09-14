# Closed beta runbook

## SETUP

Use one shared Evolution instance, PostgreSQL/pgvector, the API and one worker.
Copy `.env.example` to `.env`; never commit secrets.

## REQUIRED ENV VARIABLES

Set `DATABASE_URL`, Evolution URL/key/webhook secret, `ADMIN_API_KEY`, versioned
terms/privacy values and the chosen AI/transcription keys. Keep
`REQUIRE_ONBOARDING=true`.

## EVOLUTION SETUP

Point `messages.upsert` to `POST /webhooks/evolution` and send the configured
`x-api-key`. Text, PDF and audio are accepted. Keep the Evolution session volume.

## AI PROVIDER SETUP

Set the OpenAI-compatible base URL, model and key. Without a transcription key,
audio jobs enter bounded retry and expose a degraded provider state.

## DATABASE / MIGRATIONS / WORKER

Use `pgvector/pgvector:pg16`. Run `alembic upgrade head` before starting API or
worker. The Compose stack does this for the API; the worker refuses an outdated
schema. Inspect queued/failed jobs at `/health/operational`.

## PAYMENT SETUP

No Perfect Pay/InfinitePay credentials are currently validated. The generic HMAC
contract is available at `/webhooks/payments/generic`; set
`PAYMENT_WEBHOOK_SECRET`. Replace it with a documented provider adapter before
live billing. Never mark a payment successful manually without evidence.

## BACKUP

Back up PostgreSQL daily and uploaded files from `DATA_DIR` together. Recommended
retention: 7 daily, 4 weekly and 3 monthly copies, encrypted outside the host.
Run `BACKUP_DIR=/backups DATABASE_URL=... DATA_DIR=/app/data bash ops/backup.sh`.
Restore only into an isolated recovery database first with
`CONFIRM_RESTORE=yes BACKUP_FILE=... bash ops/restore.sh`, restore the matching
file archive, run migrations and acceptance tests, then document the result.
This repository documents the procedure; a production restoration has not yet
been claimed as tested.

## COMMON FAILURES

- 401 webhook: verify secret headers.
- Startup refusal: run `alembic upgrade head`.
- Failed audio: check transcription credentials/timeouts.
- Failed PDF: verify real PDF content and storage permissions.
- Failed reminder: check Evolution health and retry count.
- Payment degraded: configure the webhook secret/provider adapter.

## USER OPERATIONS

New users onboard progressively in WhatsApp and enter `trial` after `ACEITO`.
Activation normally comes from a signed payment event. Authorized Nova operators
may use `PATCH /internal/companies/{id}/access` with `x-admin-key` to activate or
block a company. Validate the returned company and never expose this key publicly.

## FAILED JOB INSPECTION

Use the protected metrics API and `/health/operational`, then query `jobs` by ID.
Review `kind`, `attempts`, `error` and timestamps; do not log message/document
contents. Fix the provider cause before re-queuing within `max_attempts`.
