import base64
import hashlib
import math
from pathlib import Path
from typing import Protocol

import httpx
from pypdf import PdfReader

from .schemas import NormalizedInbound


MAX_MEDIA_BYTES = 20 * 1024 * 1024


class MediaStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def save(self, company_id: str, inbound: NormalizedInbound) -> tuple[str, bytes]:
        if inbound.media_base64:
            content = base64.b64decode(inbound.media_base64, validate=True)
        elif inbound.media_url:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.get(inbound.media_url)
                response.raise_for_status()
                content = response.content
        else:
            raise ValueError("media content is missing")
        if not content or len(content) > MAX_MEDIA_BYTES:
            raise ValueError("media size is invalid")
        folder = self.root / company_id
        folder.mkdir(parents=True, exist_ok=True)
        suffix = ".pdf" if inbound.message_type == "document" else ".audio"
        safe_id = hashlib.sha256(inbound.event_id.encode()).hexdigest()
        path = folder / f"{safe_id}{suffix}"
        path.write_bytes(content)
        return str(path), content


class TranscriptionProvider(Protocol):
    def transcribe(self, path: str, mimetype: str | None = None) -> str: ...


class OpenAICompatibleTranscriber:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model

    def transcribe(self, path: str, mimetype: str | None = None) -> str:
        if not self.api_key:
            raise RuntimeError("transcription provider is not configured")
        with open(path, "rb") as handle:
            response = httpx.post(f"{self.base_url}/audio/transcriptions", timeout=60,
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={"model": self.model, "language": "pt"},
                files={"file": (Path(path).name, handle, mimetype or "application/octet-stream")})
        response.raise_for_status()
        return response.json()["text"].strip()


def deterministic_embedding(text: str, dimensions: int = 384) -> list[float]:
    vector = [0.0] * dimensions
    for token in text.casefold().split():
        digest = hashlib.sha256(token.encode()).digest()
        vector[int.from_bytes(digest[:2], "big") % dimensions] += 1 if digest[2] % 2 else -1
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def extract_pdf_chunks(path: str, size: int = 1400, overlap: int = 150) -> list[str]:
    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if not text:
        raise ValueError("PDF has no extractable text")
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end].strip())
        if end == len(text): break
        start = end - overlap
    return chunks
