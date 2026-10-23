"""Snowflake-only model configs, stripped by the overlay."""
from __future__ import annotations

from ..inventory import Inventory
from .base import Finding, Outcome

SNOWFLAKE_ONLY_CONFIGS = (
    "automatic_clustering", "base_location_root", "base_location_subpath", "change_tracking",
    "cluster_by", "copy_grants", "external_volume", "grants", "query_tag", "row_access_policy",
    "secure", "snowflake_warehouse", "table_format", "table_tag", "target_lag", "tmp_relation_type",
    "transient", "warehouse",
)


def strip_configs(inv: Inventory) -> tuple[dict[str, list[str]], list[Finding]]:
    stripped, findings = {}, []
    for node in inv.of_type("model", "seed", "snapshot"):
        keys = [k for k in SNOWFLAKE_ONLY_CONFIGS if node.config.get(k) not in (None, False, "", [], {})]
        if keys:
            stripped[node.unique_id] = keys
            findings.append(Finding("configs.snowflake_only", node.unique_id, Outcome.AUTO_FIXED,
                                    f"{node.name}: Snowflake-only config {', '.join(keys)}",
                                    "ignored on DuckDB"))
    return stripped, findings
