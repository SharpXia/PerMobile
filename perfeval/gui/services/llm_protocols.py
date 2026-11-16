from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

from ..models.llm import LlmApiFormat, LlmProfile


class LlmProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class LlmHttpRequest:
    url: str
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True)
class LlmResponse:
    text: str
    usage: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


def validate_base_url(value: str) -> str:
    raw = str(value).strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise LlmProtocolError(f"LLM Base URL 无效：{exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise LlmProtocolError("LLM Base URL 仅支持 http 或 https")
    if not parsed.hostname:
        raise LlmProtocolError("LLM Base URL 缺少主机名")
    if parsed.username or parsed.password:
        raise LlmProtocolError("LLM Base URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise LlmProtocolError("LLM Base URL 不能包含 query 或 fragment")
    normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return normalized.rstrip("/")


def build_request(
    profile: LlmProfile,
    api_key: str,
    *,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int = 3000,
) -> LlmHttpRequest:
    base_url = validate_base_url(profile.base_url)
    key = str(api_key).strip()
    if not key:
        raise LlmProtocolError("API Key 不能为空")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    if profile.api_format is LlmApiFormat.OPENAI_CHAT:
        url = f"{base_url}/chat/completions"
        headers["Authorization"] = f"Bearer {key}"
        payload: dict[str, Any] = {
            "model": profile.model,
            "max_tokens": max(1, int(max_output_tokens)),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    elif profile.api_format is LlmApiFormat.ANTHROPIC_MESSAGES:
        url = f"{base_url}/messages"
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": profile.model,
            "max_tokens": max(1, int(max_output_tokens)),
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    elif profile.api_format is LlmApiFormat.GEMINI_GENERATE_CONTENT:
        model = quote(profile.model, safe="")
        url = f"{base_url}/models/{model}:generateContent"
        headers["x-goog-api-key"] = key
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"maxOutputTokens": max(1, int(max_output_tokens))},
        }
    else:  # pragma: no cover - enum exhaustiveness guard
        raise LlmProtocolError(f"不支持的 LLM API 格式：{profile.api_format}")

    return LlmHttpRequest(
        url=url,
        headers=headers,
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


def parse_response(api_format: LlmApiFormat, payload: Mapping[str, Any]) -> LlmResponse:
    if api_format is LlmApiFormat.OPENAI_CHAT:
        choices = payload.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        text = _text_content(content)
        usage = payload.get("usage")
    elif api_format is LlmApiFormat.ANTHROPIC_MESSAGES:
        text = _text_blocks(payload.get("content"))
        usage = payload.get("usage")
    elif api_format is LlmApiFormat.GEMINI_GENERATE_CONTENT:
        candidates = payload.get("candidates")
        content = candidates[0].get("content") if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping) else None
        parts = content.get("parts") if isinstance(content, Mapping) else None
        text = _text_blocks(parts)
        usage = payload.get("usageMetadata")
    else:  # pragma: no cover - enum exhaustiveness guard
        raise LlmProtocolError(f"不支持的 LLM API 格式：{api_format}")

    normalized = text.strip()
    if not normalized:
        raise LlmProtocolError("LLM 响应中没有可用文本")
    return LlmResponse(normalized, usage if isinstance(usage, Mapping) else {})


def safe_error_message(
    payload: bytes,
    *,
    status_code: int | None = None,
    secrets: Sequence[str] = (),
) -> str:
    prefix = f"HTTP {status_code}" if status_code else "LLM 请求失败"
    try:
        value = json.loads(payload[:32_768].decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeError):
        return prefix
    message: object = None
    if isinstance(value, Mapping):
        error = value.get("error")
        if isinstance(error, Mapping):
            message = error.get("message") or error.get("status") or error.get("type")
        elif isinstance(error, str):
            message = error
        message = message or value.get("message")
    text = redact_sensitive_text(message or "", secrets=secrets).strip().replace("\n", " ")[:500]
    return f"{prefix}：{text}" if text else prefix


def redact_sensitive_text(value: object, *, secrets: Sequence[str] = ()) -> str:
    text = str(value)
    for secret in sorted({str(item) for item in secrets if str(item)}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)\b(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b((?:x-api-key|x-goog-api-key|api[_ -]?key)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    return text


def _text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    return _text_blocks(value)


def _text_blocks(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
            parts.append(str(item["text"]))
    return "\n".join(parts)


__all__ = [
    "LlmHttpRequest",
    "LlmProtocolError",
    "LlmResponse",
    "build_request",
    "parse_response",
    "redact_sensitive_text",
    "safe_error_message",
    "validate_base_url",
]
