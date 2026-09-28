"""Opt-in, stateless Responses adapter. Provider credentials never enter the store."""

import json
import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 512 * 1024


class ProviderError(Exception):
    """A public, credential-free failure message."""

    def __init__(self, message: str, *, usage: dict | None = None):
        super().__init__(message)
        self.usage = {} if usage is None else dict(usage)


class AgentLimit(Exception):
    """An explicit local or provider generation bound was reached."""

    def __init__(self, message: str, *, usage: dict | None = None):
        super().__init__(message)
        self.usage = {} if usage is None else dict(usage)


@dataclass(frozen=True)
class AgentConfig:
    provider: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        return cls(
            provider=os.environ.get("ARGOS_STUDIO_AGENT_PROVIDER", "").strip(),
            model=os.environ.get("ARGOS_STUDIO_AGENT_MODEL", "").strip(),
            api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        )

    @property
    def reason(self) -> str | None:
        if not self.provider:
            return (
                "Agent désactivé : configurer un fournisseur, un modèle et un accès côté serveur."
            )
        if self.provider != "openai":
            return "Fournisseur non pris en charge. L’adaptateur disponible est openai."
        if not self.model or len(self.model) > 120:
            return "Configurer ARGOS_STUDIO_AGENT_MODEL avec un identifiant de modèle valide."
        if not self.api_key:
            return "Configurer OPENAI_API_KEY côté serveur."
        if not self.api_key.isascii() or any(character.isspace() for character in self.api_key):
            return "Configurer OPENAI_API_KEY avec une clé API valide côté serveur."
        return None


@dataclass
class Reply:
    # Transient continuation includes opaque reasoning items. Never persist them
    # or expose them as an explanation; only tool I/O and final text are recorded.
    continuation: list[dict]
    calls: list[dict]
    answer: str
    usage: dict


class Provider(Protocol):
    provider: str
    model: str
    sends_data_off_machine: bool

    async def respond(
        self,
        *,
        instructions: str,
        messages: list[dict],
        schemas: list[dict],
        max_output_tokens: int,
    ) -> Reply: ...

    async def close(self) -> None: ...


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _invalid_constant(_value):
    raise ValueError("Nonfinite JSON")


def _usage(payload: dict) -> dict:
    usage = payload.get("usage")
    if usage is None:
        return {}
    if not isinstance(usage, dict):
        raise ValueError("Invalid usage")
    return {
        key: value
        for key in ("input_tokens", "output_tokens")
        if type(value := usage.get(key)) is int and value >= 0
    }


class OpenAIProvider:
    provider = "openai"
    sends_data_off_machine = True

    def __init__(self, config: AgentConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        if config.reason:
            raise ValueError(config.reason)
        self.model = config.model
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def respond(self, *, instructions, messages, schemas, max_output_tokens) -> Reply:
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ProviderError("La limite de génération doit être un entier positif.")
        try:
            body = {
                "model": self.model,
                "instructions": instructions,
                "input": messages,
                "tools": [{**schema, "type": "function", "strict": True} for schema in schemas],
                "parallel_tool_calls": False,
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "max_output_tokens": max_output_tokens,
            }
            content = encode(body).encode()
        except (TypeError, ValueError, RecursionError):
            raise ProviderError("Le contexte transmis au fournisseur est invalide.") from None
        if len(content) > MAX_REQUEST_BYTES:
            raise AgentLimit("Contexte maximal atteint (256 Kio).")
        try:
            async with self._client.stream(
                "POST",
                "https://api.openai.com/v1/responses",
                content=content,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    # Do not persist bodies, headers, URLs or exception strings:
                    # remote errors can echo request data or credentials.
                    raise ProviderError(
                        f"Le fournisseur a refusé l’appel (HTTP {response.status_code})."
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise AgentLimit("Réponse du fournisseur trop volumineuse (512 Kio).")
        except httpx.HTTPError:
            raise ProviderError(
                "Le fournisseur est inaccessible ou le délai de l’appel est dépassé."
            ) from None
        usage = {}
        try:
            payload = json.loads(data, parse_constant=_invalid_constant)
            if isinstance(payload, dict):
                usage = _usage(payload)
            # JSON escapes may decode to lone surrogates and exponent notation
            # may overflow to infinity. Reject both before any call is executed
            # or an unpersistable answer reaches the session store.
            encode(payload).encode()
            return self._reply(payload)
        except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
            raise ProviderError(
                "Réponse du fournisseur invalide ou non prise en charge.", usage=usage
            ) from None

    @staticmethod
    def _reply(payload: dict) -> Reply:
        if not isinstance(payload, dict):
            raise ValueError("Invalid response")
        usage = _usage(payload)
        if payload.get("status") == "incomplete":
            # Incomplete generations can still consume input and reasoning tokens.
            # Preserve reported counts while never executing their partial calls.
            raise AgentLimit("Le fournisseur a interrompu la génération avant sa fin.", usage=usage)
        if payload.get("status") != "completed":
            raise ProviderError("Le fournisseur n’a pas terminé la génération.", usage=usage)
        output = payload["output"]
        if not isinstance(output, list) or len(output) > 32:
            raise ValueError("Invalid output")
        calls, text = [], []
        for item in output:
            if not isinstance(item, dict):
                raise ValueError("Invalid output item")
            if item["type"] == "function_call":
                call = {key: item[key] for key in ("call_id", "name", "arguments")}
                if not all(isinstance(value, str) and value for value in call.values()):
                    raise ValueError("Invalid call")
                if item.get("status", "completed") != "completed":
                    raise ValueError("Incomplete call")
                calls.append(call)
            elif item["type"] == "message":
                if (
                    item.get("role") != "assistant"
                    or item.get("status", "completed") != "completed"
                    or not isinstance(item["content"], list)
                ):
                    raise ValueError("Invalid role")
                for part in item["content"]:
                    if not isinstance(part, dict):
                        raise ValueError("Invalid message part")
                    if part["type"] == "output_text":
                        value = part["text"]
                    elif part["type"] == "refusal":
                        value = part["refusal"]
                    else:
                        raise ValueError("Unsupported message content")
                    if not isinstance(value, str):
                        raise ValueError("Invalid message text")
                    text.append(value)
            elif item["type"] == "reasoning":
                if item.get("encrypted_content") is not None and not isinstance(
                    item["encrypted_content"], str
                ):
                    raise ValueError("Invalid encrypted reasoning")
            else:
                raise ValueError("Unexpected provider tool")
        if len(calls) > 1:
            raise ValueError("Parallel calls are not supported")
        return Reply(output, calls, "\n".join(text), usage)

    async def close(self):
        await self._client.aclose()
