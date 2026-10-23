"""Snowflake-only SQL in model code.

Detection only (no rewrite): Jinja is masked and string literals / comments are
removed first, so only real SQL is matched. Functions DuckDB macros can emulate
are auto_fixed (compat macro library); syntax that needs a rewrite is review
until the transpile layer (phase 5); constructs without a DuckDB equivalent are
unsupported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..inventory import Inventory
from .base import Finding, Outcome

_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
_STRINGS_COMMENTS = re.compile(r"'(?:[^']|'')*'|\$\$.*?\$\$|--[^\n]*|/\*.*?\*/", re.S)


@dataclass(frozen=True)
class Feature:
    id: str
    pattern: re.Pattern
    outcome: Outcome
    message: str


def _f(id: str, pattern: str, outcome: Outcome, message: str) -> Feature:
    return Feature(id, re.compile(pattern, re.I), outcome, message)


FEATURES = (
    # emulated by DuckDB macros installed at run start
    _f("iff", r"\biff\s*\(", Outcome.AUTO_FIXED, "IFF()"),
    _f("nvl", r"\bnvl2?\s*\(", Outcome.AUTO_FIXED, "NVL()/NVL2()"),
    _f("div0", r"\bdiv0(null)?\s*\(", Outcome.AUTO_FIXED, "DIV0()"),
    _f("zeroifnull", r"\b(zeroifnull|nullifzero)\s*\(", Outcome.AUTO_FIXED, "ZEROIFNULL()/NULLIFZERO()"),
    _f("to_varchar", r"\bto_varchar\s*\(", Outcome.AUTO_FIXED, "TO_VARCHAR()"),
    _f("array_size", r"\barray_size\s*\(", Outcome.AUTO_FIXED, "ARRAY_SIZE()"),
    # need a rewrite
    _f("flatten", r"\bflatten\s*\(", Outcome.REVIEW, "FLATTEN() (rewrite to UNNEST)"),
    _f("variant_path", r"(?<![:\w])[A-Za-z_]\w*:[A-Za-z_\"]", Outcome.REVIEW, "semi-structured path col:field"),
    _f("parse_json", r"\b(try_)?parse_json\s*\(", Outcome.REVIEW, "PARSE_JSON()"),
    _f("object_construct", r"\bobject_construct(_keep_null)?\s*\(", Outcome.REVIEW, "OBJECT_CONSTRUCT()"),
    _f("try_to", r"\btry_to_\w+\s*\(", Outcome.REVIEW, "TRY_TO_*()"),
    _f("dateadd", r"\bdate(add|diff)\s*\(", Outcome.REVIEW, "DATEADD()/DATEDIFF() (date part syntax)"),
    _f("timestamp_tz", r"\btimestamp_(ntz|ltz|tz)\b", Outcome.REVIEW, "TIMESTAMP_NTZ/LTZ/TZ types"),
    _f("identifier", r"\bidentifier\s*\(", Outcome.REVIEW, "IDENTIFIER()"),
    # no DuckDB equivalent
    _f("connect_by", r"\bconnect\s+by\b", Outcome.UNSUPPORTED, "CONNECT BY"),
    _f("match_recognize", r"\bmatch_recognize\b", Outcome.UNSUPPORTED, "MATCH_RECOGNIZE"),
    _f("stage", r"(?<![\w$@])@[A-Za-z_~%]", Outcome.UNSUPPORTED, "stage reference (@stage)"),
    _f("stage_columns", r"(?<![\w$])\$\d+\b", Outcome.UNSUPPORTED, "stage file columns ($1)"),
)


def sql_body(raw_code: str) -> str:
    """Model code with Jinja masked and literals/comments removed."""
    return _STRINGS_COMMENTS.sub(" ", _JINJA.sub(" __jinja__ ", raw_code))


def detect(raw_code: str) -> list[Feature]:
    body = sql_body(raw_code)
    return [f for f in FEATURES if f.pattern.search(body)]


def check_sql(inv: Inventory) -> list[Finding]:
    findings = []
    for node in inv.of_type("model", "snapshot", "test"):
        if node.language != "sql":
            continue
        for outcome in (Outcome.UNSUPPORTED, Outcome.REVIEW, Outcome.AUTO_FIXED):
            hits = [f for f in detect(node.raw_code) if f.outcome is outcome]
            if not hits:
                continue
            action = {Outcome.AUTO_FIXED: "DuckDB compat macros",
                      Outcome.REVIEW: "rewrite needed (transpile layer, phase 5)",
                      Outcome.UNSUPPORTED: "no DuckDB equivalent"}[outcome]
            findings.append(Finding(f"sql.{outcome.value}", node.unique_id, outcome,
                                    f"{node.name}: {', '.join(f.message for f in hits)}", action))
    return findings
