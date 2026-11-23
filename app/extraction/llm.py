"""LLM-based extraction. The PDF itself (text or scanned image) is sent to a multimodal model which returns
structured JSON matching INVOICE_SCHEMA. Supports Anthropic Claude, Google Gemini and OpenAI."""
import base64
import json
import os
import re

import httpx

from .. import config
from .schema import EXTRACTION_PROMPT, INVOICE_SCHEMA


class LLMError(RuntimeError):
    pass


def _b64(pdf_bytes: bytes) -> str:
    return base64.standard_b64encode(pdf_bytes).decode()


def _json_from_text(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("Model did not return JSON")
    return json.loads(m.group(0))


def extract_anthropic(pdf_bytes: bytes) -> tuple[dict, dict]:
    base = os.getenv("ANTHROPIC_API_BASE", "https://api.anthropic.com")
    body = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "temperature": 0,
        "tools": [{"name": "record_invoice", "description": "Record the structured invoice data.",
                   "input_schema": INVOICE_SCHEMA}],
        "tool_choice": {"type": "tool", "name": "record_invoice"},
        "messages": [{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(pdf_bytes)}},
            {"type": "text", "text": EXTRACTION_PROMPT}]}],
    }
    headers = {"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    r = httpx.post(f"{base}/v1/messages", json=body, headers=headers, timeout=config.LLM_TIMEOUT_S)
    if r.status_code != 200:
        raise LLMError(f"Anthropic API {r.status_code}: {r.text[:300]}")
    data = r.json()
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            usage = data.get("usage", {})
            return block["input"], {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}
    raise LLMError("Claude response contained no structured output")


def _schema_prompt() -> str:
    return EXTRACTION_PROMPT + "\n\nReturn ONLY a JSON object (no prose) that conforms to this JSON schema:\n" + \
        json.dumps(INVOICE_SCHEMA)


def extract_gemini(pdf_bytes: bytes) -> tuple[dict, dict]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "application/pdf", "data": _b64(pdf_bytes)}},
            {"text": _schema_prompt()}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    r = httpx.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=config.LLM_TIMEOUT_S)
    if r.status_code != 200:
        raise LLMError(f"Gemini API {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError):
        raise LLMError(f"Unexpected Gemini response: {str(data)[:300]}")
    usage = data.get("usageMetadata", {})
    return _json_from_text(text), {"input_tokens": usage.get("promptTokenCount"), "output_tokens": usage.get("candidatesTokenCount")}


def extract_openai(pdf_bytes: bytes) -> tuple[dict, dict]:
    body = {
        "model": config.OPENAI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"filename": "invoice.pdf", "file_data": "data:application/pdf;base64," + _b64(pdf_bytes)}},
            {"type": "text", "text": _schema_prompt()}]}],
    }
    r = httpx.post("https://api.openai.com/v1/chat/completions", json=body,
                   headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"}, timeout=config.LLM_TIMEOUT_S)
    if r.status_code != 200:
        raise LLMError(f"OpenAI API {r.status_code}: {r.text[:300]}")
    data = r.json()
    usage = data.get("usage", {})
    return _json_from_text(data["choices"][0]["message"]["content"]), {
        "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}


PROVIDERS = {"anthropic": extract_anthropic, "gemini": extract_gemini, "openai": extract_openai}
