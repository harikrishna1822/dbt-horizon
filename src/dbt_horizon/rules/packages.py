"""dbt packages: known DuckDB-compatible ones pass, the rest need a look."""
from __future__ import annotations

from ..inventory import Inventory
from .base import Finding, Outcome

# Hub packages whose macros dispatch to DuckDB (or are adapter-neutral).
DUCKDB_COMPATIBLE = {
    "dbt-labs/dbt_utils", "dbt-labs/codegen", "dbt-labs/audit_helper",
    "calogica/dbt_expectations", "metaplane/dbt_expectations",
    "calogica/dbt_date", "godatadriven/dbt_date",
}


def check_packages(inv: Inventory) -> list[Finding]:
    findings = []
    for spec in inv.packages:
        name = spec.get("package") or spec.get("git") or spec.get("local") or spec.get("tarball") or "?"
        if spec.get("package") in DUCKDB_COMPATIBLE:
            continue
        findings.append(Finding("packages.unknown", "project", Outcome.REVIEW,
                                f"package {name}: DuckDB support not known",
                                "check its macros have a duckdb or default implementation"))
    return findings
