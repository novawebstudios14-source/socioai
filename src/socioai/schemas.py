from pydantic import BaseModel


class NormalizedInbound(BaseModel):
    event_id: str
    instance: str
    phone: str
    text: str
    sender_name: str | None = None


class WebhookResult(BaseModel):
    status: str
    message_id: str | None = None

