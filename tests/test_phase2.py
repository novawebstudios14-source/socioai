import base64
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.orm import Session

from socioai.config import Settings
from socioai.main import create_app
from socioai.models import Document, DocumentChunk, Job, Memory, PhoneIdentity, Reminder, Task
from socioai.tools import ToolExecutor


class FakeTransport:
    def __init__(self): self.sent = []
    def send_text(self, instance, phone, text): self.sent.append((instance, phone, text))


class FakeTranscriber:
    def __init__(self, text): self.text = text
    def transcribe(self, path, mimetype=None): return self.text


class FailingTransport(FakeTransport):
    def send_text(self, instance, phone, text):
        self.sent.append((instance, phone, text))
        if text.startswith("Lembrete:"):
            raise TimeoutError("provider timeout")


def settings_for(tmp_path: Path, name="phase2.db"):
    settings = Settings(database_url=f"sqlite:///{tmp_path / name}", data_dir=str(tmp_path / "data"))
    config = Config("alembic.ini"); config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    return settings


def text_payload(event_id, phone, text):
    return {"event": "messages.upsert", "instance": "shared", "data": {
        "key": {"id": event_id, "remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False},
        "message": {"conversation": text}}}


def media_payload(event_id, phone, kind, content, mimetype, filename=None):
    field = "documentMessage" if kind == "document" else "audioMessage"
    media = {"base64": base64.b64encode(content).decode(), "mimetype": mimetype}
    if filename: media["fileName"] = filename
    return {"event": "messages.upsert", "instance": "shared", "data": {
        "key": {"id": event_id, "remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False},
        "message": {field: media}}}


def make_pdf(text: str) -> bytes:
    output = BytesIO(); pdf = canvas.Canvas(output)
    pdf.drawString(72, 760, text); pdf.save()
    return output.getvalue()


def test_explicit_memory_survives_restart(tmp_path):
    settings = settings_for(tmp_path)
    phone = "11999991111"
    with TestClient(create_app(settings, transport=FakeTransport())) as client:
        assert client.post("/webhooks/evolution", json=text_payload(
            "m-1", phone, "Guarda que Carlos é nosso contador.")).status_code == 200
    transport = FakeTransport()
    with TestClient(create_app(settings, transport=transport)) as client:
        client.post("/webhooks/evolution", json=text_payload("m-2", phone, "Quem é nosso contador?"))
    assert transport.sent[-1][2] == "Nosso(a) contador é Carlos."


def test_reminder_persists_restart_and_executes_once(tmp_path):
    settings = settings_for(tmp_path, "reminder.db")
    phone = "11999991111"
    first_transport = FakeTransport()
    with TestClient(create_app(settings, transport=first_transport)) as client:
        client.post("/webhooks/evolution", json=text_payload(
            "r-1", phone, "Me lembra amanhã às 10 de falar com o contador."))
        with Session(client.app.state.engine) as db:
            reminder = db.scalar(select(Reminder))
            assert reminder.status == "queued"
            due_at = reminder.due_at
            assert db.scalar(select(Job).where(Job.kind == "send_reminder")).status == "queued"
    transport = FakeTransport()
    with TestClient(create_app(settings, transport=transport)) as client:
        worker = client.app.state.worker
        assert worker.run_once(at=due_at + timedelta(seconds=1)) == 1
        assert worker.run_once(at=due_at + timedelta(seconds=1)) == 0
    assert [x for x in transport.sent if x[2].startswith("Lembrete:")] == [
        ("shared", "+5511999991111", "Lembrete: falar com o contador")]


def test_pdf_is_grounded_and_tenant_scoped(tmp_path):
    settings = settings_for(tmp_path, "documents.db")
    transport = FakeTransport(); app = create_app(settings, transport=transport)
    pdf = make_pdf("Contrato Alfa. O prazo de pagamento e de 30 dias. Existe multa de 2 por cento.")
    with TestClient(app) as client:
        client.post("/webhooks/evolution", json=media_payload(
            "d-1", "11999991111", "document", pdf, "application/pdf", "contrato-alfa.pdf"))
        assert client.app.state.worker.run_once() == 1
        client.post("/webhooks/evolution", json=text_payload(
            "d-2", "11999991111", "Qual é o prazo de pagamento desse contrato?"))
        assert "30 dias" in transport.sent[-1][2]
        client.post("/webhooks/evolution", json=text_payload(
            "d-3", "11999992222", "Qual é o prazo de pagamento desse contrato?"))
        assert transport.sent[-1][2] == "Não encontrei essa informação nos seus documentos."
    with Session(app.state.engine) as db:
        document = db.scalar(select(Document))
        chunks = db.scalars(select(DocumentChunk).where(DocumentChunk.company_id == document.company_id)).all()
        assert document.status == "ready" and chunks and len(chunks[0].embedding) == 384


def test_audio_transcription_creates_reminder(tmp_path):
    settings = settings_for(tmp_path, "audio.db")
    transport = FakeTransport()
    app = create_app(settings, transport=transport,
                     transcriber=FakeTranscriber("Me lembra amanhã de mandar a proposta."))
    with TestClient(app) as client:
        client.post("/webhooks/evolution", json=media_payload(
            "a-1", "11999991111", "audio", b"fake audio", "audio/ogg"))
        assert client.app.state.worker.run_once() == 1
    with Session(app.state.engine) as db:
        reminder = db.scalar(select(Reminder))
        assert reminder and reminder.text == "mandar a proposta" and reminder.status == "queued"


def test_tool_layer_enforces_tenant_scope(tmp_path):
    settings = settings_for(tmp_path, "tools.db")
    app = create_app(settings, transport=FakeTransport())
    with TestClient(app) as client:
        client.post("/webhooks/evolution", json=text_payload("t-a", "11999991111", "oi"))
        client.post("/webhooks/evolution", json=text_payload("t-b", "11999992222", "oi"))
    with Session(app.state.engine) as db:
        identities = db.scalars(select(PhoneIdentity).order_by(PhoneIdentity.phone_e164)).all()
        company_a, company_b = identities[0].company_id, identities[1].company_id
        tools = ToolExecutor()
        task = tools.create_task(db, company_a, "Segredo da Alfa")
        db.commit()
        assert tools.list_tasks(db, company_b).data["items"] == []
        assert not tools.complete_task(db, company_b, task.data["id"]).ok
        assert tools.search_memory(db, company_b, "Alfa").data["items"] == []


def test_tasks_and_daily_agenda(tmp_path):
    settings = settings_for(tmp_path, "tasks.db")
    transport = FakeTransport(); app = create_app(settings, transport=transport)
    with TestClient(app) as client:
        client.post("/webhooks/evolution", json=text_payload(
            "task-1", "11999991111", "Preciso mandar proposta pro Carlos amanhã."))
        assert transport.sent[-1][2] == "Tarefa criada: mandar proposta pro Carlos."
        client.post("/webhooks/evolution", json=text_payload(
            "task-2", "11999991111", "O que tenho amanhã?"))
        assert "mandar proposta pro Carlos" in transport.sent[-1][2]
        client.post("/webhooks/evolution", json=text_payload(
            "task-3", "11999991111", "Marca isso como concluído."))
        assert "Tarefa concluída" in transport.sent[-1][2]
    with Session(app.state.engine) as db:
        assert db.scalar(select(Task)).status == "completed"


def test_worker_retries_are_bounded(tmp_path):
    settings = settings_for(tmp_path, "retries.db")
    transport = FailingTransport(); app = create_app(settings, transport=transport)
    with TestClient(app) as client:
        client.post("/webhooks/evolution", json=text_payload(
            "retry-1", "11999991111", "Me lembra amanhã às 10 de testar retries."))
        with Session(client.app.state.engine) as db:
            due = db.scalar(select(Reminder)).due_at
        for minutes in (1, 2, 3, 10):
            client.app.state.worker.run_once(at=due + timedelta(minutes=minutes))
    with Session(app.state.engine) as db:
        job = db.scalar(select(Job).where(Job.kind == "send_reminder"))
        reminder = db.scalar(select(Reminder))
        assert job.status == "failed" and job.attempts == job.max_attempts == 3
        assert reminder.status == "failed"
    assert len([x for x in transport.sent if x[2].startswith("Lembrete:")]) == 3


def test_usage_limit_returns_429(tmp_path):
    settings = settings_for(tmp_path, "limits.db").model_copy(update={"max_messages_per_day": 1})
    with TestClient(create_app(settings, transport=FakeTransport())) as client:
        assert client.post("/webhooks/evolution", json=text_payload(
            "limit-1", "11999991111", "oi")).status_code == 200
        assert client.post("/webhooks/evolution", json=text_payload(
            "limit-2", "11999991111", "oi novamente")).status_code == 429
