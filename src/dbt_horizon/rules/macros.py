"""Project overrides of dbt's naming macros."""
from __future__ import annotations

from ..inventory import Inventory
from .base import Finding, Outcome


def check_macro_overrides(inv: Inventory, mode: str) -> list[Finding]:
    """Read mode keeps the project's naming. Write mode wraps it: the shim calls the
    project's macro, then applies target_naming (schema suffix, identifier case)."""
    if mode != "write":
        return []
    return [Finding("macros.naming_override", "project", Outcome.AUTO_FIXED,
                    f"project defines {name}",
                    f"renamed to project_{name}; the shim's {name} calls it, then applies target_naming")
            for name in inv.root_macro_overrides if name in ("generate_schema_name", "generate_alias_name")]
