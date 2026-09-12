from typing import Protocol

import httpx


class LLMProvider(Protocol):
    def reply(self, message: str, memories: dict[str, str], history: list[tuple[str, str]]) -> str: ...


class DeterministicLLM:
    def reply(self, message: str, memories: dict[str, str], history: list[tuple[str, str]]) -> str:
        lowered = message.casefold()
        if "nome da minha empresa" in lowered or "como se chama minha empresa" in lowered:
            name = memories.get("company.name")
            return f"O nome da sua empresa é {name}." if name else "Ainda não sei o nome da sua empresa."
        if "company.name" in memories:
            return f"Entendido. Estou aqui para ajudar a {memories['company.name']}."
        return "Entendido. Como posso ajudar sua empresa?"


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model

    def reply(self, message: str, memories: dict[str, str], history: list[tuple[str, str]]) -> str:
        context = "\n".join(f"{k}: {v}" for k, v in memories.items()) or "Nenhuma memória relevante."
        messages = [{"role": "system", "content": f"Você é o Sócio IA. Responda em pt-BR, de forma curta. Memórias:\n{context}"}]
        messages += [{"role": role, "content": content} for role, content in history[-8:]]
        messages.append({"role": "user", "content": message})
        response = httpx.post(
            f"{self.base_url}/chat/completions", timeout=30,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "temperature": 0.2},
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

