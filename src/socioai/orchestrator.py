import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .models import Company, Message, PhoneIdentity
from .schemas import NormalizedInbound
from .tools import ToolExecutor


def day_window(company: Company, offset: int = 0):
    zone = ZoneInfo(company.timezone)
    day = datetime.now(zone).date() + timedelta(days=offset)
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def natural_due(text: str, company: Company, default_hour: int = 9) -> datetime | None:
    lower = text.casefold()
    zone = ZoneInfo(company.timezone)
    local_now = datetime.now(zone)
    if "amanhã" in lower or "amanha" in lower:
        day = local_now.date() + timedelta(days=1)
    elif "hoje" in lower:
        day = local_now.date()
    else:
        return None
    match = re.search(r"(?:às|as)\s*(\d{1,2})(?::(\d{2}))?", lower)
    hour, minute = (int(match.group(1)), int(match.group(2) or 0)) if match else (default_hour, 0)
    if hour > 23 or minute > 59:
        return None
    return datetime.combine(day, time(hour, minute), zone).astimezone(timezone.utc)


class ActionOrchestrator:
    def __init__(self, tools: ToolExecutor):
        self.tools = tools

    def execute(self, db: Session, company: Company, identity: PhoneIdentity, message: Message,
                inbound: NormalizedInbound) -> str | None:
        text, lower = inbound.text.strip(), inbound.text.casefold().strip()

        match = re.search(r"guarda que\s+(.+?)\s+é noss[oa]\s+([\wáàâãéêíóôõúç]+)", text, re.I)
        if match:
            person, role = match.group(1).strip(), self._role(match.group(2))
            result = self.tools.save_memory(db, company.id, f"business.contact.{role}", person, message.id)
            return f"Guardado: {person} é nosso(a) {role}." if result.ok else f"Não consegui guardar: {result.error}."

        match = re.search(r"noss[oa]\s+([\wáàâãéêíóôõúç]+)\s+agora é\s+(.+?)[.!?]*$", text, re.I)
        if match:
            role, person = self._role(match.group(1)), match.group(2).strip()
            result = self.tools.save_memory(db, company.id, f"business.contact.{role}", person, message.id)
            return f"Atualizado: nosso(a) {role} agora é {person}." if result.ok else f"Não consegui atualizar: {result.error}."

        match = re.search(r"quem é noss[oa]\s+([\wáàâãéêíóôõúç]+)", lower)
        if match:
            role = self._role(match.group(1))
            result = self.tools.search_memory(db, company.id, role)
            items = result.data["items"]
            return f"Nosso(a) {role} é {items[0]['value']}." if items else f"Ainda não sei quem é nosso(a) {role}."

        if lower.startswith("esquece"):
            role = next((self._role(x) for x in ("contador", "contadora") if x in lower), None)
            if not role:
                return "Qual informação você quer que eu esqueça?"
            result = self.tools.delete_memory(db, company.id, key=f"business.contact.{role}")
            return "Informação esquecida." if result.ok else "Não encontrei essa informação."

        match = re.match(r"anota(?: que)?\s+(.+)", text, re.I)
        if match:
            result = self.tools.store_note(db, company.id, match.group(1).strip(), message.id)
            return "Anotado." if result.ok else f"Não consegui anotar: {result.error}."

        match = re.match(r"preciso\s+(.+?)[.!?]*$", text, re.I)
        if match:
            due = natural_due(text, company)
            title = re.sub(r"\s+(?:hoje|amanhã|amanha)[.!?]*$", "", match.group(1), flags=re.I).strip()
            result = self.tools.create_task(db, company.id, title, due)
            return f"Tarefa criada: {title}." if result.ok else f"Não consegui criar a tarefa: {result.error}."

        if "marca isso como concluído" in lower or "marque isso como concluído" in lower:
            result = self.tools.complete_task(db, company.id)
            return f"Tarefa concluída: {result.data['title']}." if result.ok else "Não encontrei tarefa aberta para concluir."

        reminder_match = re.match(r"me lembra\s+(.+?)\s+de\s+(.+?)[.!?]*$", text, re.I)
        if reminder_match:
            due = natural_due(reminder_match.group(1), company)
            if not due:
                return "Para quando devo criar esse lembrete?"
            subject = reminder_match.group(2).strip()
            result = self.tools.create_reminder(db, company.id, identity.id, inbound.instance,
                                                subject, due, f"reminder:{message.external_id}")
            if not result.ok:
                return f"Não consegui criar o lembrete: {result.error}."
            local_due = due.astimezone(ZoneInfo(company.timezone))
            return f"Lembrete criado para {local_due:%d/%m às %H:%M}: {subject}."

        if "quais lembretes" in lower:
            result = self.tools.list_reminders(db, company.id)
            return self._format_items(result.data["items"], "text", "Nenhum lembrete ativo.")

        if "cancela esse lembrete" in lower or "cancele esse lembrete" in lower:
            result = self.tools.cancel_reminder(db, company.id)
            return "Lembrete cancelado." if result.ok else "Não encontrei lembrete ativo para cancelar."

        match = re.search(r"o que (?:você|voce) lembra sobre\s+(.+?)[?]*$", lower)
        if match:
            result = self.tools.search_memory(db, company.id, match.group(1))
            items = result.data["items"]
            return self._format_items(items, "value", "Não encontrei memória sobre isso.")

        if any(x in lower for x in ("o que tenho hoje", "tarefas para hoje")):
            start, end = day_window(company)
            result = self.tools.get_today_agenda(db, company.id, start, end)
            return self._format_agenda(result.data, "Nada agendado para hoje.")

        if any(x in lower for x in ("o que tenho amanhã", "o que tenho amanha")):
            start, end = day_window(company, 1)
            result = self.tools.get_tomorrow_agenda(db, company.id, start, end)
            return self._format_agenda(result.data, "Nada agendado para amanhã.")

        if "atrasad" in lower or "ficou pendente" in lower:
            result = self.tools.list_tasks(db, company.id, overdue=True)
            return self._format_items(result.data["items"], "title", "Nenhuma tarefa atrasada.")

        if "tarefas" in lower and ("abertas" in lower or "tenho" in lower):
            result = self.tools.list_tasks(db, company.id)
            return self._format_items(result.data["items"], "title", "Nenhuma tarefa aberta.")

        if any(x in lower for x in ("contrato", "documento", "prazo de pagamento", "tem multa")):
            result = self.tools.search_documents(db, company.id, text)
            items = result.data["items"]
            if items:
                excerpt = self._relevant_sentence(items[0]["content"], lower)
                return f"Segundo {items[0]['filename']}: {excerpt}"
            return "Não encontrei essa informação nos seus documentos."
        return None

    @staticmethod
    def _format_items(items: list[dict], field: str, empty: str) -> str:
        return empty if not items else "\n".join(f"- {item[field]}" for item in items)

    @staticmethod
    def _format_agenda(data: dict, empty: str) -> str:
        lines = [f"- Tarefa: {x['title']}" for x in data["tasks"]]
        lines += [f"- Lembrete: {x['text']}" for x in data["reminders"]]
        return "\n".join(lines) if lines else empty

    @staticmethod
    def _relevant_sentence(content: str, query: str) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", content.strip())
        terms = [x for x in query.split() if len(x) > 4]
        return max(sentences, key=lambda s: sum(t in s.casefold() for t in terms), default=content[:500])[:700]

    @staticmethod
    def _role(role: str) -> str:
        return {"contadora": "contador"}.get(role.casefold(), role.casefold())
