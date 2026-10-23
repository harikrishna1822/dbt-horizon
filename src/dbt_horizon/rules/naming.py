"""Write mode: where each model lands in Snowflake."""
from __future__ import annotations

from ..config import TargetNaming
from ..inventory import Inventory
from .base import Finding, Outcome
from .sources import SourcePlan


def plan_targets(inv: Inventory, naming: TargetNaming, materializations: dict[str, str | None],
                 sources: dict[str, SourcePlan]) -> tuple[dict[str, str], list[Finding]]:
    """DATABASE.SCHEMA.IDENTIFIER per persisted model. Writing over a source schema is refused."""
    source_schemas = {(p.database or "").upper() + "." + p.schema.upper() for p in sources.values()}
    targets, findings = {}, []
    for node in inv.of_type("model", "snapshot"):
        if materializations.get(node.unique_id) in (None, "ephemeral"):
            continue
        schema = naming.schema_template.format(schema=node.schema or "", database=node.database or "")
        identifier = node.alias or node.name
        if naming.identifier_case == "upper":
            identifier = identifier.upper()
        targets[node.unique_id] = f"{node.database}.{schema}.{identifier}"
        if f"{(node.database or '').upper()}.{schema.upper()}" in source_schemas:
            findings.append(Finding("naming.overwrites_sources", node.unique_id, Outcome.UNSUPPORTED,
                                    f"{node.name} would be written into {node.database}.{schema}, which holds sources",
                                    "change target_naming.schema"))
    return targets, findings
