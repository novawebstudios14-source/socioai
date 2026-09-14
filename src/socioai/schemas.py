from pydantic import BaseModel


class NormalizedInbound(BaseModel):
    event_id: str
    instance: str
    phone: str
    text: str = ""
    sender_name: str | None = None
    message_type: str = "text"
    media_url: str | None = None
    media_base64: str | None = None
    media_mimetype: str | None = None
    filename: str | None = None


class WebhookResult(BaseModel):
    status: str
    message_id: str | None = None
