#!/usr/bin/env python
"""Pre-flight check for `rasa studio upload` against Rasa Studio 1.x.

Builds the payload that `rasa studio upload` sends with rasa-pro's own
importer, then checks it against the validation Rasa Studio runs when it
receives an upload. Studio's checks are not run by `rasa train` or
`rasa data validate`.

Use it locally, as a pre-commit hook, or in CI. Nothing is uploaded to Studio.

Usage (in the Python env that has rasa-pro installed; see requirements.txt):
    python studio_lint.py \
        --data data --domain domain.yml --config config.yml --endpoints endpoints.yml

Exit codes: 0 = no errors, 1 = errors found, 2 = could not load the project.

Rules mirror Rasa Studio 1.17.x upload validation.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    rule: str
    where: str
    message: str
    severity: str = "error"  # "error" | "warning"
    file: Optional[str] = None  # source file of the problem, when known
    line: Optional[int] = None  # 1-based line in `file`, when known

    @property
    def source(self) -> str:
        if not self.file:
            return ""
        return f"{self.file}:{self.line}" if self.line else self.file

    def __str__(self) -> str:
        src = f"{self.source}  " if self.source else ""
        return (
            f"{self.severity.upper():7} [{self.rule}] {src}{self.where}: {self.message}"
        )


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)

    def error(self, rule: str, where: str, message: str) -> None:
        self.findings.append(Finding(rule, where, message))

    def warn(self, rule: str, where: str, message: str) -> None:
        self.findings.append(Finding(rule, where, message, "warning"))

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    def mark(self) -> int:
        return len(self.findings)

    def locate_since(self, mark: int, file: Optional[str], line: Optional[int]) -> None:
        """Attach a source location to findings added since `mark` that have none."""
        for f in self.findings[mark:]:
            if not f.file and file:
                f.file, f.line = file, line


# --------------------------------------------------------------------------- #
# Studio slot types and condition operators
# --------------------------------------------------------------------------- #

STUDIO_SLOT_TYPES = {"text", "bool", "categorical", "float", "any"}

IS, IS_NOT = "is", "is_not"
IS_SET, IS_EMPTY = "is_set", "is_empty"
CONTAINS, NOT_CONTAINS = "contains", "not_contains"
MATCHES, NOT_MATCHES = "matches", "not_matches"
IS_ONE_OF, IS_NOT_ONE_OF = "is_one_of", "is_not_one_of"
EQ, NEQ = "is_equal_to", "is_not_equal_to"
GT, GTE, LT, LTE = "gt", "gte", "lt", "lte"

ALLOWED_OPERATORS = {
    "bool": {IS, IS_NOT},
    "text": {
        IS,
        IS_NOT,
        IS_SET,
        IS_EMPTY,
        CONTAINS,
        NOT_CONTAINS,
        MATCHES,
        NOT_MATCHES,
        IS_ONE_OF,
        IS_NOT_ONE_OF,
    },
    "categorical": {IS, IS_NOT, IS_SET, IS_EMPTY, IS_ONE_OF, IS_NOT_ONE_OF},
    "float": {EQ, NEQ, GT, GTE, LT, LTE, IS_SET, IS_EMPTY, IS_ONE_OF, IS_NOT_ONE_OF},
    "any": {
        IS,
        IS_NOT,
        GT,
        GTE,
        LT,
        LTE,
        IS_SET,
        IS_EMPTY,
        IS_ONE_OF,
        IS_NOT_ONE_OF,
        CONTAINS,
        NOT_CONTAINS,
        MATCHES,
        NOT_MATCHES,
    },
}

REVERSIBLE = {
    IS: IS_NOT,
    IS_NOT: IS,
    IS_SET: IS_EMPTY,
    IS_EMPTY: IS_SET,
    CONTAINS: NOT_CONTAINS,
    NOT_CONTAINS: CONTAINS,
    IS_ONE_OF: IS_NOT_ONE_OF,
    IS_NOT_ONE_OF: IS_ONE_OF,
    MATCHES: NOT_MATCHES,
    NOT_MATCHES: MATCHES,
}


class ConditionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Studio's condition parser rules
# --------------------------------------------------------------------------- #

_MISSING = object()  # JS `undefined` (no operator/right side), distinct from null
AND_RE = re.compile(r" and ", re.I)
OR_RE = re.compile(r" or ", re.I)
OPERATOR_RE = re.compile(r" (is not|is|matches|contains|=|==|!=|<|<=|>|>=) ", re.I)
CONTEXT_RE = re.compile(r"context\.(?P<name>[a-z_]+)", re.I)
DQ_RE = re.compile(r'"[^"]*"')
SQ_RE = re.compile(r"'[^']*'")


def _mask_quotes(s: str):
    masked: Dict[str, str] = {}
    for prefix, rx in (("doubleQuotesMatches", DQ_RE), ("singleQuotesMatches", SQ_RE)):
        for i, m in enumerate(rx.findall(s)):
            marker = f"{prefix}{i}"
            s = s.replace(m, marker, 1)
            masked[marker] = m
    return s, masked


def _unmask(s, masked):
    if s is None or s is _MISSING:
        return s
    for marker, m in masked.items():
        s = s.replace(marker, m, 1)
    return s


def _strip_quotes(s: str) -> str:
    return re.sub(r"""^['"](.+(?=['"]$))['"]$""", r"\1", s)


def _split_parts(cond: str):
    masked_cond, masked = _mask_quotes(cond)
    parts = OPERATOR_RE.split(masked_cond)
    left = parts[0]
    op = parts[1] if len(parts) > 1 else _MISSING
    right = parts[2] if len(parts) > 2 else _MISSING
    left, right = _unmask(left, masked), _unmask(right, masked)
    if right is not _MISSING and _strip_quotes(right).strip().lower() == "null":
        right = None
    return left, op, right


def _is_slot(s) -> bool:
    return isinstance(s, str) and s.startswith("slots.")


def _is_context(s) -> bool:
    return isinstance(s, str) and CONTEXT_RE.search(s) is not None


def _to_operator(op: str, slot_type: str) -> str:
    table = {">": GT, ">=": GTE, "<": LT, "<=": LTE}
    if op in table:
        return table[op]
    if op in ("=", "=="):
        return EQ if slot_type == "float" else IS
    if op == "!=":
        return NEQ if slot_type == "float" else IS_NOT
    low = op.lower()
    mapping = {
        "is": IS,
        "is not": IS_NOT,
        "is empty": IS_EMPTY,
        "contains": CONTAINS,
        "matches": MATCHES,
    }
    if low in mapping:
        return mapping[low]
    raise ConditionError("INVALID_CONDITION", f"Unsupported comparison operator: {op}")


def _parse_array(s: str) -> List[str]:
    if not s or s[0] != "{" or s[-1] != "}":
        raise ConditionError(
            "INVALID_CONDITION", f"Value set must be written as {{...}}: {s}"
        )
    out = []
    for m in re.finditer(r"""'([^']*)'|"([^"]*)"|(\S+)""", s[1:-1]):
        out.append(m.group(1) or m.group(2) or m.group(3))
    return out


def _is_nan(v: str) -> bool:
    # JS `isNaN(+value)`: "" and whitespace are 0, not NaN.
    v = v.strip()
    if v == "":
        return False
    try:
        float(v)
        return False
    except ValueError:
        return v.lower() not in ("infinity", "+infinity", "-infinity")


def _parse_single(cond: str, slots: Dict[str, Dict[str, Any]]):
    if re.match(r"not ", cond[:4], re.I):
        op, slot_type, values = _parse_single(cond[4:], slots)
        if slot_type == "bool" and op == IS and values and values[0] == "true":
            return op, slot_type, ["false"]
        if op not in REVERSIBLE:
            raise ConditionError(
                "INVALID_CONDITION", f"'not' cannot be applied to: {cond[4:]}"
            )
        return REVERSIBLE[op], slot_type, values

    left, op, right = _split_parts(cond)
    left_ns = _is_slot(left) or _is_context(left)
    right_ns = _is_slot(right) or _is_context(right)
    if left_ns and right_ns:
        raise ConditionError(
            "UNSUPPORTED_FUNCTIONALITY",
            f"Comparing two slots/contexts is not supported. Condition: {cond}",
        )
    if not left_ns and (right is None or right is _MISSING or right == ""):
        raise ConditionError("INVALID_CONDITION", f"Invalid condition: {cond}")
    if left_ns and not left:
        raise ConditionError("INVALID_CONDITION", f"Invalid condition: {cond}")

    slot_name = left[len("slots.") :] if left_ns else str(right)[len("slots.") :]
    slot = slots.get(slot_name)
    if not left_ns and op != "contains":
        raise ConditionError("INVALID_CONDITION", f"Invalid condition: {cond}")

    namespace_ref = left if left_ns else right
    if slot is None:
        if _is_context(namespace_ref):
            # Studio resolves context.* against its built-in contexts, which we
            # can't see. Accept it.
            return IS_SET, "any", []
        raise ConditionError(
            "MISSING_REFERENCED_OBJECT",
            f"Can't find a namespace (slots.<existing slot> or context.<name>) "
            f"in condition: {cond}",
        )

    slot_type = slot.get("type")
    # operator + values (convertConditionOperatorAndValuesFromStrings)
    if right is None or (
        right is not _MISSING
        and _strip_quotes(right).strip().lower() in ("empty", "null")
    ):
        operator = (
            IS_SET if (op is not _MISSING and op.lower() == "is not") else IS_EMPTY
        )
        values: List[str] = []
    elif op is _MISSING:
        operator, values = (IS, ["true"]) if slot_type == "bool" else (IS_SET, [])
    else:
        operator = _to_operator(op, slot_type) if left_ns else IS_ONE_OF
        values = [_strip_quotes(right)] if left_ns else _parse_array(left)

    if operator not in ALLOWED_OPERATORS.get(slot_type, set()):
        raise ConditionError(
            "INVALID_CONDITION",
            f"Operator '{op if op is not _MISSING else '(none)'}' is not allowed "
            f"for slot type '{slot_type}'. Condition: {cond}",
        )
    if slot_type == "float":
        for v in values:
            if _is_nan(v):
                raise ConditionError(
                    "INVALID_CONDITION",
                    f"Value '{v}' is not numeric for float slot '{slot_name}'",
                )
    if slot_type == "bool":
        for v in values:
            if v.lower() not in ("true", "false"):
                raise ConditionError(
                    "INVALID_CONDITION",
                    f"Value '{v}' is not true/false for bool slot '{slot_name}'",
                )
    if slot_type == "categorical":
        cats = {str(c) for c in (slot.get("values") or [])}
        missing = [v for v in values if v not in cats]
        if missing:
            raise ConditionError(
                "INVALID_CONDITION",
                f"Value(s) {missing} are not categories of slot '{slot_name}'",
            )
    return operator, slot_type, values


def check_condition(cond: str, slots: Dict[str, Dict[str, Any]]) -> None:
    """Raises ConditionError if Studio 1.x would reject this condition."""
    masked, masked_map = _mask_quotes(cond)
    if "(" in masked or ")" in masked:
        raise ConditionError(
            "INVALID_CONDITION", f"Parentheses are not allowed in conditions: {cond}"
        )
    or_parts, and_parts = OR_RE.split(masked), AND_RE.split(masked)
    if len(or_parts) > 1 and len(and_parts) > 1:
        raise ConditionError(
            "INVALID_CONDITION",
            f"Condition mixes 'and' and 'or', which Studio cannot import: {cond}",
        )
    subs = (
        or_parts if len(or_parts) > 1 else and_parts if len(and_parts) > 1 else [masked]
    )
    for sub in subs:
        _parse_single(_unmask(sub, masked_map), slots)


# --------------------------------------------------------------------------- #
# Name and format helpers
# --------------------------------------------------------------------------- #

SLOT_NAME_RE = re.compile(r"^(context\.)?[a-zA-Z0-9_][a-zA-Z0-9_-]*$")
NO_PLUS_SLASH_BANG_WS = re.compile(r"[+/!\s]")
NO_PLUS_BANG_WS = re.compile(r"[+!\s]")


def _bad_brace_snippet(text: str) -> str:
    """The first `{...}` (or unmatched `{`) that Studio rejects, for the message."""
    for m in re.finditer(r"\{\{.*?\}\}|\{%.*?%\}|\{[^{}]*\}?", text):
        frag = m.group(0)
        if frag.startswith(("{{", "{%")):
            continue
        if not re.fullmatch(r"\{[a-zA-Z0-9_]+\}", frag):
            return frag[:40]
    return text[:40]


def message_placeholders(text: str):
    """Port of validateMessageFormat. Returns (is_valid, slot_names)."""
    if not text or not text.strip():
        return True, []
    in_slot = in_tpl = False
    cur = ""
    slots: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if not in_slot and not in_tpl and ch == "{":
            if nxt in ("{", "%"):
                in_tpl = True
                i += 2
            else:
                in_slot = True
                i += 1
            continue
        if in_slot:
            if ch == "}":
                in_slot = False
                if not re.fullmatch(r"[a-zA-Z0-9_]+", cur):
                    return False, slots
                slots.append(cur)
                cur = ""
            elif not re.fullmatch(r"[a-zA-Z0-9_]", ch):
                return False, slots
            else:
                cur += ch
            i += 1
            continue
        if in_tpl and ((ch == "}" and nxt == "}") or (ch == "%" and nxt == "}")):
            in_tpl = False
            i += 2
            continue
        i += 1
    if in_slot or in_tpl:
        return False, slots
    return True, slots


LANG_RE = re.compile(
    r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?(-[a-zA-Z0-9]{5,8})*$"
)


def language_code_ok(code: Any) -> bool:
    """Approximation of Studio's language-code check."""
    if not isinstance(code, str):
        return False
    m = re.fullmatch(r"x-([^-]+(?:-[^-]+)*)-([^-]+)", code)
    base = m.group(1) if m else code
    try:  # Use the IANA registry if `langcodes` happens to be installed.
        import langcodes  # type: ignore

        return bool(langcodes.tag_is_valid(base))
    except ImportError:
        return bool(LANG_RE.fullmatch(base))


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def check_config(config: Dict[str, Any], r: Report) -> List[str]:
    where = "config.yml"
    lang = config.get("language")
    extra = config.get("additional_languages") or []
    if not lang:
        r.error(
            "CFG-LANGUAGE",
            where,
            "`language` is required by Studio (Default language is not defined)",
        )
    elif not language_code_ok(lang):
        r.error("CFG-LANGUAGE", where, f"Invalid language code: {lang}")
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        r.error(
            "CFG-LANGUAGE", where, "`additional_languages` must be a list of strings"
        )
        extra = []
    bad = [c for c in extra if not language_code_ok(c)]
    if bad:
        r.error("CFG-LANGUAGE", where, f"Invalid language code(s): {', '.join(bad)}")

    for section in ("pipeline", "policies"):
        items = config.get(section)
        if items is None:
            continue
        if not isinstance(items, list):
            r.error("CFG-SCHEMA", where, f"`{section}` must be a list")
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                r.error(
                    "CFG-SCHEMA",
                    f"{where}:{section}[{i}]",
                    "every item needs a string `name`",
                )
                continue
            if section != "pipeline":
                continue
            llm = item.get("llm")
            if (
                isinstance(llm, dict)
                and llm.get("deployment")
                and not (llm.get("api_base") and llm.get("api_version"))
            ):
                r.error(
                    "CFG-AZURE",
                    f"{where}:{section}[{i}].llm",
                    "When using Azure (deployment), api_base and api_version are required",
                )
            emb = item.get("embeddings")
            if (
                isinstance(emb, dict)
                and emb.get("type") in ("huggingface", "huggingface_local")
                and not (emb.get("model") or emb.get("model_name"))
            ):
                r.error(
                    "CFG-EMBEDDINGS",
                    f"{where}:{section}[{i}].embeddings",
                    "HuggingFace embeddings need `model` or `model_name`",
                )
    return [lang] + list(extra) if lang else list(extra)


def check_endpoints(endpoints: Any, r: Report) -> None:
    if endpoints in (None, {}):
        return
    if not isinstance(endpoints, dict):
        r.error("EP-SCHEMA", "endpoints.yml", "endpoints must be a mapping")
        return
    nlg = endpoints.get("nlg")
    if nlg is not None and not (
        isinstance(nlg, dict)
        and isinstance(nlg.get("type"), str)
        and nlg["type"].strip()
    ):
        r.error("EP-NLG", "endpoints.yml:nlg", "`nlg` needs a non-empty string `type`")
    if (
        isinstance(nlg, dict)
        and "rephrase_all" in nlg
        and not isinstance(nlg["rephrase_all"], bool)
    ):
        r.error("EP-NLG", "endpoints.yml:nlg.rephrase_all", "must be a boolean")


def check_domain(
    domain: Dict[str, Any],
    raw_user_domain: Dict[str, Any],
    all_slots: Dict[str, Dict[str, Any]],
    all_actions: Set[str],
    all_intents: Set[str],
    all_entities: Set[str],
    all_roles: Set[str],
    all_flow_ids: Set[str],
    languages: List[str],
    r: Report,
) -> None:
    dropped = sorted(set(raw_user_domain) - set(DOMAIN_KEYS))
    if dropped:
        r.warn(
            "DOM-DROPPED",
            "domain",
            f"these top-level keys are NOT sent to Studio and will be lost: {dropped}",
        )

    additional = set(languages[1:])

    # --- slots -------------------------------------------------------------
    for name, slot in (domain.get("slots") or {}).items():
        w = f"domain:slots.{name}"
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 128
            or " " in name
        ):
            r.error("SLOT-NAME", w, "slot name must be 1-128 characters with no spaces")
        elif not SLOT_NAME_RE.match(name.strip()):
            r.error(
                "SLOT-NAME",
                w,
                f"Invalid name: {name}, unsupported characters "
                "(allowed: ^(context\\.)?[a-zA-Z0-9_][a-zA-Z0-9_-]*$)",
            )
        if not isinstance(slot, dict):
            r.error("SLOT-SCHEMA", w, "slot must be a mapping")
            continue
        stype = slot.get("type")
        if stype not in STUDIO_SLOT_TYPES:
            r.error(
                "SLOT-TYPE",
                w,
                f"type '{stype}' is not supported by Studio; use one of {sorted(STUDIO_SLOT_TYPES)}",
            )
        values = slot.get("values")
        if (
            "values" in slot
            and values is not None
            and (not isinstance(values, list) or len(values) == 0)
        ):
            r.error("SLOT-VALUES", w, "`values` must be a non-empty list")
        if stype == "categorical" and values is None:
            r.error(
                "SLOT-VALUES",
                w,
                "Slot of type 'categorical' can't have undefined values",
            )
        iv = slot.get("initial_value")
        if iv is not None:
            if not (
                isinstance(iv, (str, int, float, bool))
                or (isinstance(iv, list) and all(isinstance(x, str) for x in iv))
            ):
                r.error(
                    "SLOT-INITIAL",
                    w,
                    "initial_value must be a string, number, boolean, list of strings or null",
                )
            else:
                s = str(iv).lower() if isinstance(iv, bool) else str(iv)
                if s and stype == "bool" and s not in ("true", "false"):
                    r.error(
                        "SLOT-INITIAL",
                        w,
                        "Invalid initial value: bool slots need true/false",
                    )
                if s and stype == "float":
                    try:
                        float(s)
                    except ValueError:
                        r.error(
                            "SLOT-INITIAL",
                            w,
                            "Invalid initial value: float slots need a number",
                        )
                if (
                    s
                    and stype == "categorical"
                    and s not in [str(v) for v in (values or [])]
                ):
                    r.error(
                        "SLOT-INITIAL",
                        w,
                        "Invalid initial value: not one of the slot's `values`",
                    )
                if stype in ("text", "categorical") and s.startswith(
                    ("singleQuotesMatches", "doubleQuotesMatches")
                ):
                    r.error(
                        "SLOT-INITIAL", w, "Invalid initial value (reserved prefix)"
                    )
        for i, mp in enumerate(slot.get("mappings") or []):
            mw = f"{w}.mappings[{i}]"
            mtype = mp.get("type") if isinstance(mp, dict) else None
            allowed = {
                "from_text",
                "from_llm",
                "from_intent",
                "from_entity",
                "custom",
                "controlled",
            }
            if mtype not in allowed:
                r.error(
                    "SLOT-MAPPING",
                    mw,
                    f"mapping type '{mtype}' is not supported by Studio; use one of {sorted(allowed)}",
                )
                continue
            conds = mp.get("conditions")
            if conds is not None and mtype in (
                "from_text",
                "from_intent",
                "from_entity",
            ):
                for j, c in enumerate(conds if isinstance(conds, list) else [conds]):
                    af = c.get("active_flow") if isinstance(c, dict) else None
                    if not isinstance(af, str):
                        r.error(
                            "SLOT-MAPPING",
                            f"{mw}.conditions[{j}]",
                            "Studio only supports `active_flow` mapping conditions",
                        )
                    elif af not in all_flow_ids:
                        r.error(
                            "SLOT-MAPPING",
                            f"{mw}.conditions[{j}]",
                            f"active_flow '{af}' is not a known flow",
                        )
            for key, pool, label in (
                ("intent", all_intents, "intent"),
                ("not_intent", all_intents, "intent"),
                ("entity", all_entities, "entity"),
                ("role", all_roles, "role"),
            ):
                val = mp.get(key)
                if val is None:
                    continue
                for v in val if isinstance(val, list) else [val]:
                    if v not in pool:
                        r.error(
                            "SLOT-MAPPING",
                            mw,
                            f"{label} '{v}' used in mapping is not in the domain",
                        )
            act = (
                # `action` is the deprecated spelling; rasa renames it on upload.
                (mp.get("run_action_every_turn") or mp.get("action"))
                if mtype == "controlled"
                else mp.get("action")
                if mtype == "custom"
                else None
            )
            if act and act not in all_actions:
                r.error(
                    "SLOT-MAPPING",
                    mw,
                    f"action '{act}' used in mapping is not in the domain",
                )

    # --- names -------------------------------------------------------------
    for a in domain.get("actions") or []:
        name = (
            a
            if isinstance(a, str)
            else next(iter(a), "")
            if isinstance(a, dict)
            else ""
        )
        if not name.strip() or NO_PLUS_SLASH_BANG_WS.search(name.strip()):
            r.error(
                "ACTION-NAME",
                f"domain:actions.{name}",
                "action name must be non-empty with no spaces, +, / or !",
            )
    for it in domain.get("intents") or []:
        name = (
            it
            if isinstance(it, str)
            else next(iter(it), "")
            if isinstance(it, dict)
            else ""
        )
        if not name.strip() or NO_PLUS_BANG_WS.search(name.strip()):
            r.error(
                "INTENT-NAME",
                f"domain:intents.{name}",
                "intent name must be non-empty with no spaces, + or !",
            )
    for e in domain.get("entities") or []:
        if isinstance(e, dict):
            for ename, edef in e.items():
                roles = (edef or {}).get("roles") if isinstance(edef, dict) else None
                if not isinstance(roles, list):
                    r.error(
                        "ENTITY-SCHEMA",
                        f"domain:entities.{ename}",
                        "entity mapping must be {roles: [..]} for Studio",
                    )
                for role in roles or []:
                    if NO_PLUS_SLASH_BANG_WS.search(str(role)):
                        r.error(
                            "ENTITY-NAME",
                            f"domain:entities.{ename}.roles",
                            f"role '{role}' has spaces, +, / or !",
                        )
                if NO_PLUS_SLASH_BANG_WS.search(ename):
                    r.error(
                        "ENTITY-NAME",
                        f"domain:entities.{ename}",
                        "entity name has spaces, +, / or !",
                    )
        elif isinstance(e, str) and (not e.strip() or NO_PLUS_SLASH_BANG_WS.search(e)):
            r.error(
                "ENTITY-NAME",
                f"domain:entities.{e}",
                "entity name has spaces, +, / or !",
            )

    # --- responses ---------------------------------------------------------
    for name, variations in (domain.get("responses") or {}).items():
        w = f"domain:responses.{name}"
        if not str(name).startswith("utter_"):
            r.error("RESP-NAME", w, "Response should start with utter_")
        if NO_PLUS_SLASH_BANG_WS.search(str(name).strip()):
            r.error("RESP-NAME", w, "response name must not contain spaces, +, / or !")
        if not isinstance(variations, list):
            r.error("RESP-SCHEMA", w, "response must be a list of variations")
            continue
        for i, v in enumerate(variations):
            vw = f"{w}[{i}]"
            if not isinstance(v, dict):
                r.error("RESP-SCHEMA", vw, "variation must be a mapping")
                continue
            text, custom = v.get("text"), v.get("custom")
            if "text" in v and not isinstance(text, str):
                r.error("RESP-SCHEMA", vw, "`text` must be a string")
            if "custom" in v and not isinstance(custom, dict):
                r.error("RESP-SCHEMA", vw, "`custom` must be a mapping")
            if not text and not custom:
                r.error(
                    "RESP-SCHEMA",
                    vw,
                    "All responses must have at least text or custom YAML",
                )
            meta = v.get("metadata")
            if (
                isinstance(meta, dict)
                and "rephrase" in meta
                and not isinstance(meta["rephrase"], bool)
            ):
                r.error("RESP-SCHEMA", vw, "metadata.rephrase must be a boolean")
            texts = [("text", text)] if isinstance(text, str) else []
            tr = v.get("translation")
            if tr is not None:
                if not isinstance(tr, dict) or not all(
                    isinstance(x, str) for x in tr.values()
                ):
                    r.error(
                        "RESP-SCHEMA",
                        vw,
                        "`translation` must map language codes to strings",
                    )
                else:
                    for lang_code, t in tr.items():
                        texts.append((f"translation.{lang_code}", t))
                        if lang_code not in additional:
                            r.error(
                                "RESP-TRANSLATION",
                                vw,
                                f"translation language '{lang_code}' is not in config additional_languages",
                            )
            for label, t in texts:
                ok, placeholders = message_placeholders(t)
                if not ok:
                    bad = _bad_brace_snippet(t)
                    r.error(
                        "RESP-FORMAT",
                        f"{vw}.{label}",
                        f"Invalid message format near {bad!r}: `{{...}}` must contain only a slot "
                        "name [a-zA-Z0-9_] (use {{ }} for Jinja, and avoid unmatched braces)",
                    )
                for ph in placeholders:
                    if ph not in all_slots:
                        r.error(
                            "RESP-SLOT-REF",
                            f"{vw}.{label}",
                            f'Slot "{ph}" does not exist',
                        )
            for j, b in enumerate(v.get("buttons") or []):
                bw = f"{vw}.buttons[{j}]"
                if not isinstance(b, dict):
                    r.error("RESP-BUTTON", bw, "button must be a mapping")
                    continue
                for k in ("title", "payload"):
                    if not isinstance(b.get(k), str) or not b[k].strip():
                        r.error("RESP-BUTTON", bw, f"Button {k} can't be empty")
                for lang_code, bt in (b.get("translation") or {}).items():
                    if lang_code not in additional:
                        r.error(
                            "RESP-TRANSLATION",
                            bw,
                            f"button translation language '{lang_code}' is not in config additional_languages",
                        )
                    for k in ("title", "payload"):
                        if (
                            not isinstance(bt, dict)
                            or not isinstance(bt.get(k), str)
                            or not bt[k].strip()
                        ):
                            r.error(
                                "RESP-BUTTON",
                                f"{bw}.translation.{lang_code}",
                                f"Button {k} can't be empty",
                            )
            cond = v.get("condition")
            if cond is None:
                continue
            if isinstance(cond, str):
                try:
                    check_condition(cond, all_slots)
                except ConditionError as e:
                    r.error(f"COND-{e.code}", f"{vw}.condition", str(e))
            elif isinstance(cond, list):
                for c in cond:
                    if not (
                        isinstance(c, dict)
                        and c.get("type") == "slot"
                        and isinstance(c.get("name"), str)
                        and isinstance(c.get("value"), (str, int, float, bool))
                    ):
                        r.error(
                            "RESP-CONDITION",
                            f"{vw}.condition",
                            "list conditions must be {type: slot, name: <slot>, value: <str|number|bool>}",
                        )
                    elif c["name"] not in all_slots:
                        r.error(
                            "RESP-CONDITION",
                            f"{vw}.condition",
                            f"Slot '{c['name']}' is not defined in domain",
                        )
            else:
                r.error(
                    "RESP-CONDITION",
                    f"{vw}.condition",
                    "condition must be a string or list",
                )


STEP_KINDS = ("collect", "action", "link", "set_slots", "call", "noop")


def _js_truthy(v: Any) -> bool:
    """Studio checks step kinds with JS truthiness: [] and {} are truthy."""
    if v is None or v is False:
        return False
    if isinstance(v, (list, dict)):
        return True
    return bool(v)


def iter_steps(steps: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(steps, list):
        return
    for s in steps:
        if not isinstance(s, dict):
            continue
        yield s
        nxt = s.get("next")
        if isinstance(nxt, list):
            if (
                nxt
                and isinstance(nxt[0], dict)
                and any(k in nxt[0] for k in ("if", "else"))
            ):
                for branch in nxt:
                    target = (
                        branch.get("then", branch.get("else"))
                        if isinstance(branch, dict)
                        else None
                    )
                    yield from iter_steps(target)
            else:
                yield from iter_steps(nxt)


def iter_branch_conditions(steps: Any) -> Iterable[tuple]:
    """Yields (step that owns the branch, condition string)."""
    for s_ in iter_steps(steps):
        nxt = s_.get("next")
        if isinstance(nxt, list):
            for branch in nxt:
                if isinstance(branch, dict) and isinstance(branch.get("if"), str):
                    yield s_, branch["if"]


def step_label(fid: str, step: Dict[str, Any]) -> str:
    """The step's own `id`, or a readable label when Rasa generated the id
    (`<flow>_<n>_<kind>`), since generated ids don't appear in the YAML."""
    sid = step.get("id")
    if isinstance(sid, str) and not re.match(rf"^{re.escape(fid)}_\d+_", sid):
        return f"id {sid}"
    for k in ("collect", "action", "link", "call"):
        if isinstance(step.get(k), str):
            return f"{k} {step[k]}"
    if step.get("set_slots"):
        names = [
            n for item in step["set_slots"] if isinstance(item, dict) for n in item
        ]
        return "set_slots " + ", ".join(map(str, names))
    if step.get("noop"):
        return "noop"
    return "step"


def step_line(step: Dict[str, Any]) -> Optional[int]:
    """Start line from Rasa's flow metadata ('line_numbers': '69-70')."""
    ln = (
        (step.get("metadata") or {}).get("line_numbers")
        if isinstance(step.get("metadata"), dict)
        else None
    )
    m = re.match(r"(\d+)", str(ln or ""))
    return int(m.group(1)) if m else None


def check_flows(
    flows: Dict[str, Any],
    all_slots: Dict[str, Dict[str, Any]],
    all_actions: Set[str],
    all_responses: Set[str],
    all_intents: Set[str],
    all_flow_ids: Set[str],
    languages: List[str],
    r: Report,
    src: Optional["SourceIndex"] = None,
) -> None:
    additional = set(languages[1:])
    for fid, flow in (flows.get("flows") or {}).items():
        w = f"flows:{fid}"
        ffile = flow.get("file_path") if isinstance(flow, dict) else None
        ffile = ffile or (src.flow_file(fid) if src else None)

        def at_key(key: Optional[str] = None) -> Optional[int]:
            return src.flow_line(fid, key) if src else None

        m_flow = r.mark()
        if not isinstance(flow, dict):
            r.error("FLOW-SCHEMA", w, "flow must be a mapping")
            continue
        if not isinstance(flow.get("description"), str):
            r.error("FLOW-SCHEMA", w, "`description` is required and must be a string")
        if not isinstance(flow.get("steps"), list):
            r.error("FLOW-SCHEMA", w, "`steps` is required and must be a list")
        guard = flow.get("if")
        if isinstance(guard, str) and guard and guard not in ("False", "false"):
            try:
                check_condition(guard, all_slots)
            except ConditionError as e:
                r.error(f"COND-{e.code}", f"{w}.if", str(e))
                r.locate_since(r.mark() - 1, ffile, at_key("if"))
        m_tr = r.mark()
        for lang_code in flow.get("translation") or {}:
            if lang_code not in additional:
                r.error(
                    "FLOW-TRANSLATION",
                    w,
                    f"flow name translation '{lang_code}' is not in config additional_languages",
                )
        r.locate_since(m_tr, ffile, at_key("translation"))
        m_nt = r.mark()
        for trig in flow.get("nlu_trigger") or []:
            intent = trig.get("intent") if isinstance(trig, dict) else None
            iname = intent.get("name") if isinstance(intent, dict) else intent
            if iname not in all_intents:
                r.error(
                    "FLOW-REF",
                    f"{w}.nlu_trigger",
                    f"intent '{iname}' is not declared in the domain intents (NLU-only intents are dropped by Studio)",
                )

        r.locate_since(m_nt, ffile, at_key("nlu_trigger"))
        m_ps = r.mark()
        persisted = flow.get("persisted_slots") or []
        for ps in persisted:
            if ps not in all_slots:
                r.error(
                    "FLOW-PERSISTED",
                    w,
                    f"persisted slot '{ps}' is not in the domain (Studio fails with an internal error)",
                )
        r.locate_since(m_ps, ffile, at_key("persisted_slots"))
        r.locate_since(m_flow, ffile, at_key())
        reset, no_reset = set(), set()

        for step in iter_steps(flow.get("steps")):
            sw = f"{w}.steps[{step_label(fid, step)}]"
            m_step = r.mark()
            try:
                kinds = [k for k in STEP_KINDS if _js_truthy(step.get(k))]
                if len(kinds) != 1:
                    r.error(
                        "STEP-KIND",
                        sw,
                        "Step should have exactly one of: 'collect', 'action', 'link', 'set_slots', 'call', 'noop'"
                        f" (found {kinds or 'none'})",
                    )
                    continue
                kind = kinds[0]
                if kind == "collect":
                    slot = step["collect"]
                    if slot not in all_slots:
                        r.error(
                            "STEP-REF",
                            sw,
                            f"collect slot '{slot}' is not in the domain",
                        )
                    utter = step.get("utter") or f"utter_ask_{slot}"
                    has_msg = utter in all_responses
                    has_act = f"action_ask_{slot}" in all_actions
                    if has_msg and has_act:
                        r.error(
                            "STEP-COLLECT",
                            sw,
                            f"both '{utter}' and 'action_ask_{slot}' exist; Studio accepts only one",
                        )
                    if not has_msg and not has_act and step.get("ask_before_filling"):
                        r.error(
                            "STEP-REF", sw, f"response '{utter}' is not in the domain"
                        )
                    rejections = step.get("rejections") or []
                    if len(rejections) > 1:
                        r.error(
                            "STEP-UNSUPPORTED",
                            sw,
                            "Multiple rejections in a collect step are not supported by Studio",
                        )
                    if rejections:
                        rj = rejections[0]
                        if rj.get("utter") and rj["utter"] not in all_responses:
                            r.error(
                                "STEP-REF",
                                sw,
                                f"rejection response '{rj['utter']}' is not in the domain",
                            )
                        if isinstance(rj.get("if"), str):
                            try:
                                check_condition(rj["if"], all_slots)
                            except ConditionError as e:
                                r.error(f"COND-{e.code}", f"{sw}.rejections", str(e))
                    (
                        no_reset
                        if step.get("reset_after_flow_ends") is False
                        else reset
                    ).add(slot)
                elif kind == "action":
                    a = step["action"]
                    if a.startswith("utter_"):
                        # Studio does not reject a missing response here
                        # (Studio): the upload succeeds but the flow gets
                        # an empty message node. Reported as an error on purpose.
                        if a not in all_responses:
                            r.error(
                                "STEP-EMPTY-MESSAGE",
                                sw,
                                f"response '{a}' is not in the domain; the upload succeeds but Studio "
                                "imports an empty message in its place",
                            )
                    elif a not in all_actions and f"action_{a}" not in all_actions:
                        r.error("STEP-REF", sw, f"action '{a}' is not in the domain")
                elif kind in ("link", "call"):
                    target = step[kind]
                    if target not in all_flow_ids:
                        r.error(
                            "STEP-REF",
                            sw,
                            f"{kind} target '{target}' is not a flow (Studio 1.x can only {kind} flows)",
                        )
                elif kind == "set_slots":
                    for item in step["set_slots"] or []:
                        if not isinstance(item, dict):
                            r.error(
                                "STEP-SCHEMA",
                                sw,
                                "set_slots entries must be {slot: value}",
                            )
                            continue
                        for sname, val in item.items():
                            if sname not in all_slots:
                                r.error(
                                    "STEP-REF",
                                    sw,
                                    f"set_slots slot '{sname}' is not in the domain",
                                )
                            if not (
                                val is None or isinstance(val, (str, bool, int, float))
                            ):
                                r.error(
                                    "STEP-SCHEMA",
                                    sw,
                                    f"set_slots value for '{sname}' must be a string, number, boolean or null",
                                )
                elif kind == "noop" and step["noop"] is not True:
                    r.error("STEP-SCHEMA", sw, "`noop` must be true")
            finally:
                r.locate_since(m_step, ffile, step_line(step))

        overlap = sorted((no_reset - reset) & set(persisted))
        if overlap:
            r.error(
                "FLOW-PERSISTED",
                w,
                f"slots {overlap} are in persisted_slots AND in a collect step with "
                "reset_after_flow_ends: false; use only one",
            )
            r.locate_since(r.mark() - 1, ffile, at_key("persisted_slots"))

        for owner, cond in iter_branch_conditions(flow.get("steps")):
            try:
                check_condition(cond, all_slots)
            except ConditionError as e:
                r.error(
                    f"COND-{e.code}",
                    f"{w}.steps[{step_label(fid, owner)}].next `if: {cond}`",
                    str(e),
                )
                r.locate_since(r.mark() - 1, ffile, step_line(owner))


def check_nlu(
    nlu: Dict[str, Any],
    domain_intents: Set[str],
    all_entities: Set[str],
    all_roles: Set[str],
    r: Report,
) -> None:
    """Studio imports only intents declared in the domain. Entity annotations
    in their examples must reference domain entities/roles."""
    rx = re.compile(r"\[([^\[\]]+)\](\{[^{}]*\})")
    rx2 = re.compile(r"\[([^\[\]]+)\](\((\w+)\))")
    import json

    seen: Set[str] = set()
    dropped: Set[str] = set()
    for entry in nlu.get("nlu") or []:
        if not isinstance(entry, dict) or not entry.get("intent"):
            continue  # synonyms, regexes, lookups are ignored by Studio
        intent = entry["intent"]
        if intent not in domain_intents:
            dropped.add(intent)
            continue
        if intent in seen:
            continue  # the upload merges blocks per intent; load() does the same
        seen.add(intent)
        ex = entry.get("examples")
        lines = ex.split("\n") if isinstance(ex, str) else ex or []
        for line in lines:
            line = re.sub(r"^- ", "", str(line)).strip()
            for m in rx.finditer(line):
                try:
                    d = json.loads(m.group(2))
                except ValueError:
                    r.error(
                        "NLU-ENTITY",
                        f"nlu:{intent}",
                        f"entity annotation is not valid JSON: {m.group(0)}",
                    )
                    continue
                if d.get("entity") not in all_entities:
                    r.error(
                        "NLU-ENTITY",
                        f"nlu:{intent}",
                        f"Can't find entity {d.get('entity')} in domain",
                    )
                if d.get("role") and d["role"] not in all_roles:
                    r.error(
                        "NLU-ENTITY",
                        f"nlu:{intent}",
                        f"Can't find role {d['role']} in domain",
                    )
            for m in rx2.finditer(line):
                if m.group(3) not in all_entities:
                    r.error(
                        "NLU-ENTITY",
                        f"nlu:{intent}",
                        f"Can't find entity {m.group(3)} in domain",
                    )
    if dropped:
        r.warn(
            "NLU-DROPPED",
            "nlu",
            f"intents with NLU examples but not declared in the domain are dropped by Studio: {sorted(dropped)}",
        )


def check_assistant_name(name: Optional[str], r: Report) -> None:
    if name is None:
        return
    s = name.strip()
    if not s or len(s) > 128 or " " in name:
        r.error(
            "ASSISTANT-NAME",
            "assistant name",
            "must be 1-128 characters with no spaces",
        )


# --------------------------------------------------------------------------- #
# Loading: rasa-pro's own importer builds the upload payload; plain YAML
# reading/writing uses ruamel directly.
# --------------------------------------------------------------------------- #

try:  # the domain keys `rasa studio upload` sends
    from rasa.shared.importers.utils import DOMAIN_KEYS
except ImportError:  # keeps the pure checks testable without rasa
    DOMAIN_KEYS = [
        "version",
        "actions",
        "responses",
        "slots",
        "intents",
        "entities",
        "forms",
        "session_config",
    ]


class LoadError(Exception):
    pass


def _yaml():
    from ruamel.yaml import YAML

    return YAML(typ="safe", pure=True)  # YAML 1.2, like Studio's parser


def _read_yaml(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return _yaml().load(fh)
    except FileNotFoundError:
        raise LoadError(f"file not found: {path}")
    except Exception as e:  # noqa: BLE001
        raise LoadError(f"{path} is not valid YAML: {e}")


def _yaml_roundtrip(obj: Any) -> Any:
    """Write the part as YAML and parse it back as YAML 1.2, like Studio does."""
    if not obj:
        return {}
    buf = io.StringIO()
    y = _yaml()
    y.default_flow_style = False
    y.dump(obj, buf)
    return _yaml().load(io.StringIO(buf.getvalue())) or {}


def domain_file_keys(domain_path: str) -> Set[str]:
    """Top-level keys actually written in the domain file(s). Rasa's
    Domain.as_dict() adds keys of its own (e.g. e2e_actions), so read the files."""
    files = (
        [domain_path]
        if os.path.isfile(domain_path)
        else [
            os.path.join(root, f)
            for root, _, fs in os.walk(domain_path)
            for f in fs
            if f.endswith((".yml", ".yaml"))
        ]
    )
    keys: Set[str] = set()
    for f in files:
        doc = _read_yaml(f)
        if isinstance(doc, dict):
            keys |= set(doc)
    return keys


def _yaml_paths(path: str) -> List[str]:
    if os.path.isfile(path):
        return [path]
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        out += [
            os.path.join(root, f)
            for f in sorted(files)
            if f.endswith((".yml", ".yaml"))
        ]
    return out


class SourceIndex:
    """Maps domain/flow/config/NLU items back to `file:line`, so every finding
    can point at the exact place to fix. Built from the project files with
    ruamel's round-trip loader (which records line numbers)."""

    def __init__(
        self, domain: str, data: List[str], config: str, endpoints: Optional[str]
    ):
        self.domain: Dict[tuple, tuple] = {}
        self.flows: Dict[tuple, tuple] = {}
        self.nlu: Dict[str, tuple] = {}
        self.files: Dict[str, tuple] = {}
        for f in _yaml_paths(domain) if os.path.exists(domain) else []:
            self._index_domain(f)
        for d in data:
            for f in _yaml_paths(d) if os.path.exists(d) else []:
                self._index_data(f)
        for name, path in (("config", config), ("endpoints", endpoints)):
            if path and os.path.exists(path):
                self._index_flat(name, path)

    @staticmethod
    def _load(path: str) -> Any:
        from ruamel.yaml import YAML

        try:
            with open(path, encoding="utf-8") as fh:
                return YAML(typ="rt", pure=True).load(fh)
        except Exception:  # noqa: BLE001 - indexing is best effort
            return None

    @staticmethod
    def _key_line(node: Any, key: Any) -> Optional[int]:
        try:
            return node.lc.key(key)[0] + 1
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _item_line(node: Any, i: int) -> Optional[int]:
        try:
            return node.lc.item(i)[0] + 1
        except Exception:  # noqa: BLE001
            return None

    def _index_domain(self, path: str) -> None:
        doc = self._load(path)
        if not isinstance(doc, dict):
            return
        for key, val in doc.items():
            self.domain.setdefault(("top", str(key)), (path, self._key_line(doc, key)))
            if isinstance(val, dict):
                for name, item in val.items():
                    self.domain[(str(key), str(name))] = (
                        path,
                        self._key_line(val, name),
                    )
                    if isinstance(item, list):
                        for i in range(len(item)):
                            self.domain[(str(key), str(name), i)] = (
                                path,
                                self._item_line(item, i),
                            )
            elif isinstance(val, list):
                for i, item in enumerate(val):
                    name = (
                        item
                        if isinstance(item, str)
                        else (
                            next(iter(item))
                            if isinstance(item, dict) and item
                            else None
                        )
                    )
                    if name is not None:
                        self.domain[(str(key), str(name))] = (
                            path,
                            self._item_line(val, i),
                        )

    def _index_data(self, path: str) -> None:
        doc = self._load(path)
        if not isinstance(doc, dict):
            return
        flows = doc.get("flows")
        if isinstance(flows, dict):
            for fid, flow in flows.items():
                self.flows[(str(fid), None)] = (path, self._key_line(flows, fid))
                if isinstance(flow, dict):
                    for k in flow:
                        self.flows[(str(fid), str(k))] = (path, self._key_line(flow, k))
        nlu = doc.get("nlu")
        if isinstance(nlu, list):
            for i, entry in enumerate(nlu):
                if isinstance(entry, dict) and entry.get("intent"):
                    self.nlu.setdefault(
                        str(entry["intent"]), (path, self._item_line(nlu, i))
                    )

    def _index_flat(self, name: str, path: str) -> None:
        doc = self._load(path)
        self.files[(name, None)] = (path, None)
        if not isinstance(doc, dict):
            return
        for key, val in doc.items():
            self.files[(name, str(key))] = (path, self._key_line(doc, key))
            if isinstance(val, list):
                for i in range(len(val)):
                    self.files[(name, f"{key}[{i}]")] = (path, self._item_line(val, i))

    # -- lookups ------------------------------------------------------------

    def flow_file(self, fid: str) -> Optional[str]:
        return (self.flows.get((fid, None)) or (None, None))[0]

    def flow_line(self, fid: str, key: Optional[str] = None) -> Optional[int]:
        hit = self.flows.get((fid, key)) or self.flows.get((fid, None))
        return hit[1] if hit else None

    def _domain_entry(self, rest: str) -> Optional[tuple]:
        section, _, tail = rest.partition(".")
        names = sorted(
            (k[1] for k in self.domain if len(k) == 2 and k[0] == section),
            key=len,
            reverse=True,
        )
        for name in names:
            if tail == name or tail.startswith((name + ".", name + "[", name + ":")):
                m = re.match(r"\[(\d+)\]", tail[len(name) :])
                if m and (section, name, int(m.group(1))) in self.domain:
                    return self.domain[(section, name, int(m.group(1)))]
                return self.domain[(section, name)]
        return None

    def resolve(self, f: "Finding") -> None:
        """Fill in f.file / f.line from the finding's `where`, if not set."""
        if f.file:
            return
        w = f.where
        hit = None
        if w.startswith("domain:"):
            hit = self._domain_entry(w[len("domain:") :])
        elif w == "domain":
            for key in re.findall(r"'([^']+)'", f.message):
                if ("top", key) in self.domain:
                    hit = self.domain[("top", key)]
                    break
        elif w.startswith(("config.yml", "endpoints.yml")):
            name = "config" if w.startswith("config.yml") else "endpoints"
            sub = w.split(":", 1)[1] if ":" in w else None
            key = re.match(r"([^.\[]+(?:\[\d+\])?)", sub or "")
            hit = (
                (self.files.get((name, key.group(1))) if key else None)
                or (self.files.get((name, sub.split("[")[0])) if sub else None)
                or self.files.get((name, None))
            )
            if hit is None or (hit[1] is None and "language" in f.message):
                hit = (
                    self.files.get((name, "language"))
                    or self.files.get((name, "additional_languages"))
                    or hit
                )
        elif w.startswith("nlu:"):
            hit = self.nlu.get(w[len("nlu:") :])
        elif w.startswith("flows:"):
            fid = re.split(r"[.\s]", w[len("flows:") :], maxsplit=1)[0]
            hit = self.flows.get((fid, None))
        if hit:
            f.file, f.line = hit


def load(args: argparse.Namespace):
    from rasa.core.config.configuration import Configuration
    from rasa.shared.core.domain import Domain
    from rasa.shared.core.flows.yaml_flows_io import YAMLFlowsReader
    from rasa.shared.importers.importer import TrainingDataImporter
    from rasa.shared.importers.utils import extract_calm_import_parts_from_importer
    from rasa.shared.nlu.training_data.formats.rasa_yaml import RasaYAMLReader

    Configuration.initialise_empty()
    # Same as handle_upload: env vars are NOT expanded on upload.
    Domain.expand_env_vars = False
    RasaYAMLReader.expand_env_vars = False
    YAMLFlowsReader.expand_env_vars = False

    importer = TrainingDataImporter.load_from_dict(
        domain_path=args.domain,
        config_path=args.config,
        training_data_paths=args.data,
        expand_env_vars=False,
    )
    config = _read_yaml(args.config)
    endpoints = (
        _read_yaml(args.endpoints)
        if args.endpoints and os.path.exists(args.endpoints)
        else {}
    )
    parts = extract_calm_import_parts_from_importer(
        importer=importer, config=config, endpoints=endpoints or {}
    )
    return importer, parts


# --------------------------------------------------------------------------- #
# Report files (--json / --report)
# --------------------------------------------------------------------------- #

TOOL_VERSION = "1.0"
STUDIO_TARGET = "1.17.x"

AREAS = [
    ("config.yml", "config"),
    ("endpoints.yml", "endpoints"),
    ("domain", "domain"),
    ("flows", "flows"),
    ("nlu", "nlu"),
]
AREA_TITLES = {
    "config": "config.yml",
    "endpoints": "endpoints.yml",
    "domain": "Domain",
    "flows": "Flows",
    "nlu": "NLU data",
    "other": "Other",
}

FIX_HINTS = {
    "COND": "Rewrite the condition: no parentheses, don't mix 'and' with 'or', put spaces "
    "around operators, don't compare two slots, use only true/false on bool slots.",
    "RESP-FORMAT": "Use {slot_name} for slots and {{ }} for Jinja; remove stray braces.",
    "RESP-SLOT-REF": "Add the slot to the domain or fix the placeholder name.",
    "RESP-SCHEMA": "Give each variation a string `text` or a mapping `custom`.",
    "RESP-BUTTON": "Give every button a non-empty title and payload.",
    "RESP-CONDITION": "Use {type: slot, name, value} conditions on existing slots.",
    "RESP-NAME": "Rename: start with utter_, no spaces, +, / or !.",
    "RESP-TRANSLATION": "Add the language to additional_languages in config.yml, or remove the translation.",
    "FLOW-TRANSLATION": "Add the language to additional_languages in config.yml, or remove the translation.",
    "SLOT-TYPE": "Use text, bool, categorical, float or any.",
    "SLOT-NAME": "Rename: 1-128 characters, letters, digits, _ or -.",
    "SLOT-VALUES": "Give categorical slots a non-empty `values` list.",
    "SLOT-INITIAL": "Make initial_value match the slot type (or one of its values).",
    "SLOT-MAPPING": "Use a supported mapping type and only `active_flow` conditions; fix unknown references.",
    "STEP-KIND": "Give the step exactly one of collect, action, link, set_slots, call, noop.",
    "STEP-REF": "Add the missing slot/action/flow, or fix the name. `call` can only target flows.",
    "STEP-EMPTY-MESSAGE": "Add the response to the domain, or fix the name.",
    "STEP-COLLECT": "Keep either utter_ask_<slot> or action_ask_<slot>, not both.",
    "STEP-UNSUPPORTED": "Combine the rejections into one.",
    "STEP-SCHEMA": "Use {slot: value} with a string, number, boolean or null value.",
    "FLOW-SCHEMA": "Give the flow a `description` and `steps`.",
    "FLOW-REF": "Declare the intent under `intents:` in the domain.",
    "FLOW-PERSISTED": "Use persisted_slots OR reset_after_flow_ends: false for a slot, not both; "
    "only list existing slots.",
    "NLU-ENTITY": "Declare the entity/role in the domain, or fix the annotation.",
    "CFG-LANGUAGE": "Set `language` to a valid code, e.g. en or en-US.",
    "CFG-SCHEMA": "Give every pipeline/policy item a `name`.",
    "CFG-AZURE": "Add api_base and api_version to the Azure llm config.",
    "CFG-EMBEDDINGS": "Add `model` or `model_name` to the HuggingFace embeddings.",
    "EP-NLG": "Give the nlg endpoint a string `type`.",
    "ACTION-NAME": "Rename: no spaces, +, / or !.",
    "INTENT-NAME": "Rename: no spaces, + or !.",
    "ENTITY-NAME": "Rename: no spaces, +, / or !.",
    "ENTITY-SCHEMA": "Write entity roles as {entity: {roles: [..]}}.",
    "ASSISTANT-NAME": "Use 1-128 characters with no spaces.",
    "DOM-DROPPED": "Nothing to fix if intended; these keys won't exist in Studio.",
    "NLU-DROPPED": "Declare the intents in the domain if Studio should keep them.",
    "LOAD": "Fix this so Rasa can load the project, then run the lint again.",
}


def _area_and_location(where: str):
    for prefix, area in AREAS:
        if where == prefix or where.startswith(prefix + ":"):
            return area, where[len(prefix) + 1 :] or prefix
    return "other", where


def _hint(rule: str) -> str:
    return FIX_HINTS.get(rule) or FIX_HINTS.get(rule.split("-")[0], "")


def _rasa_version() -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("rasa-pro")
    except Exception:  # noqa: BLE001
        return None


def _write(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def build_json(r: Report, args: argparse.Namespace, result: str, exit_code: int) -> str:
    import json
    from collections import Counter
    from datetime import datetime, timezone

    findings = []
    for f in r.findings:
        area, loc = _area_and_location(f.where)
        findings.append(
            {
                "severity": f.severity,
                "rule": f.rule,
                "area": area,
                "location": loc,
                "file": f.file,
                "line": f.line,
                "message": f.message,
                "fix": _hint(f.rule),
            }
        )
    n_err = len(r.errors)
    doc = {
        "tool": "studio_lint",
        "tool_version": TOOL_VERSION,
        "studio_target": STUDIO_TARGET,
        "rasa_pro_version": _rasa_version(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": {
            "cwd": os.getcwd(),
            "data": args.data,
            "domain": args.domain,
            "config": args.config,
            "endpoints": args.endpoints,
        },
        "result": result,  # "pass" | "fail" | "load_error"
        "exit_code": exit_code,
        "summary": {
            "errors": n_err,
            "warnings": len(r.findings) - n_err,
            "by_rule": dict(sorted(Counter(f.rule for f in r.findings).items())),
        },
        "findings": findings,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def build_text_report(r: Report, args: argparse.Namespace, result: str) -> str:
    from collections import OrderedDict
    from datetime import datetime

    n_err = len(r.errors)
    n_warn = len(r.findings) - n_err
    headline = {
        "pass": "PASS - no errors. The project should upload to Studio.",
        "fail": f"FAIL - fix {n_err} error(s) before uploading to Studio.",
        "load_error": "NOT CHECKED - Rasa could not load the project, so no Studio checks ran.",
    }[result]
    if result == "pass" and args.fail_on_warnings and n_warn:
        headline = f"FAIL - {n_warn} warning(s) and --fail-on-warnings is set."

    out = [
        "Studio upload check (studio_lint)",
        "=" * 34,
        f"Result:   {headline}",
        f"Errors:   {n_err}",
        f"Warnings: {n_warn}",
        "",
        f"Project:  {os.getcwd()}",
        f"Files:    data={' '.join(args.data)}  domain={args.domain}  config={args.config}  "
        f"endpoints={args.endpoints}",
        f"Checked:  {datetime.now().strftime('%Y-%m-%d %H:%M')} against Studio {STUDIO_TARGET} "
        f"rules with rasa-pro {_rasa_version() or '?'}",
    ]

    for severity, title, blurb in (
        (
            "error",
            "ERRORS",
            "Studio will reject the upload, or import a broken assistant. Fix all of these.",
        ),
        (
            "warning",
            "WARNINGS",
            "The upload works, but something is dropped or changed. Review these.",
        ),
    ):
        items = [f for f in r.findings if f.severity == severity]
        if not items:
            continue
        out += [
            "",
            "",
            f"{title} ({len(items)})",
            "-" * (len(title) + len(str(len(items))) + 3),
            blurb,
        ]
        by_area: "OrderedDict[str, list]" = OrderedDict()
        for f in items:
            by_area.setdefault(_area_and_location(f.where)[0], []).append(f)
        for area in [a for _, a in AREAS] + ["other"]:
            if area not in by_area:
                continue
            out += ["", f"{AREA_TITLES[area]}:"]
            by_rule: "OrderedDict[str, list]" = OrderedDict()
            for f in by_area[area]:
                by_rule.setdefault(f.rule, []).append(f)
            for rule, fs in by_rule.items():
                out.append(f"  [{rule}] x{len(fs)}")
                hint = _hint(rule)
                if hint:
                    out.append(f"    How to fix: {hint}")
                for f in fs:
                    loc = _area_and_location(f.where)[1]
                    out.append(f"    - {loc}")
                    if f.source:
                        out.append(f"      at {f.source}")
                    out.append(f"      {f.message}")
    if not r.findings:
        out += ["", "No issues found."]
    out += [
        "",
        "",
        "This is a pre-check: it mirrors Studio 1.17 upload validation without "
        "uploading. If an upload still fails, report the Studio error so the rule can be added.",
    ]
    return "\n".join(out) + "\n"


def write_outputs(
    r: Report, args: argparse.Namespace, result: str, exit_code: int
) -> None:
    if args.json:
        _write(args.json, build_json(r, args, result, exit_code))
        print(f"studio_lint: wrote JSON findings to {args.json}", file=sys.stderr)
    if args.report:
        _write(args.report, build_text_report(r, args, result))
        print(f"studio_lint: wrote report to {args.report}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-d", "--data", nargs="+", default=["data"])
    p.add_argument(
        "--domain", default="domain.yml" if os.path.exists("domain.yml") else "domain"
    )
    p.add_argument("-c", "--config", default="config.yml")
    p.add_argument("--endpoints", default="endpoints.yml")
    p.add_argument(
        "--assistant-name",
        default=None,
        help="Also check the Studio assistant name rules",
    )
    p.add_argument("--fail-on-warnings", action="store_true")
    p.add_argument(
        "--json",
        metavar="PATH",
        help="Also write findings as JSON (for CI/tooling), e.g. studio-lint.json",
    )
    p.add_argument(
        "--report",
        metavar="PATH",
        help="Also write a human-readable report, e.g. studio-lint-report.txt",
    )
    args = p.parse_args(argv)

    r = Report()
    try:
        importer, parts = load(args)
        domain_keys = domain_file_keys(args.domain)
        src = SourceIndex(args.domain, args.data, args.config, args.endpoints)
    except Exception as e:  # noqa: BLE001 - report any loader failure plainly
        msg = f"could not load the project the way `rasa studio upload` does: {e}"
        print(f"ERROR   [LOAD] {msg}", file=sys.stderr)
        r.error("LOAD", "project", msg)
        write_outputs(r, args, "load_error", 2)
        return 2

    domain = _yaml_roundtrip(parts.domain)
    flows = _yaml_roundtrip(parts.flows)
    config = _yaml_roundtrip(parts.config)
    endpoints = _yaml_roundtrip(parts.endpoints)
    nlu = _yaml_roundtrip(parts.nlu)

    # Reference targets: the full domain/flows, including Rasa's defaults.
    full_domain = importer.get_domain()
    all_slots = dict(full_domain.as_dict().get("slots") or {})
    all_actions = set(full_domain.action_names_or_texts) | set(full_domain.user_actions)
    all_responses = set(full_domain.responses)
    # Studio resolves intents against domain intents only (NLU-only intents are dropped).
    all_intents = set(full_domain.intents)
    all_entities = set(full_domain.entities)
    all_roles = {
        role for roles in full_domain.entity_properties.roles.values() for role in roles
    }
    all_flow_ids = {f.id for f in importer.get_flows()}

    check_assistant_name(args.assistant_name, r)
    languages = check_config(config, r)
    check_endpoints(endpoints, r)
    check_domain(
        domain,
        domain_keys,
        all_slots,
        all_actions,
        all_intents,
        all_entities,
        all_roles,
        all_flow_ids,
        languages,
        r,
    )
    check_flows(
        flows,
        all_slots,
        all_actions,
        all_responses,
        all_intents,
        all_flow_ids,
        languages,
        r,
        src,
    )
    check_nlu(nlu, all_intents, all_entities, all_roles, r)

    for f in r.findings:
        src.resolve(f)
        print(f)
    n_err, n_warn = len(r.errors), len(r.findings) - len(r.errors)
    print(f"\nstudio_lint: {n_err} error(s), {n_warn} warning(s)")
    exit_code = 1 if (n_err or (args.fail_on_warnings and n_warn)) else 0
    write_outputs(r, args, "fail" if n_err else "pass", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
