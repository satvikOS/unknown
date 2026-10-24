"""Streaming tool-use calls to Anthropic for the analyst, on the agent layer's Anthropic adapter.

Admission is the adapter's: provider enabled, key present, region recorded, an OPEN reservation for this exact
provider and model, and an allow decision of the data-use gate bound to this request's hash (``_client_for_send``).
Errors map to the same taxonomy (``map_exception``) and usage is normalised the same way (``normalize_usage``), so the
ledger settles exactly what the response reports.

Request shape (Claude API skill docs, Python: streaming, tool use, adaptive thinking, prompt caching; SDK 1.12.1
``Messages.stream`` signature checked):
  client.messages.stream(model, max_tokens, system=[{text, cache_control: ephemeral}], tools=[...], messages=[...],
                         output_config={"effort": <explicit>}, inference_geo=<explicit>)
  * ``thinking`` is omitted: on the default model adaptive thinking is what an omitted value gives, and the other forms
    are a 400. Thinking blocks are passed back unchanged within a job's tool loop.
  * prompt caching: one breakpoint on the system block caches tools + system (render order tools -> system ->
    messages); a second, moving breakpoint on the last block of the newest user message caches the growing tool loop.
    Writes use the 5-minute TTL, which the worst case prices at the cache-write multiplier.
  * never sent: ``temperature``/``top_p``/``top_k`` (400), ``tool_choice`` (auto is the default), an assistant prefill.
  * server-side refusal fallback is not used by the analyst (no ``fallbacks``), so no other model can bill a request.
Token counting: ``client.messages.count_tokens(model, system, messages, tools)``.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ...domain.canonical import content_hash, sha256_hex
from ..providers.anthropic import AnthropicAdapter, _attr, map_exception, normalize_usage
from ..providers.base import ErrorClass, ProviderError, SendRefused
from ..tariffs import Usage

CACHE_TTL = "5m"
FORBIDDEN = frozenset({"thinking", "temperature", "top_p", "top_k", "tool_choice", "fallbacks", "betas"})
MAX_REQUEST_BYTES = 32_000_000


@dataclass(frozen=True)
class AnalystRequest:
    """One model turn. ``messages`` hold plain input dicts (never SDK objects), so the hash is stable."""
    job_id: str
    role: str
    model_id: str
    system: str
    tools: tuple[dict, ...]
    messages: tuple[dict, ...]
    max_output_tokens: int
    effort: str | None

    def _body(self) -> dict:
        return {"model_id": self.model_id, "system": self.system, "tools": list(self.tools), "messages": list(self.messages),
                "max_output_tokens": self.max_output_tokens, "effort": self.effort}

    def request_hash(self) -> str:
        return content_hash(self._body())

    def payload_bytes(self) -> int:
        return len(json.dumps(self._body(), ensure_ascii=False, separators=(",", ":"), default=str).encode())


@dataclass
class TurnResult:
    stop_reason: str | None
    content: list[dict]
    usages: tuple[Usage, ...]
    usage_complete: bool
    returned_model_id: str | None
    request_id: str | None
    raw_sha256: str
    inference_geo: str | None = None
    cancelled: bool = False
    streamed_text: bool = False
    usage_raw: dict = field(default_factory=dict)


def input_block(block: Any) -> dict:
    """An SDK content block as the input dict that may be sent back in the next request."""
    kind = _attr(block, "type")
    if kind == "text":
        return {"type": "text", "text": _attr(block, "text") or ""}
    if kind == "thinking":
        return {"type": "thinking", "thinking": _attr(block, "thinking") or "", "signature": _attr(block, "signature") or ""}
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": _attr(block, "data")}
    if kind == "tool_use":
        return {"type": "tool_use", "id": _attr(block, "id"), "name": _attr(block, "name"), "input": _attr(block, "input") or {}}
    if isinstance(block, dict):
        return dict(block)
    return block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else {"type": str(kind)}


def with_cache_breakpoints(system: str, messages: tuple[dict, ...]) -> tuple[list[dict], list[dict]]:
    sys_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    msgs = [dict(m) for m in messages]
    if msgs and msgs[-1]["role"] == "user":
        last = msgs[-1]
        content = last["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else copy.deepcopy(list(content))
        if blocks:
            blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        msgs[-1] = {"role": "user", "content": blocks}
    return sys_blocks, msgs


class StreamingClaude:
    def __init__(self, adapter: AnthropicAdapter):
        self.adapter = adapter
        self.provider_id = adapter.provider_id

    # admission facts are the adapter's
    def admission_problem(self) -> str | None:
        return self.adapter.admission_problem()

    def requested_region(self) -> str | None:
        return self.adapter.requested_region()

    def effort_for(self, role: str) -> str | None:
        return self.adapter.effort_for(role)

    def _client(self, req: AnalystRequest, reservation, gate, feature: str):
        if req.payload_bytes() > MAX_REQUEST_BYTES:
            raise SendRefused(self.provider_id, "request_too_large")
        return self.adapter._client_for_send(reservation=reservation, gate=gate, request=req, feature=feature)

    def params(self, req: AnalystRequest) -> dict:
        if not req.effort:
            raise SendRefused(self.provider_id, "effort_not_configured_for_role")
        system, messages = with_cache_breakpoints(req.system, req.messages)
        params = {"model": req.model_id, "max_tokens": req.max_output_tokens, "system": system, "tools": list(req.tools),
                  "messages": messages, "output_config": {"effort": req.effort}, "inference_geo": self.requested_region()}
        assert not FORBIDDEN & set(params)
        return params

    def count_tokens(self, req: AnalystRequest, *, admission, gate) -> int:
        client = self._client(req, admission, gate, "count_tokens")
        system, messages = with_cache_breakpoints(req.system, req.messages)
        try:
            resp = client.messages.count_tokens(model=req.model_id, system=system, messages=messages, tools=list(req.tools))
        except Exception as exc:
            err = map_exception(exc, req.model_id)
            if err is None:
                raise
            raise err from None
        tokens = _attr(resp, "input_tokens")
        if type(tokens) is not int or tokens < 0:
            raise ProviderError(self.provider_id, ErrorClass.UNKNOWN, "count_tokens returned no input_tokens", model_id=req.model_id)
        return tokens

    def stream(self, req: AnalystRequest, *, reservation, gate, on_text: Callable[[str], None],
               on_block: Callable[[str, str | None], None], should_stop: Callable[[], bool]) -> TurnResult:
        """Send one turn and stream it. ``should_stop`` is polled between events; when it turns true the stream is
        closed and the result is ``cancelled`` (the request was sent; its usage is unknown)."""
        client = self._client(req, reservation, gate, "generate")
        params = self.params(req)
        region = params["inference_geo"]
        streamed = False
        try:
            with client.messages.stream(**params) as stream:
                for event in stream:
                    kind = _attr(event, "type")
                    if kind == "text":
                        text = _attr(event, "text") or ""
                        if text:
                            streamed = True
                            on_text(text)
                    elif kind == "content_block_start":
                        block = _attr(event, "content_block")
                        on_block(_attr(block, "type") or "", _attr(block, "name"))
                    if should_stop():
                        return TurnResult(None, [], (), False, None, _request_id(stream), sha256_hex(""), cancelled=True,
                                          streamed_text=streamed)
                final = stream.get_final_message()
                request_id = _request_id(stream) or _attr(final, "_request_id")
        except Exception as exc:
            err = map_exception(exc, req.model_id)
            if err is None:
                raise
            err.streamed_text = streamed  # type: ignore[attr-defined]
            raise err from None
        raw = final.to_json() if hasattr(final, "to_json") else json.dumps(final, default=str, sort_keys=True)
        usages, complete, _ = normalize_usage(final, region)
        usage = _attr(final, "usage")
        usage_raw = {k: _attr(usage, k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens")} if usage is not None else {}
        return TurnResult(_attr(final, "stop_reason"), [input_block(b) for b in (_attr(final, "content") or [])], usages,
                          complete, _attr(final, "model"), request_id, sha256_hex(raw),
                          inference_geo=_attr(usage, "inference_geo") if usage is not None else None,
                          streamed_text=streamed, usage_raw=usage_raw)


def _request_id(stream: Any) -> str | None:
    try:
        rid = getattr(stream, "request_id", None)
    except Exception:  # the SDK reads it from the HTTP response headers; a fake stream may have none
        return None
    return rid if isinstance(rid, str) else None
