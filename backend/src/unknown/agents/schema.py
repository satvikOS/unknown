"""Local JSON-Schema subset validator and per-provider compatibility checker (CAP-MODEL-02).

Every model output is validated locally against the full canonical schema, whatever the
provider claims to enforce. The validator supports exactly the keywords below and
refuses (``SchemaError``) any other keyword at compile time, so a constraint is never
silently ignored:

  type (incl. a list of types), properties, required, additionalProperties (bool),
  enum, const, items (single schema), minItems, maxItems, uniqueItems, minLength,
  maxLength, minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf,
  minProperties, maxProperties, anyOf, allOf, not, if/then/else, $ref (local "#/..."),
  $defs/definitions, and the annotations title, description, $schema, $id.

Numbers: ``integer`` accepts only Python ``int`` (never ``bool``); ``number`` accepts
``int`` or a finite ``Decimal`` (parse model output with ``parse_float=Decimal``).
Floats are rejected (ADR-0004). Schema bounds must be integers.

``compile_for(provider, schema)`` classifies every keyword per provider rule recorded in
CAP-MODEL-02 as enforced / best_effort / not_enforced / unverified / rejected, strips
keywords the provider would reject with a 400 where they can be moved to local
validation (Anthropic), and raises ``SchemaRejected`` (no network call) when the schema
cannot be sent at all.
"""
from __future__ import annotations

import copy
import enum
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from ..domain.canonical import canonical_json, sha256_hex

TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array", "null"})
ANNOTATIONS = frozenset({"title", "description", "$schema", "$id", "$comment"})
SUPPORTED = frozenset({
    "type", "properties", "required", "additionalProperties", "enum", "const", "items", "minItems", "maxItems",
    "uniqueItems", "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minProperties", "maxProperties", "anyOf", "allOf", "not", "if", "then", "else", "$ref",
    "$defs", "definitions",
}) | ANNOTATIONS
MAX_REF_DEPTH = 64


class SchemaError(ValueError):
    """The schema itself is invalid or uses a keyword the local validator cannot enforce."""


class SchemaRejected(SchemaError):
    def __init__(self, provider: str, findings: list["Finding"]):
        self.provider, self.findings = provider, findings
        bad = [f for f in findings if f.enforcement is Enforcement.REJECTED]
        super().__init__(f"schema rejected for {provider}: " + "; ".join(f"{f.path} {f.keyword}: {f.reason}" for f in bad))


def schema_hash(schema: dict) -> str:
    return sha256_hex(canonical_json(schema))


def strict_equal(a: Any, b: Any) -> bool:
    """JSON equality that does not confuse True with 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, Decimal)) and isinstance(b, (int, Decimal)):
        return Decimal(a) == Decimal(b)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _is_type(value: Any, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return type(value) is int
    if t == "number":
        return (type(value) is int) or (isinstance(value, Decimal) and value.is_finite())
    if t == "boolean":
        return type(value) is bool
    if t == "null":
        return value is None
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    return False


# ============================================================================ compile-time check
def _walk(schema: Any, path: str = "#") -> Iterable[tuple[str, dict]]:
    """Yield (path, subschema) for every subschema reachable without resolving $ref."""
    if not isinstance(schema, dict):
        return
    yield path, schema
    for k in ("properties", "$defs", "definitions"):
        sub = schema.get(k)
        if isinstance(sub, dict):
            for name, s in sub.items():
                yield from _walk(s, f"{path}/{k}/{name}")
    for k in ("items", "not", "if", "then", "else", "additionalProperties"):
        s = schema.get(k)
        if isinstance(s, dict):
            yield from _walk(s, f"{path}/{k}")
        elif k == "items" and isinstance(s, list):
            for i, sub in enumerate(s):
                yield from _walk(sub, f"{path}/{k}/{i}")
    for k in ("anyOf", "allOf", "oneOf"):
        s = schema.get(k)
        if isinstance(s, list):
            for i, sub in enumerate(s):
                yield from _walk(sub, f"{path}/{k}/{i}")


def check_schema(schema: Any) -> None:
    """Raise ``SchemaError`` unless every keyword is one the local validator enforces."""
    if not isinstance(schema, dict):
        raise SchemaError("schema must be an object")
    try:
        canonical_json(schema)
    except TypeError as exc:
        raise SchemaError(f"schema is not canonical JSON: {exc}") from None
    for path, s in _walk(schema):
        unknown = set(s) - SUPPORTED
        if unknown:
            raise SchemaError(f"{path}: keywords not enforceable locally: {sorted(unknown)}")
        t = s.get("type")
        if t is not None:
            ts = t if isinstance(t, list) else [t]
            if not ts or any(x not in TYPES for x in ts):
                raise SchemaError(f"{path}: invalid type {t!r}")
        props = s.get("properties")
        if props is not None and (not isinstance(props, dict) or any(not isinstance(v, dict) for v in props.values())):
            raise SchemaError(f"{path}: properties must map names to schema objects (boolean schemas are not supported)")
        req = s.get("required")
        if req is not None and (not isinstance(req, list) or any(not isinstance(r, str) for r in req)):
            raise SchemaError(f"{path}: required must be a list of names")
        ap = s.get("additionalProperties")
        if ap is not None and not isinstance(ap, bool):
            raise SchemaError(f"{path}: additionalProperties must be true or false")
        items = s.get("items")
        if items is not None and not isinstance(items, dict):
            raise SchemaError(f"{path}: only single-schema 'items' is supported (array-form items is not)")
        for k in ("minItems", "maxItems", "minLength", "maxLength", "minProperties", "maxProperties"):
            if k in s and (type(s[k]) is not int or s[k] < 0):
                raise SchemaError(f"{path}: {k} must be a non-negative integer")
        for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
            if k in s and type(s[k]) is not int:
                raise SchemaError(f"{path}: {k} must be an integer")
        if "multipleOf" in s and s["multipleOf"] <= 0:
            raise SchemaError(f"{path}: multipleOf must be positive")
        for k in ("anyOf", "allOf"):
            if k in s and (not isinstance(s[k], list) or not s[k] or any(not isinstance(x, dict) for x in s[k])):
                raise SchemaError(f"{path}: {k} must be a non-empty list of schemas")
        if "enum" in s and (not isinstance(s["enum"], list) or not s["enum"]):
            raise SchemaError(f"{path}: enum must be a non-empty list")
        if "uniqueItems" in s and not isinstance(s["uniqueItems"], bool):
            raise SchemaError(f"{path}: uniqueItems must be a boolean")
        ref = s.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise SchemaError(f"{path}: only local $ref ('#/...') is supported, got {ref!r}")
            _resolve(schema, ref)


def _resolve(root: dict, ref: str) -> dict:
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"unresolvable $ref {ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise SchemaError(f"$ref {ref} does not point to a schema object")
    return node


# ============================================================================ validation
class Validator:
    def __init__(self, schema: dict):
        check_schema(schema)
        self.schema = schema
        self.hash = schema_hash(schema)

    def errors(self, instance: Any) -> list[str]:
        out: list[str] = []
        self._validate(instance, self.schema, "$", out, 0)
        return out

    def is_valid(self, instance: Any) -> bool:
        return not self.errors(instance)

    def _validate(self, x: Any, s: dict, path: str, out: list[str], ref_depth: int) -> None:
        if "$ref" in s:
            if ref_depth >= MAX_REF_DEPTH:
                out.append(f"{path}: $ref nesting exceeds {MAX_REF_DEPTH}")
                return
            self._validate(x, _resolve(self.schema, s["$ref"]), path, out, ref_depth + 1)
        if isinstance(x, float):
            out.append(f"{path}: floating-point value refused; numbers must be int or Decimal")
            return
        t = s.get("type")
        if t is not None:
            ts = t if isinstance(t, list) else [t]
            if not any(_is_type(x, tt) for tt in ts):
                out.append(f"{path}: expected {t}, got {type(x).__name__}")
                return
        if "const" in s and not strict_equal(x, s["const"]):
            out.append(f"{path}: must equal {s['const']!r}")
        if "enum" in s and not any(strict_equal(x, e) for e in s["enum"]):
            out.append(f"{path}: not one of the allowed values")
        if isinstance(x, str):
            if "minLength" in s and len(x) < s["minLength"]:
                out.append(f"{path}: shorter than {s['minLength']}")
            if "maxLength" in s and len(x) > s["maxLength"]:
                out.append(f"{path}: longer than {s['maxLength']}")
        if _is_type(x, "number"):
            v = Decimal(x)
            if "minimum" in s and v < s["minimum"]:
                out.append(f"{path}: below minimum {s['minimum']}")
            if "maximum" in s and v > s["maximum"]:
                out.append(f"{path}: above maximum {s['maximum']}")
            if "exclusiveMinimum" in s and v <= s["exclusiveMinimum"]:
                out.append(f"{path}: not above {s['exclusiveMinimum']}")
            if "exclusiveMaximum" in s and v >= s["exclusiveMaximum"]:
                out.append(f"{path}: not below {s['exclusiveMaximum']}")
            if "multipleOf" in s and v % s["multipleOf"] != 0:
                out.append(f"{path}: not a multiple of {s['multipleOf']}")
        if isinstance(x, list):
            if "minItems" in s and len(x) < s["minItems"]:
                out.append(f"{path}: fewer than {s['minItems']} items")
            if "maxItems" in s and len(x) > s["maxItems"]:
                out.append(f"{path}: more than {s['maxItems']} items")
            if s.get("uniqueItems"):
                for i in range(len(x)):
                    if any(strict_equal(x[i], x[j]) for j in range(i)):
                        out.append(f"{path}: items are not unique")
                        break
            if "items" in s:
                for i, item in enumerate(x):
                    self._validate(item, s["items"], f"{path}[{i}]", out, ref_depth)
        if isinstance(x, dict):
            props = s.get("properties", {})
            for name in s.get("required", []):
                if name not in x:
                    out.append(f"{path}: missing required property {name!r}")
            if s.get("additionalProperties") is False:
                extra = set(x) - set(props)
                if extra:
                    out.append(f"{path}: unexpected properties {sorted(extra)}")
            if "minProperties" in s and len(x) < s["minProperties"]:
                out.append(f"{path}: fewer than {s['minProperties']} properties")
            if "maxProperties" in s and len(x) > s["maxProperties"]:
                out.append(f"{path}: more than {s['maxProperties']} properties")
            for name, sub in props.items():
                if name in x:
                    self._validate(x[name], sub, f"{path}.{name}", out, ref_depth)
        if "anyOf" in s:
            if not any(not self._sub_errors(x, sub, path, ref_depth) for sub in s["anyOf"]):
                out.append(f"{path}: matches none of anyOf")
        for i, sub in enumerate(s.get("allOf", [])):
            self._validate(x, sub, path, out, ref_depth)
        if "not" in s and not self._sub_errors(x, s["not"], path, ref_depth):
            out.append(f"{path}: must not match the 'not' schema")
        if "if" in s:
            if not self._sub_errors(x, s["if"], path, ref_depth):
                if "then" in s:
                    self._validate(x, s["then"], path, out, ref_depth)
            elif "else" in s:
                self._validate(x, s["else"], path, out, ref_depth)

    def _sub_errors(self, x: Any, s: dict, path: str, ref_depth: int) -> list[str]:
        out: list[str] = []
        self._validate(x, s, path, out, ref_depth)
        return out


def parse_model_json(text: str) -> Any:
    """Parse model output as JSON with exact decimals; NaN/Infinity are refused."""
    def _const(name: str):
        raise ValueError(f"non-finite number {name} refused")
    return json.loads(text, parse_float=Decimal, parse_constant=_const)


# ============================================================================ provider compatibility
class Enforcement(enum.Enum):
    ENFORCED = "enforced"
    BEST_EFFORT = "best_effort"
    NOT_ENFORCED = "not_enforced"      # provider ignores/strips it; validated locally
    UNVERIFIED = "unverified"          # the register says nothing; validated locally
    REJECTED = "rejected"              # the provider returns 400 or the rule forbids it


@dataclass(frozen=True)
class Finding:
    path: str
    keyword: str
    enforcement: Enforcement
    reason: str
    record_id: str = "CAP-MODEL-02"


@dataclass(frozen=True)
class CompiledSchema:
    provider: str
    schema_hash: str
    provider_schema: dict
    local: Validator
    findings: tuple[Finding, ...]


STRUCTURAL = frozenset({"type", "properties", "required", "items", "$defs", "definitions"}) | ANNOTATIONS


def _refs(s: dict) -> list[str]:
    return [sub["$ref"] for _, sub in _walk(s) if isinstance(sub.get("$ref"), str)]


def _recursive(schema: dict) -> bool:
    """True if some local $ref target (transitively) references itself."""
    graph: dict[str, set[str]] = {}
    for path, s in _walk(schema):
        if path.count("/") == 2 and path.split("/")[1] in ("$defs", "definitions"):
            graph["#/" + path.split("/", 1)[1]] = {r for r in _refs(s) if r.startswith("#/")}
    graph["#"] = {r for r in _refs(schema) if r.startswith("#/")} | ({"#"} if any(r == "#" for r in _refs(schema)) else set())
    def reaches(start: str, target: str, seen: set[str]) -> bool:
        for nxt in graph.get(start, ()):  # follow edges
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                if reaches(nxt, target, seen):
                    return True
        return False
    return any(reaches(node, node, set()) for node in graph)


def _openai(schema: dict) -> list[Finding]:
    f: list[Finding] = []
    total_props = enum_values = chars = 0
    max_depth = 0
    for path, s in _walk(schema):
        depth = path.count("/properties/") + path.count("/items")
        max_depth = max(max_depth, depth)
        for kw in ("allOf", "not", "dependentRequired", "dependentSchemas", "if", "then", "else"):
            if kw in s:
                f.append(Finding(path, kw, Enforcement.REJECTED, "unsupported by OpenAI Structured Outputs"))
        if isinstance(s.get("properties"), dict):
            total_props += len(s["properties"])
            chars += sum(len(k) for k in s["properties"])
        for dk in ("$defs", "definitions"):
            if isinstance(s.get(dk), dict):
                chars += sum(len(k) for k in s[dk])
        if isinstance(s.get("enum"), list):
            enum_values += len(s["enum"])
            chars += sum(len(str(v)) for v in s["enum"])
        if "const" in s:
            chars += len(str(s["const"]))
        if s.get("type") == "object" or "properties" in s:
            if s.get("additionalProperties") is not False:
                f.append(Finding(path, "additionalProperties", Enforcement.REJECTED, "strict mode needs additionalProperties: false on every object"))
            if set(s.get("properties", {})) != set(s.get("required", [])):
                f.append(Finding(path, "required", Enforcement.REJECTED, "strict mode needs every property listed in required (use a null union for optional values)"))
        for kw in set(s) - STRUCTURAL - {"additionalProperties", "allOf", "not", "if", "then", "else"}:
            f.append(Finding(path, kw, Enforcement.UNVERIFIED, "OpenAI support for this keyword is not recorded; validated locally"))
    if total_props > 5000:
        f.append(Finding("#", "properties", Enforcement.REJECTED, f"{total_props} object properties > 5000"))
    if max_depth > 10:
        f.append(Finding("#", "nesting", Enforcement.REJECTED, f"nesting depth {max_depth} > 10"))
    if enum_values > 1000:
        f.append(Finding("#", "enum", Enforcement.REJECTED, f"{enum_values} enum values > 1000"))
    if chars > 120_000:
        f.append(Finding("#", "size", Enforcement.REJECTED, f"{chars} characters of names/enum/const values > 120,000"))
    return f


ANTHROPIC_STRIP = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength"})


def _anthropic(schema: dict) -> list[Finding]:
    f: list[Finding] = []
    optional = unions = 0
    if _recursive(schema):
        f.append(Finding("#", "$ref", Enforcement.REJECTED, "recursive schemas are not supported by Anthropic"))
    for path, s in _walk(schema):
        ref = s.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            f.append(Finding(path, "$ref", Enforcement.REJECTED, "external $ref is not supported by Anthropic"))
        for kw in ANTHROPIC_STRIP & set(s):
            f.append(Finding(path, kw, Enforcement.NOT_ENFORCED, "unsupported by Anthropic (400 if sent); stripped and validated locally"))
        if "maxItems" in s:
            f.append(Finding(path, "maxItems", Enforcement.NOT_ENFORCED, "array constraints beyond minItems 0/1 unsupported; stripped and validated locally"))
        if "minItems" in s and s["minItems"] > 1:
            f.append(Finding(path, "minItems", Enforcement.NOT_ENFORCED, "minItems above 1 unsupported; stripped and validated locally"))
        if s.get("type") == "object" or "properties" in s:
            if s.get("additionalProperties") is not False:
                f.append(Finding(path, "additionalProperties", Enforcement.REJECTED, "Anthropic requires additionalProperties: false"))
            optional += len(set(s.get("properties", {})) - set(s.get("required", [])))
        if "anyOf" in s or isinstance(s.get("type"), list):
            unions += 1
    if optional > 24:
        f.append(Finding("#", "required", Enforcement.REJECTED, f"{optional} optional parameters > 24"))
    if unions > 16:
        f.append(Finding("#", "anyOf", Enforcement.REJECTED, f"{unions} union-typed parameters > 16"))
    return f


def _xai(schema: dict) -> list[Finding]:
    f: list[Finding] = []
    for path, s in _walk(schema):
        if isinstance(s.get("items"), list):
            f.append(Finding(path, "items", Enforcement.REJECTED, "array-form items is rejected by xAI (400)"))
        for kw in ("enum", "anyOf"):
            if kw in s and isinstance(s[kw], list) and not s[kw]:
                f.append(Finding(path, kw, Enforcement.REJECTED, f"zero-variant {kw} is rejected by xAI (400)"))
        for kw in ("maxContains", "minContains"):
            if kw in s:
                f.append(Finding(path, kw, Enforcement.REJECTED, f"{kw} is rejected by xAI (400)"))
        if isinstance(s.get("properties"), dict) and any(isinstance(v, bool) for v in s["properties"].values()):
            f.append(Finding(path, "properties", Enforcement.REJECTED, "boolean property schemas are rejected by xAI (400)"))
        for kw in ("not", "if", "then", "else"):
            if kw in s:
                f.append(Finding(path, kw, Enforcement.BEST_EFFORT, "best effort on xAI; validated locally"))
        if "allOf" in s:
            e = Enforcement.BEST_EFFORT if len(s["allOf"]) > 1 else Enforcement.UNVERIFIED
            f.append(Finding(path, "allOf", e, "allOf with more than one subschema is best effort on xAI; validated locally"))
        for kw, limit in (("minLength", 2048), ("maxLength", 2048), ("minItems", 256), ("maxItems", 256),
                          ("minProperties", 64), ("maxProperties", 64)):
            if kw in s:
                ok = isinstance(s[kw], int) and s[kw] <= limit
                f.append(Finding(path, kw, Enforcement.ENFORCED if ok else Enforcement.BEST_EFFORT,
                                 f"conformance guaranteed up to {limit}" if ok else f"above {limit}: model behaviour only; validated locally"))
        if "additionalProperties" in s:
            f.append(Finding(path, "additionalProperties", Enforcement.ENFORCED, "defaults to false on xAI"))
    return f


GEMINI_NOT_ENFORCED = frozenset({"minLength", "maxLength", "uniqueItems", "contains", "minContains", "maxContains"})


def _gemini(schema: dict) -> list[Finding]:
    f: list[Finding] = []
    for path, s in _walk(schema):
        for kw in GEMINI_NOT_ENFORCED & set(s):
            f.append(Finding(path, kw, Enforcement.NOT_ENFORCED, "stripped by Gemini and not enforced; validated locally"))
        for kw in ("minItems", "maxItems") :
            if kw in s:
                f.append(Finding(path, kw, Enforcement.ENFORCED, "enforced by Gemini"))
        for kw in set(s) - STRUCTURAL - GEMINI_NOT_ENFORCED - {"minItems", "maxItems"}:
            f.append(Finding(path, kw, Enforcement.UNVERIFIED, "Gemini support for this keyword is not recorded; validated locally"))
    f.append(Finding("#", "size", Enforcement.UNVERIFIED, "very large or deeply nested schemas may return 400 INVALID_ARGUMENT; no numeric limit is recorded"))
    return f


_RULES = {"openai": _openai, "anthropic": _anthropic, "xai": _xai, "gemini": _gemini}


def _strip(schema: dict, keywords: frozenset[str], drop_max_items: bool) -> dict:
    out = copy.deepcopy(schema)
    for _, s in _walk(out):
        for kw in keywords & set(s):
            del s[kw]
        if drop_max_items:
            s.pop("maxItems", None)
            if isinstance(s.get("minItems"), int) and s["minItems"] > 1:
                del s["minItems"]
    return out


def compile_for(provider: str, schema: dict) -> CompiledSchema:
    """Check a canonical schema against one provider's recorded rules, with zero network calls."""
    rules = _RULES.get(provider)
    if rules is None:
        raise SchemaError(f"no structured-output rules recorded for provider {provider!r}")
    findings = list(rules(schema))
    try:
        local = Validator(schema)
    except SchemaError as exc:
        findings.append(Finding("#", "local", Enforcement.REJECTED, f"not enforceable locally: {exc}"))
        raise SchemaRejected(provider, findings) from None
    if any(fd.enforcement is Enforcement.REJECTED for fd in findings):
        raise SchemaRejected(provider, findings)
    provider_schema = _strip(schema, ANTHROPIC_STRIP, True) if provider == "anthropic" else copy.deepcopy(schema)
    return CompiledSchema(provider, local.hash, provider_schema, local, tuple(findings))
