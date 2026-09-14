from typing import Any, Protocol

import httpx

from .schemas import NormalizedInbound


class WhatsAppTransport(Protocol):
    def send_text(self, instance: str, phone: str, text: str) -> None: ...


class EvolutionWhatsAppTransport:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def send_text(self, instance: str, phone: str, text: str) -> None:
        headers = {"apikey": self.api_key} if self.api_key else {}
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"{self.base_url}/message/sendText/{instance}",
                headers=headers,
                json={"number": phone.lstrip("+"), "text": text},
            )
            response.raise_for_status()


def normalize_evolution(payload: dict[str, Any]) -> NormalizedInbound | None:
    event = str(payload.get("event", "")).lower()
    if event and "message" not in event:
        return None
    data = payload.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None
    message = data.get("message") or {}
    text = message.get("conversation") or (message.get("extendedTextMessage") or {}).get("text") or ""
    document = message.get("documentMessage") or {}
    audio = message.get("audioMessage") or {}
    media = document or audio
    message_type = "document" if document else "audio" if audio else "text"
    remote = key.get("remoteJid") or data.get("remoteJid")
    event_id = key.get("id") or data.get("id")
    instance = payload.get("instance") or data.get("instance")
    if not all((remote, event_id, instance)) or (not text and not media):
        return None
    return NormalizedInbound(
        event_id=str(event_id), instance=str(instance), phone=str(remote), text=str(text),
        sender_name=data.get("pushName"),
        message_type=message_type,
        media_url=media.get("url"),
        media_base64=media.get("base64") or message.get("base64") or data.get("base64"),
        media_mimetype=media.get("mimetype"),
        filename=document.get("fileName"),
    )
