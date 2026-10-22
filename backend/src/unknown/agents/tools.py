"""Default-deny tool gateway (CAP-AGT-03, CAP-MODEL-03; OWASP LLM06 "complete mediation").

The model never decides what it may call. Every tool request goes through
``ToolGateway.call``, which checks, in order:
  1. the job's tool-call budget (``max_tool_calls``; every request counts, denied or not);
  2. the tool is in the job envelope's ``allowed_tools`` (the only source of authority);
  3. the tool is registered;
  4. the arguments validate against the tool's input schema (always, even with strict mode);
  5. the content the tool returns may enter a cloud model context (dataset rights via the gate).

The registry refuses any tool that is not read-only and any name on the forbidden list
(order, policy, secret, job-spawning or stop-latch tools), so no LLM role can hold a
write-capable tool (privilege surface = 0). Tool results are returned as untrusted data
blocks; their text cannot change permissions, budgets or routing.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .envelope import JobEnvelope
from .evidence import render_data_block
from .schema import Validator

FORBIDDEN_TOOL_NAMES = frozenset({
    "submit_order", "place_order", "modify_order", "cancel_order", "cancel_all_orders", "submit_intent",
    "request_cancel", "set_policy", "publish_policy", "activate_policy", "authorize_capital", "read_secret",
    "get_credentials", "clear_stop", "start", "start_session", "create_job", "spawn_agent", "agent",
    "run_shell", "shell", "fetch_url", "http_request", "write_file", "submit_research_proposal",
})
_WRITE_HINT = re.compile(r"(order|trade|policy|secret|credential|spawn|delete|write|transfer|withdraw)")


class ToolRegistryError(ValueError):
    pass


class ToolBudgetExceeded(RuntimeError):
    """The job's max_tool_calls is exhausted; the job ends ``exceeded_tool_calls``."""


class Decision(enum.Enum):
    EXECUTED = "executed"
    DENIED = "denied"


@dataclass(frozen=True)
class ToolOutput:
    text: str
    dataset_id: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], ToolOutput]
    read_only: bool = True


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    decision: Decision
    rule_id: str
    content: str  # a rendered untrusted data block, or a refusal message


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Validator]] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.read_only:
            raise ToolRegistryError(f"{spec.name}: only read-only tools may be registered for model roles (CAP-AGT-03)")
        if spec.name in FORBIDDEN_TOOL_NAMES or _WRITE_HINT.search(spec.name):
            raise ToolRegistryError(f"{spec.name}: name denotes a write-capable or spawning action; refused")
        if spec.name in self._tools:
            raise ToolRegistryError(f"duplicate tool {spec.name}")
        if spec.input_schema.get("type") != "object":
            raise ToolRegistryError(f"{spec.name}: the parameters root must be an object")
        self._tools[spec.name] = (spec, Validator(spec.input_schema))

    def get(self, name: str) -> tuple[ToolSpec, Validator] | None:
        return self._tools.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def privilege_surface(self) -> dict:
        return {"tools": len(self._tools), "write_capable": sum(1 for s, _ in self._tools.values() if not s.read_only)}


@dataclass
class ToolGateway:
    registry: ToolRegistry
    envelope: JobEnvelope
    rights_ok: Callable[[str], bool]          # dataset_id -> may this content enter the model context?
    attempts: int = 0
    executed: int = 0
    log: list[dict] = field(default_factory=list)

    def _deny(self, call_id: str, name: str, rule: str, message: str) -> ToolResult:
        self.log.append({"call_id": call_id, "tool": name, "decision": "denied", "rule_id": rule})
        return ToolResult(call_id, name, Decision.DENIED, rule, f"tool call refused ({rule}): {message}")

    def call(self, name: str, args: Any, *, call_id: str) -> ToolResult:
        if self.attempts >= self.envelope.max_tool_calls:
            self.log.append({"call_id": call_id, "tool": name, "decision": "denied", "rule_id": "max_tool_calls"})
            raise ToolBudgetExceeded(f"job {self.envelope.job_id} exhausted max_tool_calls={self.envelope.max_tool_calls}")
        self.attempts += 1
        if not isinstance(name, str) or not self.envelope.allows_tool(name):
            return self._deny(call_id, str(name), "not_allowlisted", "the job envelope does not allow this tool")
        entry = self.registry.get(name)
        if entry is None:
            return self._deny(call_id, name, "unknown_tool", "no such tool is registered")
        spec, validator = entry
        errors = validator.errors(args)
        if errors:
            return self._deny(call_id, name, "invalid_arguments", "; ".join(errors[:5]))
        out = spec.handler(args)
        self.executed += 1
        if not self.rights_ok(out.dataset_id):
            self.log.append({"call_id": call_id, "tool": name, "decision": "executed", "rule_id": "rights_redacted"})
            return ToolResult(call_id, name, Decision.EXECUTED, "rights_redacted",
                              "tool result withheld: its source dataset does not permit cloud-model processing")
        block = render_data_block(kind="tool_result", block_id=call_id, dataset_id=out.dataset_id, body=out.text)
        self.log.append({"call_id": call_id, "tool": name, "decision": "executed", "rule_id": "allowed",
                         "injection_flags": list(block.flags)})
        return ToolResult(call_id, name, Decision.EXECUTED, "allowed", block.text)
