from __future__ import annotations

import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .config import LLMSettings
from .storage import ToolError, read_json, write_json


class NoRedirect(HTTPRedirectHandler):
    # Do not forward provider credentials to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LLMFormatError(ToolError):
    """A completed provider call that returned unusable structured content."""


class LLMBudgetError(ToolError):
    """A run has exhausted its cumulative request/token budget."""


class LLMClient:
    """One configurable chat-completions provider, without LiteLLM or telemetry."""
    def __init__(self, settings: LLMSettings, run_dir: Path):
        self.settings = settings
        self.run_dir = run_dir
        records = [read_json(path) for path in sorted((run_dir / "llm_calls").glob("*.json"))]
        self.calls = max((item["call"] for item in records), default=0)
        self.total_tokens = sum(item.get("tokens_charged", 0) for item in records)
        self.opener = build_opener(NoRedirect())

    def redact(self, text: str) -> str:
        return text.replace(self.settings.api_key, "[REDACTED]") if self.settings.api_key else text

    def usage(self) -> dict:
        return {"model": self.settings.model, "calls": self.calls, "charged_total_tokens": self.total_tokens,
                "max_calls": self.settings.max_calls, "max_total_tokens": self.settings.max_total_tokens}

    def ask(self, messages: list[dict], purpose: str) -> dict:
        body = {"model": self.settings.model, "messages": messages, "stream": False,
                "temperature": self.settings.temperature, "max_tokens": self.settings.max_tokens,
                **self.settings.extra_body}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(self.settings.retries + 1):
            # Failed HTTP calls count too; token budget includes a conservative next-response estimate.
            estimated = max(1, len(payload) // 3) + self.settings.max_tokens
            if self.calls >= self.settings.max_calls or self.total_tokens + estimated > self.settings.max_total_tokens:
                raise LLMBudgetError("达到 LLM 调用/Token 预算，已停止；已生成的记录会保留")
            self.calls += 1
            call_id = self.calls
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if self.settings.api_key:
                headers["Authorization"] = "Bearer " + self.settings.api_key
            started = time.monotonic()
            log = {"call": call_id, "purpose": purpose, "model": self.settings.model,
                   "messages": json.loads(self.redact(json.dumps(messages, ensure_ascii=False)))}
            try:
                req = Request(self.settings.endpoint, data=payload, headers=headers, method="POST")
                with self.opener.open(req, timeout=self.settings.timeout) as response:
                    raw = json.loads(response.read(8_000_000).decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("接口响应顶层不是对象")
                usage = raw.get("usage", {})
                if not isinstance(usage, dict):
                    usage = {}
                tokens = usage.get("total_tokens") or estimated
                self.total_tokens += max(0, int(tokens))
                log["tokens_charged"] = max(0, int(tokens))
                content = raw["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError("接口未返回文本 content")
                finish_reason = raw["choices"][0].get("finish_reason")
                log.update(response=self.redact(content), usage=usage, tokens_charged=max(0, int(tokens)),
                           finish_reason=finish_reason,
                           seconds=round(time.monotonic() - started, 2))
                write_json(self.run_dir / "llm_calls" / f"{call_id:04d}.json", log)
                if finish_reason == "length":
                    raise LLMFormatError("LLM 输出达到 Token 上限而被截断；请增大 LLM_MAX_TOKENS 或关闭思考模式")
                return parse_object(content)
            except HTTPError as exc:
                # Provider errors can echo arbitrary request content, so persist only sanitized text.
                detail = self.redact(exc.read(4096).decode("utf-8", errors="replace"))
                log.update(error=f"HTTP {exc.code}: {detail}", tokens_charged=estimated)
                write_json(self.run_dir / "llm_calls" / f"{call_id:04d}.json", log)
                self.total_tokens += estimated
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.settings.retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise ToolError(f"LLM 请求失败，HTTP {exc.code}；详情见调用记录") from exc
            except (URLError, TimeoutError, OSError) as exc:
                log.update(error=self.redact(str(exc)), tokens_charged=estimated)
                write_json(self.run_dir / "llm_calls" / f"{call_id:04d}.json", log)
                self.total_tokens += estimated
                if attempt < self.settings.retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise ToolError("LLM 网络请求失败；详情见调用记录") from exc
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                if "tokens_charged" not in log:
                    log["tokens_charged"] = estimated
                    self.total_tokens += estimated
                log.update(error=self.redact(str(exc)))
                write_json(self.run_dir / "llm_calls" / f"{call_id:04d}.json", log)
                raise LLMFormatError("LLM 响应格式不符合要求；详情见调用记录") from exc
        raise ToolError("LLM 请求未完成")


def parse_object(text: str) -> dict:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise LLMFormatError("LLM 必须返回完整 JSON 对象；不会执行响应中的说明文字") from exc
    if not isinstance(value, dict):
        raise LLMFormatError("LLM 响应顶层必须是 JSON 对象")
    return value
