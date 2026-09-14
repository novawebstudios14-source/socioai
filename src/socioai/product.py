from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (Company, CompanyFact, Consent, Memory, Message, Opportunity, Plan,
                     RecommendationLog, Subscription, UsageEvent, User, PhoneIdentity)


DEFAULT_LIMITS = {
    "messages_month": 500, "llm_calls_month": 300, "documents_month": 20,
    "document_size_bytes": 20 * 1024 * 1024, "audio_minutes_month": 120,
    "active_reminders": 100, "storage_bytes": 500 * 1024 * 1024,
    "memory_items": 500,
}


def ensure_product_records(db: Session, company: Company) -> Subscription:
    plan = db.get(Plan, "starter")
    if not plan:
        plan = Plan(code="starter", name="Sócio IA Inicial", limits=DEFAULT_LIMITS)
        db.add(plan); db.flush()
    subscription = db.scalar(select(Subscription).where(Subscription.company_id == company.id))
    if not subscription:
        subscription = Subscription(company_id=company.id, plan_code=plan.code, status="trial")
        db.add(subscription); db.flush()
    return subscription


@dataclass
class AccessDecision:
    allowed: bool
    reason: str | None = None


class AccessPolicy:
    ALLOWED = {"trial", "active"}

    def decide(self, db: Session, company: Company) -> AccessDecision:
        if company.access_status == "pending_onboarding":
            return AccessDecision(False, "onboarding")
        if company.access_status not in self.ALLOWED:
            messages = {
                "past_due": "Seu acesso está suspenso por pendência de pagamento.",
                "cancelled": "Sua assinatura está cancelada.",
                "blocked": "Seu acesso está bloqueado. Fale com o suporte da Nova Web Studios.",
            }
            return AccessDecision(False, messages.get(company.access_status, "Acesso indisponível."))
        subscription = ensure_product_records(db, company)
        if subscription.status not in self.ALLOWED:
            return AccessDecision(False, "Sua assinatura não permite uso no momento.")
        return AccessDecision(True)


class UsagePolicy:
    KIND_LIMIT = {
        "inbound_message": "messages_month", "llm_call": "llm_calls_month",
        "document_upload": "documents_month", "audio_minute": "audio_minutes_month",
    }

    def check(self, db: Session, company_id: str, kind: str, units: int = 1) -> AccessDecision:
        subscription = db.scalar(select(Subscription).where(Subscription.company_id == company_id))
        plan = db.get(Plan, subscription.plan_code) if subscription else None
        key = self.KIND_LIMIT.get(kind)
        if not plan or not key: return AccessDecision(True)
        limit = plan.limits.get(key)
        if limit is None: return AccessDecision(True)
        month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        used = db.scalar(select(func.coalesce(func.sum(UsageEvent.units), 0)).where(
            UsageEvent.company_id == company_id, UsageEvent.kind == kind,
            UsageEvent.created_at >= month))
        if used + units > limit:
            return AccessDecision(False, f"Você atingiu o limite mensal de {key.replace('_', ' ')} do seu plano.")
        return AccessDecision(True)


class CompanyProfileService:
    def confirm(self, db: Session, company_id: str, field: str, value: str,
                source_type: str, source_id: str, provenance: str = "USER_CONFIRMED"):
        fact = db.scalar(select(CompanyFact).where(CompanyFact.company_id == company_id,
                                                   CompanyFact.field == field))
        if fact:
            fact.value, fact.provenance, fact.source_type, fact.source_id = value, provenance, source_type, source_id
        else:
            fact = CompanyFact(company_id=company_id, field=field, value=value, provenance=provenance,
                               source_type=source_type, source_id=source_id)
            db.add(fact)
        db.flush(); return fact


class OnboardingService:
    TERMS = (
        "Para usar o Sócio IA, preciso do seu consentimento. Conversas serão armazenadas; documentos "
        "enviados poderão ser processados; memórias e informações operacionais serão persistidas para "
        "prestar o serviço. Com sua autorização, sinais operacionais também poderão ser analisados para "
        "identificar soluções tecnológicas relevantes da Nova Web Studios — sem inventar problemas. "
        "Digite ACEITO para concordar ou NÃO ACEITO para encerrar."
    )

    def __init__(self, terms_version: str, privacy_version: str):
        self.terms_version, self.privacy_version = terms_version, privacy_version
        self.profile = CompanyProfileService()

    def handle(self, db: Session, company: Company, user: User, identity: PhoneIdentity,
               message: Message) -> str:
        text, lower = message.content.strip(), message.content.casefold().strip()
        step = company.onboarding_step
        if step == "name":
            if lower in {"oi", "olá", "ola", "bom dia", "boa tarde", "boa noite"}:
                return "Olá! Para começar, como você se chama?"
            user.display_name = text; company.onboarding_step = "company"
            return f"Prazer, {text}! Qual é o nome da sua empresa?"
        if step == "company":
            company.name, company.onboarding_step = text, "segment"
            self.profile.confirm(db, company.id, "company_name", text, "message", message.id)
            db.add(Memory(company_id=company.id, key="company.name", value=text,
                          source_message_id=message.id))
            return "Qual é o segmento principal da empresa?"
        if step == "segment":
            company.segment, company.onboarding_step = text, "objective"
            self.profile.confirm(db, company.id, "segment", text, "message", message.id)
            return "Qual é o principal objetivo que você quer alcançar com o Sócio IA?"
        if step == "objective":
            company.primary_objective, company.onboarding_step = text, "consent"
            self.profile.confirm(db, company.id, "primary_objective", text, "message", message.id)
            return self.TERMS
        if step == "consent":
            if lower not in {"aceito", "eu aceito", "concordo"}:
                return "Sem consentimento não consigo ativar o serviço. Se concordar, digite ACEITO."
            scopes = ["service_data", "document_processing", "persistent_memory", "commercial_analysis"]
            db.add(Consent(company_id=company.id, user_id=user.id, phone_identity_id=identity.id,
                           terms_version=self.terms_version, privacy_version=self.privacy_version,
                           accepted_scopes=scopes))
            company.onboarding_step, company.access_status = "completed", "trial"
            ensure_product_records(db, company)
            return "Tudo certo! Seu período de teste foi ativado e o Sócio IA já está pronto para ajudar."
        return "Seu cadastro já está concluído."


class OpportunityEngine:
    PAINS = {
        "manual": ("manual_process", "Processo manual recorrente", "Automação do processo"),
        "planilha": ("data_management", "Dependência recorrente de planilhas", "Sistema integrado de dados"),
        "sem crm": ("CRM", "Ausência de CRM", "Implantação de CRM"),
        "site": ("website", "Necessidade relacionada ao site", "Website orientado à conversão"),
    }

    def analyze(self, db: Session, company_id: str) -> Opportunity | None:
        consent = db.scalar(select(Consent).where(Consent.company_id == company_id).order_by(
            Consent.accepted_at.desc()))
        if not consent or "commercial_analysis" not in consent.accepted_scopes: return None
        messages = db.scalars(select(Message).where(Message.company_id == company_id).order_by(
            Message.created_at.desc()).limit(30)).all()
        for token, (category, title, solution) in self.PAINS.items():
            evidence = [m for m in messages if m.direction.value == "inbound" and token in m.content.casefold()]
            if len(evidence) < 2: continue
            existing = db.scalar(select(Opportunity).where(Opportunity.company_id == company_id,
                                                            Opportunity.category == category,
                                                            Opportunity.status.notin_(("lost", "ignored"))))
            if existing: return existing
            excerpts = [{"source_type": "message", "source_id": m.id, "excerpt": m.content[:220]}
                        for m in evidence[:3]]
            confidence = min(0.95, 0.65 + 0.1 * len(evidence))
            opportunity = Opportunity(company_id=company_id, category=category, title=title,
                problem=f"O usuário mencionou {token} repetidamente.", evidence=excerpts,
                business_impact="Possível perda de tempo e aumento de erros operacionais.",
                suggested_solution=solution, confidence=confidence,
                priority="high" if len(evidence) >= 3 else "medium")
            db.add(opportunity); db.flush(); return opportunity
        return None

    def recommendation(self, db: Session, opportunity: Opportunity, confidence: float,
                       cooldown_days: int) -> str | None:
        if opportunity.confidence < confidence: return None
        cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
        recent = db.scalar(select(RecommendationLog).where(
            RecommendationLog.company_id == opportunity.company_id,
            RecommendationLog.sent_at >= cutoff))
        if recent: return None
        db.add(RecommendationLog(company_id=opportunity.company_id, opportunity_id=opportunity.id))
        return (f"Você comentou mais de uma vez sobre {opportunity.problem.lower()} "
                f"Isso pode afetar a operação. A Nova Web Studios pode ajudar com "
                f"{opportunity.suggested_solution.lower()}. Quer ver como funcionaria?")
