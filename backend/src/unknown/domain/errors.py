"""Error taxonomy.

``Rejected`` is a deterministic domain refusal: it is stored as the command's
result (a decision record) and returned again on an identical retry.
Everything else is a fault: the transaction rolls back and nothing is stored.
"""
from __future__ import annotations

from typing import Any


class Rejected(Exception):
    def __init__(self, code: str, message: str = "", **details: Any):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = details

    def to_json(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class CommandConflict(Exception):
    """A command id was reused with a different payload (ADR-0005 #4)."""


class InvariantViolation(AssertionError):
    """A projection invariant failed; the transaction must not commit."""


class LeaseLost(RuntimeError):
    """This process no longer holds the writer lease (ADR-0011)."""


class Unauthorized(Rejected):
    def __init__(self, role: str, kind: str):
        super().__init__("UNAUTHORIZED", f"role {role} may not issue {kind}", role=role, kind=kind)
