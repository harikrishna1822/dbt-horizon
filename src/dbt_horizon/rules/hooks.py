"""Model and project hooks: Snowflake administration statements are disabled, the rest kept."""
from __future__ import annotations

import re

from ..inventory import Inventory
from .base import Finding, Outcome

# Statements that only make sense in Snowflake (privileges, warehouses, sessions, stages, tasks).
SNOWFLAKE_STATEMENT = re.compile(
    r"^\s*(grant|revoke|use\s+(role|warehouse|secondary\s+roles)|"
    r"alter\s+(warehouse|session|user|account|task|pipe|stream)|"
    r"create\s+(or\s+replace\s+)?(stage|pipe|task|stream|warehouse|file\s+format)|"
    r"execute\s+task|call|copy\s+into|put|get|remove|list)\b"
    r"|\bsystem\$", re.I)

_JINJA_ONLY = re.compile(r"^\s*(\{\{.*?\}\}|\{%.*?%\}|\s)*\s*$", re.S)


def _short(sql: str, width: int = 70) -> str:
    one_line = " ".join(sql.split())
    return one_line if len(one_line) <= width else one_line[: width - 3] + "..."


def is_snowflake_statement(sql: str) -> bool:
    return bool(SNOWFLAKE_STATEMENT.search(sql))


def plan_hooks(inv: Inventory) -> tuple[list[dict], list[Finding]]:
    """Returns (disabled hooks, findings). Hooks that are not disabled run on DuckDB as written."""
    disabled, findings = [], []
    hooks = [(n.unique_id, kind, sql)
             for n in inv.of_type("model", "seed", "snapshot")
             for kind, sqls in (("pre-hook", n.pre_hooks), ("post-hook", n.post_hooks))
             for sql in sqls]
    hooks += [("project", kind, sql) for kind, sqls in inv.project_hooks.items() for sql in sqls]

    for subject, kind, sql in hooks:
        if not sql.strip():
            continue
        if is_snowflake_statement(sql):
            disabled.append({"subject": subject, "kind": kind, "sql": sql})
            findings.append(Finding("hooks.snowflake_statement", subject, Outcome.AUTO_FIXED,
                                    f"{kind} `{_short(sql)}`", "disabled: Snowflake administration statement"))
        else:
            # A pure macro call's SQL is only known at run time.
            action = ("macro call: check the SQL it generates runs on DuckDB" if _JINJA_ONLY.match(sql)
                      else "runs on DuckDB as written")
            findings.append(Finding("hooks.kept", subject, Outcome.REVIEW, f"{kind} `{_short(sql)}`", action))
    return disabled, findings
