"""Which materialization each node gets on DuckDB, per mode."""
from __future__ import annotations

from ..inventory import Inventory, NodeInfo
from .base import Finding, Outcome

BUILTIN = {"view", "table", "incremental", "ephemeral", "seed", "snapshot", "test", "unit"}
# Snowflake-only materializations that become a plain table
TO_TABLE = {"dynamic_table", "materialized_view"}


def custom_materializations(inv: Inventory) -> set[str]:
    """Project materializations usable on DuckDB: defined for `default` or `duckdb`."""
    usable = set()
    for name in inv.root_materializations:          # e.g. "my_mat_default", "my_mat_snowflake"
        for suffix in ("_default", "_duckdb"):
            if name.endswith(suffix):
                usable.add(name.removesuffix(suffix))
    return usable


def plan_node(node: NodeInfo, mode: str, custom: set[str]) -> tuple[str | None, Finding | None]:
    """(planned materialization, finding or None when it runs unchanged)."""
    m = node.materialized
    uid = node.unique_id

    if node.resource_type == "model" and node.language == "python":
        return None, Finding("materialization.python_model", uid, Outcome.UNSUPPORTED,
                             f"{node.name} is a Python (Snowpark) model; its DataFrame API differs on DuckDB")

    if node.resource_type == "seed":
        if mode == "write":
            return "seed_local", Finding("materialization.seed_local", uid, Outcome.AUTO_FIXED,
                                         f"seed {node.name} is loaded into the local DuckDB file",
                                         "models read it in the same session; it is not written to Snowflake")
        return "seed", None

    if node.resource_type == "snapshot":
        if mode == "write":
            return None, Finding("materialization.snapshot_write", uid, Outcome.UNSUPPORTED,
                                 f"snapshot {node.name}: snapshots are not supported in write mode yet")
        return "snapshot", None

    if node.resource_type != "model":
        return m, None

    if m in TO_TABLE:
        planned = "iceberg_table" if mode == "write" else "table"
        return planned, Finding("materialization.to_table", uid, Outcome.AUTO_FIXED,
                                f"{node.name}: {m} -> {planned}", "refreshed on every run instead of by Snowflake")
    if m not in BUILTIN:
        if m in custom:
            return m, Finding("materialization.custom", uid, Outcome.REVIEW,
                              f"{node.name} uses the project's own materialization {m!r}",
                              "check it works against an Iceberg catalog")
        return None, Finding("materialization.unknown", uid, Outcome.UNSUPPORTED,
                             f"{node.name} uses materialization {m!r}, which has no DuckDB implementation")

    if mode == "read" or m == "ephemeral":
        return m, None
    if m == "view":
        return "ephemeral", Finding("materialization.view_to_ephemeral", uid, Outcome.AUTO_FIXED,
                                    f"{node.name}: view -> ephemeral",
                                    "DuckDB can't create views in the Iceberg catalog; inlined as a CTE")
    if m == "table":
        return "iceberg_table", Finding("materialization.iceberg_table", uid, Outcome.AUTO_FIXED,
                                        f"{node.name}: table -> iceberg_table",
                                        "drop + CTAS (the Iceberg REST catalog has no rename)")
    # incremental
    return "iceberg_incremental", Finding("materialization.iceberg_incremental", uid, Outcome.AUTO_FIXED,
                                          f"{node.name}: incremental -> iceberg_incremental",
                                          f"strategy {node.config.get('incremental_strategy') or 'default'} "
                                          "applied with MERGE/INSERT on the Iceberg table")


def plan_materializations(inv: Inventory, mode: str) -> tuple[dict[str, str | None], list[Finding]]:
    custom = custom_materializations(inv)
    planned, findings = {}, []
    for node in inv.of_type("model", "seed", "snapshot"):
        materialization, finding = plan_node(node, mode, custom)
        planned[node.unique_id] = materialization
        if finding:
            findings.append(finding)
    return planned, findings
