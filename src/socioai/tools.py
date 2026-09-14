import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document, DocumentChunk, Job, Memory, Reminder, Task, UsageEvent, now
from .media import deterministic_embedding

logger = logging.getLogger("socioai.tools")


class ToolResult(BaseModel):
    ok: bool
    action: str
    data: dict = Field(default_factory=dict)
    error: str | None = None


class ToolExecutor:
    """Validated, tenant-scoped backend actions. The model never mutates data directly."""

    def _log(self, action: str, company_id: str, **metadata):
        logger.info("tool_executed", extra={"action": action, "company_id": company_id, **metadata})

    def save_memory(self, db: Session, company_id: str, key: str, value: str,
                    source_message_id: str | None = None) -> ToolResult:
        key, value = key.strip().lower(), value.strip()
        if not key or not value or len(key) > 120 or len(value) > 4000:
            return ToolResult(ok=False, action="save_memory", error="invalid memory")
        item = db.scalar(select(Memory).where(Memory.company_id == company_id, Memory.key == key))
        if item:
            item.value, item.source_message_id = value, source_message_id
            action = "update_memory"
        else:
            item = Memory(company_id=company_id, key=key, value=value, source_message_id=source_message_id)
            db.add(item)
            action = "save_memory"
        db.flush(); self._log(action, company_id, key=key)
        return ToolResult(ok=True, action=action, data={"id": item.id, "key": key, "value": value})

    def search_memory(self, db: Session, company_id: str, query: str) -> ToolResult:
        terms = [term for term in query.casefold().split() if len(term) > 2]
        items = db.scalars(select(Memory).where(Memory.company_id == company_id)).all()
        ranked = sorted(items, key=lambda x: sum(t in f"{x.key} {x.value}".casefold() for t in terms), reverse=True)
        matches = [{"id": x.id, "key": x.key, "value": x.value} for x in ranked if not terms or any(
            t in f"{x.key} {x.value}".casefold() for t in terms)][:8]
        self._log("search_memory", company_id, count=len(matches))
        return ToolResult(ok=True, action="search_memory", data={"items": matches})

    def update_memory(self, db: Session, company_id: str, memory_id: str, value: str) -> ToolResult:
        item = db.scalar(select(Memory).where(Memory.company_id == company_id, Memory.id == memory_id))
        if not item:
            return ToolResult(ok=False, action="update_memory", error="memory not found")
        item.value = value.strip(); db.flush(); self._log("update_memory", company_id, id=memory_id)
        return ToolResult(ok=True, action="update_memory", data={"id": item.id, "value": item.value})

    def delete_memory(self, db: Session, company_id: str, memory_id: str | None = None,
                      key: str | None = None) -> ToolResult:
        query = select(Memory).where(Memory.company_id == company_id)
        query = query.where(Memory.id == memory_id) if memory_id else query.where(Memory.key == key)
        item = db.scalar(query)
        if not item:
            return ToolResult(ok=False, action="delete_memory", error="memory not found")
        db.delete(item); db.flush(); self._log("delete_memory", company_id, id=item.id)
        return ToolResult(ok=True, action="delete_memory", data={"id": item.id})

    def store_note(self, db: Session, company_id: str, text: str, source_message_id: str) -> ToolResult:
        return self.save_memory(db, company_id, f"note.{source_message_id}", text, source_message_id)

    def create_task(self, db: Session, company_id: str, title: str,
                    due_at: datetime | None = None) -> ToolResult:
        title = title.strip()
        if not title or len(title) > 500:
            return ToolResult(ok=False, action="create_task", error="invalid task")
        item = Task(company_id=company_id, title=title, due_at=due_at)
        db.add(item); db.flush(); self._log("create_task", company_id, id=item.id)
        return ToolResult(ok=True, action="create_task", data={"id": item.id, "title": title,
            "due_at": due_at.isoformat() if due_at else None, "status": item.status})

    def list_tasks(self, db: Session, company_id: str, start: datetime | None = None,
                   end: datetime | None = None, overdue: bool = False) -> ToolResult:
        query = select(Task).where(Task.company_id == company_id, Task.status == "open")
        if start: query = query.where(Task.due_at >= start)
        if end: query = query.where(Task.due_at < end)
        if overdue: query = query.where(Task.due_at < now())
        items = db.scalars(query.order_by(Task.due_at, Task.created_at)).all()
        data = [{"id": x.id, "title": x.title, "due_at": x.due_at.isoformat() if x.due_at else None} for x in items]
        self._log("list_tasks", company_id, count=len(data))
        return ToolResult(ok=True, action="list_tasks", data={"items": data})

    def complete_task(self, db: Session, company_id: str, task_id: str | None = None) -> ToolResult:
        query = select(Task).where(Task.company_id == company_id, Task.status == "open")
        query = query.where(Task.id == task_id) if task_id else query.order_by(Task.created_at.desc())
        item = db.scalar(query)
        if not item:
            return ToolResult(ok=False, action="complete_task", error="task not found")
        item.status, item.completed_at = "completed", now()
        db.flush(); self._log("complete_task", company_id, id=item.id)
        return ToolResult(ok=True, action="complete_task", data={"id": item.id, "title": item.title})

    def create_reminder(self, db: Session, company_id: str, phone_identity_id: str,
                        instance: str, text: str, due_at: datetime, idempotency_key: str) -> ToolResult:
        existing = db.scalar(select(Reminder).where(Reminder.company_id == company_id,
                                                    Reminder.idempotency_key == idempotency_key))
        if existing:
            return ToolResult(ok=True, action="create_reminder", data={"id": existing.id,
                "due_at": existing.due_at.isoformat(), "status": existing.status, "duplicate": True})
        if due_at <= datetime.now(timezone.utc):
            return ToolResult(ok=False, action="create_reminder", error="reminder must be in the future")
        reminder = Reminder(company_id=company_id, phone_identity_id=phone_identity_id,
            transport_instance=instance, text=text.strip(), due_at=due_at, idempotency_key=idempotency_key)
        db.add(reminder); db.flush()
        job = Job(company_id=company_id, kind="send_reminder", message_id=None,
                  idempotency_key=f"job:{idempotency_key}", payload={"reminder_id": reminder.id},
                  scheduled_for=due_at)
        db.add(job); db.flush(); self._log("create_reminder", company_id, id=reminder.id)
        db.add(UsageEvent(company_id=company_id, kind="reminder_created"))
        return ToolResult(ok=True, action="create_reminder", data={"id": reminder.id,
            "due_at": due_at.isoformat(), "status": reminder.status})

    def list_reminders(self, db: Session, company_id: str) -> ToolResult:
        items = db.scalars(select(Reminder).where(Reminder.company_id == company_id,
            Reminder.status == "queued").order_by(Reminder.due_at)).all()
        return ToolResult(ok=True, action="list_reminders", data={"items": [
            {"id": x.id, "text": x.text, "due_at": x.due_at.isoformat()} for x in items]})

    def get_today_agenda(self, db: Session, company_id: str, start: datetime,
                         end: datetime) -> ToolResult:
        return self._agenda(db, company_id, start, end, "get_today_agenda")

    def get_tomorrow_agenda(self, db: Session, company_id: str, start: datetime,
                            end: datetime) -> ToolResult:
        return self._agenda(db, company_id, start, end, "get_tomorrow_agenda")

    def _agenda(self, db: Session, company_id: str, start: datetime, end: datetime,
                action: str) -> ToolResult:
        tasks = self.list_tasks(db, company_id, start, end).data["items"]
        reminders = db.scalars(select(Reminder).where(
            Reminder.company_id == company_id, Reminder.status == "queued",
            Reminder.due_at >= start, Reminder.due_at < end).order_by(Reminder.due_at)).all()
        data = {"tasks": tasks, "reminders": [
            {"id": x.id, "text": x.text, "due_at": x.due_at.isoformat()} for x in reminders]}
        self._log(action, company_id, count=len(tasks) + len(reminders))
        return ToolResult(ok=True, action=action, data=data)

    def cancel_reminder(self, db: Session, company_id: str, reminder_id: str | None = None) -> ToolResult:
        query = select(Reminder).where(Reminder.company_id == company_id, Reminder.status == "queued")
        query = query.where(Reminder.id == reminder_id) if reminder_id else query.order_by(Reminder.created_at.desc())
        item = db.scalar(query)
        if not item or item.status != "queued":
            return ToolResult(ok=False, action="cancel_reminder", error="active reminder not found")
        item.status = "cancelled"; db.flush(); self._log("cancel_reminder", company_id, id=item.id)
        return ToolResult(ok=True, action="cancel_reminder", data={"id": item.id})

    def get_document(self, db: Session, company_id: str, document_id: str) -> ToolResult:
        item = db.scalar(select(Document).where(Document.company_id == company_id, Document.id == document_id))
        if not item: return ToolResult(ok=False, action="get_document", error="document not found")
        return ToolResult(ok=True, action="get_document", data={"id": item.id, "filename": item.filename,
            "status": item.status})

    def search_documents(self, db: Session, company_id: str, query: str) -> ToolResult:
        if db.bind and db.bind.dialect.name == "postgresql":
            distance = DocumentChunk.embedding.cosine_distance(deterministic_embedding(query))
            rows = db.execute(select(DocumentChunk, Document).join(Document,
                (Document.id == DocumentChunk.document_id) &
                (Document.company_id == DocumentChunk.company_id)).where(
                DocumentChunk.company_id == company_id, Document.status == "ready").order_by(distance).limit(5)).all()
            matches = [{"document_id": d.id, "filename": d.filename, "position": c.position,
                        "content": c.content} for c, d in rows]
            self._log("search_documents", company_id, count=len(matches), mode="pgvector")
            return ToolResult(ok=True, action="search_documents", data={"items": matches})
        terms = [x.strip(".,?!") for x in query.casefold().split() if len(x.strip(".,?!")) > 3]
        chunks = db.execute(select(DocumentChunk, Document).join(Document,
            (Document.id == DocumentChunk.document_id) & (Document.company_id == DocumentChunk.company_id)).where(
            DocumentChunk.company_id == company_id, Document.status == "ready")).all()
        ranked = sorted(chunks, key=lambda row: sum(t in row[0].content.casefold() for t in terms), reverse=True)
        matches = [{"document_id": d.id, "filename": d.filename, "position": c.position, "content": c.content}
                   for c, d in ranked if not terms or any(t in c.content.casefold() for t in terms)][:5]
        self._log("search_documents", company_id, count=len(matches), mode="lexical")
        return ToolResult(ok=True, action="search_documents", data={"items": matches})
