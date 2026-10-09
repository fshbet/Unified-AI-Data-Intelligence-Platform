"""AI provider abstraction. Two wire protocols cover every listed vendor:
  * OpenAI-compatible chat/completions (OpenAI, Azure OpenAI, Ollama, OpenRouter, Gemini's OpenAI endpoint, LM Studio, vLLM, custom)
  * Anthropic Messages API
Raw httpx keeps the dependency surface tiny and streaming uniform."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from backend.anonymize.guard import LeakError, assert_no_leak
from backend.core.config import settings

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    raw_assistant_message: dict | None = None  # provider-native message to append to history

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AIProviderError(RuntimeError):
    pass


class AIProvider:
    """Interface. `messages` use the OpenAI shape: {role, content} plus {role:'tool', tool_call_id, content}."""

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, temperature: float = 0.1, max_tokens: int = 4096, embedding_model: str | None = None, extra: dict | None = None):
        self.model, self.api_key, self.base_url = model, api_key, base_url
        self.temperature, self.max_tokens, self.embedding_model = temperature, max_tokens, embedding_model
        self.extra = extra or {}
        # Set per request by the orchestrator. Every outbound body goes through _wire(), which
        # runs the egress guard against it, so a provider added later inherits the check.
        self.vault: Any = None
        self.require_vault: bool = False

    def _wire(self, body: dict) -> dict:
        """Last thing before the wire. Guards the SERIALISED body, so it also covers the system
        prompt, tool-result messages and anything a future refactor forgets to anonymise."""
        if self.vault is not None:
            assert_no_leak(json.dumps(body, default=str), self.vault)
        elif self.require_vault:
            # Fail closed on the plumbing even though the policy layer fails open: a caller that
            # forgets to attach a vault must not silently send plaintext.
            raise LeakError("AI call attempted without an anonymisation vault")
        return body

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float | None = None, json_mode: bool = False) -> ChatResponse:
        raise NotImplementedError

    def stream(self, messages: list[dict], temperature: float | None = None) -> Iterator[str]:
        """Yield text deltas. Default: non-streaming fallback."""
        yield self.chat(messages, temperature=temperature).content

    def generate_embedding(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def structured_output(self, schema: dict, prompt: str, system: str | None = None) -> dict:
        sys_msg = (system or "You are a precise assistant.") + "\nRespond ONLY with a JSON object matching this JSON schema, no prose:\n" + json.dumps(schema)
        r = self.chat([{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}], json_mode=True, temperature=0)
        return parse_json(r.content)

    def test(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            r = self.chat([{"role": "user", "content": "Reply with the single word OK."}], temperature=0)
            return {"ok": True, "message": r.content.strip()[:80], "latency_ms": int((time.perf_counter() - t0) * 1000), "model": self.model}
        except LeakError:
            raise  # never downgraded to a status payload: this is a security event
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)[:500]}


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise AIProviderError(f"Model did not return valid JSON: {text[:200]}")


def _strip_think(text: str) -> str:
    """Qwen/DeepSeek style <think> blocks must never leak into answers."""
    import re

    return re.sub(r"<think>.*?</think>\s*", "", text or "", flags=re.S).strip()


class OpenAICompatibleProvider(AIProvider):
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.extra.get("azure"):
            h["api-key"] = self.api_key or ""
        return h

    def _url(self, path: str) -> str:
        base = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        if self.extra.get("azure"):
            ver = self.extra.get("api_version", "2024-10-21")
            return f"{base}/openai/deployments/{self.model}/{path}?api-version={ver}"
        return f"{base}/{path}"

    def chat(self, messages, tools=None, temperature=None, json_mode=False) -> ChatResponse:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature if temperature is None else temperature, "max_tokens": self.max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if ":11434" in (self.base_url or "") or "ollama" in (self.base_url or ""):
            body["options"] = {"num_ctx": int(self.extra.get("num_ctx", 16384))}
        t0 = time.perf_counter()
        with httpx.Client(timeout=settings.ai_request_timeout_seconds) as c:
            r = c.post(self._url("chat/completions"), headers=self._headers(), json=self._wire(body))
        if r.status_code >= 400:
            raise AIProviderError(f"{r.status_code}: {r.text[:500]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(tc.get("id") or f"call_{len(calls)}", fn["name"], args))
        usage = data.get("usage") or {}
        return ChatResponse(_strip_think(msg.get("content") or ""), calls, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), int((time.perf_counter() - t0) * 1000), raw_assistant_message=msg)

    def stream(self, messages, temperature=None) -> Iterator[str]:
        body = {"model": self.model, "messages": messages, "temperature": self.temperature if temperature is None else temperature, "max_tokens": self.max_tokens, "stream": True}
        in_think = False
        with httpx.Client(timeout=settings.ai_request_timeout_seconds) as c, c.stream("POST", self._url("chat/completions"), headers=self._headers(), json=self._wire(body)) as r:
            if r.status_code >= 400:
                r.read()
                raise AIProviderError(f"{r.status_code}: {r.text[:500]}")
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue
                if "<think>" in delta:
                    in_think = True
                if in_think:
                    if "</think>" in delta:
                        in_think = False
                        delta = delta.split("</think>", 1)[1]
                    else:
                        continue
                if delta:
                    yield delta

    def generate_embedding(self, texts: list[str]) -> list[list[float]]:
        if not self.embedding_model:
            raise AIProviderError("No embedding model configured")
        with httpx.Client(timeout=120) as c:
            r = c.post(self._url("embeddings"), headers=self._headers(), json=self._wire({"model": self.embedding_model, "input": texts}))
        if r.status_code >= 400:
            raise AIProviderError(f"{r.status_code}: {r.text[:300]}")
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


class AnthropicProvider(AIProvider):
    def _headers(self) -> dict:
        return {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def _url(self) -> str:
        return (self.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"

    @staticmethod
    def _convert(messages: list[dict]) -> tuple[str | None, list[dict]]:
        system, out = None, []
        for m in messages:
            if m["role"] == "system":
                system = (system + "\n" if system else "") + m["content"]
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                content = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                for tc in m["tool_calls"]:
                    args = tc["function"]["arguments"]
                    content.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": json.loads(args) if isinstance(args, str) else args})
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return system, out

    def chat(self, messages, tools=None, temperature=None, json_mode=False) -> ChatResponse:
        system, msgs = self._convert(messages)
        body: dict[str, Any] = {"model": self.model, "messages": msgs, "max_tokens": self.max_tokens, "temperature": self.temperature if temperature is None else temperature}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("parameters", {"type": "object", "properties": {}})} for t in tools]
        t0 = time.perf_counter()
        with httpx.Client(timeout=settings.ai_request_timeout_seconds) as c:
            r = c.post(self._url(), headers=self._headers(), json=self._wire(body))
        if r.status_code >= 400:
            raise AIProviderError(f"{r.status_code}: {r.text[:500]}")
        data = r.json()
        text, calls = "", []
        for block in data.get("content", []):
            if block["type"] == "text":
                text += block["text"]
            elif block["type"] == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], block.get("input") or {}))
        usage = data.get("usage") or {}
        raw = {"role": "assistant", "content": text or None, "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in calls] or None}
        return ChatResponse(text, calls, usage.get("input_tokens", 0), usage.get("output_tokens", 0), int((time.perf_counter() - t0) * 1000), raw_assistant_message=raw)

    def stream(self, messages, temperature=None) -> Iterator[str]:
        system, msgs = self._convert(messages)
        body: dict[str, Any] = {"model": self.model, "messages": msgs, "max_tokens": self.max_tokens, "stream": True, "temperature": self.temperature if temperature is None else temperature}
        if system:
            body["system"] = system
        with httpx.Client(timeout=settings.ai_request_timeout_seconds) as c, c.stream("POST", self._url(), headers=self._headers(), json=self._wire(body)) as r:
            if r.status_code >= 400:
                r.read()
                raise AIProviderError(f"{r.status_code}: {r.text[:500]}")
            for line in r.iter_lines():
                if line.startswith("data:"):
                    try:
                        ev = json.loads(line[5:])
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                        yield ev["delta"]["text"]

    def generate_embedding(self, texts: list[str]) -> list[list[float]]:
        raise AIProviderError("Anthropic does not provide embeddings; configure an OpenAI-compatible embedding provider")


PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "cls": OpenAICompatibleProvider},
    "azure_openai": {"base_url": "https://<resource>.openai.azure.com", "cls": OpenAICompatibleProvider, "extra": {"azure": True}},
    "anthropic": {"base_url": "https://api.anthropic.com", "cls": AnthropicProvider},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "cls": OpenAICompatibleProvider},
    "ollama": {"base_url": "http://localhost:11434/v1", "cls": OpenAICompatibleProvider},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "cls": OpenAICompatibleProvider},
    "custom": {"base_url": "http://localhost:8000/v1", "cls": OpenAICompatibleProvider},
}


def make_provider(provider: str, model: str, api_key: str | None, base_url: str | None, temperature: float = 0.1, max_tokens: int = 4096, embedding_model: str | None = None, extra: dict | None = None) -> AIProvider:
    spec = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["custom"])
    cls = spec["cls"]
    return cls(model=model, api_key=api_key, base_url=base_url or spec["base_url"], temperature=temperature, max_tokens=max_tokens, embedding_model=embedding_model, extra={**spec.get("extra", {}), **(extra or {})})
