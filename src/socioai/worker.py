import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .media import TranscriptionProvider, deterministic_embedding, extract_pdf_chunks
from .models import Document, DocumentChunk, Job, Reminder, now
from .schemas import NormalizedInbound

logger = logging.getLogger("socioai.worker")


class PersistentWorker:
    def __init__(self, session_factory, transport, inbound_service, transcriber: TranscriptionProvider):
        self.session_factory = session_factory
        self.transport = transport
        self.inbound_service = inbound_service
        self.transcriber = transcriber

    def run_once(self, at: datetime | None = None, limit: int = 20) -> int:
        at = at or now()
        with self.session_factory() as db:
            jobs = db.scalars(select(Job).where(
                Job.status.in_(("queued", "failed")), Job.scheduled_for <= at,
                Job.attempts < Job.max_attempts).order_by(Job.scheduled_for).limit(limit).with_for_update(
                    skip_locked=True)).all()
            ids = [job.id for job in jobs]
        processed = 0
        for job_id in ids:
            if self._execute(job_id, at): processed += 1
        return processed

    def _execute(self, job_id: str, at: datetime) -> bool:
        with self.session_factory() as db:
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not job or job.status not in ("queued", "failed") or job.attempts >= job.max_attempts:
                return False
            job.status, job.attempts, job.error = "processing", job.attempts + 1, None
            db.commit()
            try:
                if job.kind == "send_reminder": self._send_reminder(db, job, at)
                elif job.kind == "process_document": self._process_document(db, job)
                elif job.kind == "transcribe_audio": self._transcribe_audio(db, job)
                else: raise ValueError(f"unsupported job kind: {job.kind}")
                job.status, job.completed_at = "completed", now()
                db.commit(); return True
            except Exception as exc:
                logger.exception("job_failed", extra={"job_id": job.id, "kind": job.kind})
                db.rollback()
                job = db.get(Job, job_id)
                job.error = str(exc)[:2000]
                job.status = "failed" if job.attempts >= job.max_attempts else "queued"
                job.scheduled_for = at + timedelta(seconds=min(300, 2 ** job.attempts))
                if job.attempts >= job.max_attempts and job.kind == "process_document":
                    document = db.scalar(select(Document).where(
                        Document.company_id == job.company_id,
                        Document.id == job.payload.get("document_id")))
                    if document: document.status, document.error = "failed", job.error
                if job.attempts >= job.max_attempts and job.kind == "send_reminder":
                    reminder = db.scalar(select(Reminder).where(
                        Reminder.company_id == job.company_id,
                        Reminder.id == job.payload.get("reminder_id")))
                    if reminder: reminder.status = "failed"
                db.commit(); return False

    def _send_reminder(self, db, job: Job, at: datetime):
        reminder = db.scalar(select(Reminder).where(Reminder.company_id == job.company_id,
                                                    Reminder.id == job.payload["reminder_id"]))
        if not reminder or reminder.status == "cancelled": return
        if reminder.sent_at or reminder.status == "completed": return
        phone = reminder.phone_identity_id
        from .models import PhoneIdentity
        identity = db.scalar(select(PhoneIdentity).where(PhoneIdentity.company_id == job.company_id,
                                                         PhoneIdentity.id == phone))
        if not identity: raise RuntimeError("reminder phone identity not found")
        self.transport.send_text(reminder.transport_instance, identity.phone_e164,
                                 f"Lembrete: {reminder.text}")
        reminder.status, reminder.sent_at = "completed", at

    def _process_document(self, db, job: Job):
        document = db.scalar(select(Document).where(Document.company_id == job.company_id,
                                                    Document.id == job.payload["document_id"]))
        if not document or document.status == "ready": return
        document.status = "processing"; db.flush()
        for position, content in enumerate(extract_pdf_chunks(document.storage_path)):
            db.add(DocumentChunk(company_id=job.company_id, document_id=document.id,
                                 position=position, content=content,
                                 embedding=deterministic_embedding(content)))
        document.status = "ready"

    def _transcribe_audio(self, db, job: Job):
        text = self.transcriber.transcribe(job.payload["storage_path"], job.payload.get("mimetype"))
        inbound = NormalizedInbound(event_id=f"transcription:{job.id}", instance=job.payload["instance"],
            phone=job.payload["phone"], text=text, message_type="text")
        result = self.inbound_service.handle(db, inbound)
        if result.status not in ("processed", "duplicate"):
            raise RuntimeError("transcribed message was not processed")
