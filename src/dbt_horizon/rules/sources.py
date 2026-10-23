"""Where each dbt source is read from on DuckDB, and whether Horizon serves it."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import SourceMappingRule
from ..inventory import Inventory, SourceInfo
from .base import Finding, Outcome


@dataclass(frozen=True)
class SourcePlan:
    source_id: str
    original: str                 # DB.SCHEMA.TABLE as the Snowflake run reads it
    database: str | None
    schema: str
    identifier: str
    rule_index: int | None        # source_mapping rule that matched, None = same name
    verified: bool | None         # in Horizon? None = not checked (offline)

    @property
    def mapped(self) -> str:
        return ".".join(p for p in (self.database, self.schema, self.identifier) if p)

    def to_dict(self) -> dict:
        return {"source": self.source_id, "original": self.original, "reads": self.mapped,
                "rule_index": self.rule_index, "verified": self.verified}


def map_location(database: str | None, schema: str,
                 rules: list[SourceMappingRule]) -> tuple[str | None, str, int | None]:
    """First matching rule wins; names compare case-insensitively (Snowflake upper-cases)."""
    for i, rule in enumerate(rules):
        if rule.match.database and (database or "").upper() != rule.match.database.upper():
            continue
        if rule.match.schema_name and schema.upper() != rule.match.schema_name.upper():
            continue
        return rule.to.database or database, rule.to.schema_name or schema, i
    return database, schema, None


def plan_sources(inv: Inventory, rules: list[SourceMappingRule],
                 catalog_tables: set[str] | None) -> tuple[dict[str, SourcePlan], dict[str, str], list[Finding]]:
    """Returns (plan per source, --vars overrides, findings).

    `catalog_tables`: upper-case DB.SCHEMA.TABLE names Horizon lists; None = offline.
    """
    plans: dict[str, SourcePlan] = {}
    findings: list[Finding] = []
    var_values: dict[str, set[str]] = {}

    for src in inv.sources:
        database, schema, idx = map_location(src.database, src.schema, rules)
        verified = None
        if catalog_tables is not None and database:
            verified = f"{database}.{schema}.{src.identifier}".upper() in catalog_tables
        plan = SourcePlan(src.unique_id, str(src), database, schema, src.identifier, idx, verified)
        plans[src.unique_id] = plan

        if not database:
            findings.append(Finding("sources.no_database", src.unique_id, Outcome.NEEDS_MAPPING,
                                    f"{src} has no database, so it can't be attached through Horizon",
                                    "add a source_mapping rule with to.database"))
        elif verified is False:
            findings.append(Finding(
                "sources.not_in_horizon", src.unique_id, Outcome.NEEDS_MAPPING,
                f"{plan.mapped} is not an Iceberg table visible to the reader role "
                "(native Snowflake tables are not served to external engines)",
                "add a source_mapping rule to its Iceberg copy, or create one in Snowflake"))
        elif verified is None:
            findings.append(Finding("sources.unverified", src.unique_id, Outcome.REVIEW,
                                    f"reads {plan.mapped}: not checked against Horizon (offline)"))

        for field, var in src.vars.items():
            var_values.setdefault(var, set()).add(database if field == "database" else schema)

    overrides = _var_overrides(inv, var_values, findings)
    for plan in plans.values():
        if plan.rule_index is not None:
            src = next(s for s in inv.sources if s.unique_id == plan.source_id)
            if not _covered_by_vars(src, plan, overrides):
                findings.append(Finding(
                    "sources.rewrite_yml", plan.source_id, Outcome.AUTO_FIXED,
                    f"{plan.original} -> {plan.mapped}",
                    f"database/schema rewritten in {src.file} (workspace copy)"))
    return plans, overrides, findings


def _var_overrides(inv: Inventory, var_values: dict[str, set[str]], findings: list[Finding]) -> dict[str, str]:
    """A var used for source locations is overridden with --vars, when every source using it
    maps to the same value. Vars also used by models are flagged: overriding changes them too."""
    overrides: dict[str, str] = {}
    for var, values in sorted(var_values.items()):
        if len(values) != 1:
            findings.append(Finding(
                "sources.var_conflict", "project", Outcome.REVIEW,
                f"var {var!r} feeds sources that map to different values ({', '.join(sorted(values))})",
                "those sources get their yml rewritten instead of --vars"))
            continue
        value = values.pop()
        if value == inv.project_vars.get(var):
            continue
        users = [n.name for n in inv.of_type("model", "snapshot", "seed")
                 if re.search(rf"""\bvar\(\s*['"]{re.escape(var)}['"]""", n.raw_code)]
        if users:
            findings.append(Finding(
                "sources.var_used_by_models", "project", Outcome.REVIEW,
                f"var {var!r} is also used by {', '.join(sorted(users))}; overriding it to {value!r} changes them too"))
        overrides[var] = value
        findings.append(Finding("sources.var_override", "project", Outcome.AUTO_FIXED,
                                f"var {var!r}: {inv.project_vars.get(var)!r} -> {value!r}",
                                "passed as --vars (no file changed)"))
    return overrides


def _covered_by_vars(src: SourceInfo, plan: SourcePlan, overrides: dict[str, str]) -> bool:
    """True when --vars alone moves the source to its mapped location."""
    for field, original, wanted in (("database", src.database, plan.database), ("schema", src.schema, plan.schema)):
        if original == wanted:
            continue
        var = src.vars.get(field)
        if var is None or overrides.get(var) != wanted:
            return False
    return True
