"""Rule engine: inventory + config (+ Horizon table list) -> analysis.

Pure and deterministic: the same inputs always give the same findings and plan.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

from ..config import AppConfig
from ..inventory import Inventory
from .base import Finding, Outcome
from .configs import strip_configs
from .hooks import plan_hooks
from .macros import check_macro_overrides
from .materializations import plan_materializations
from .naming import plan_targets
from .packages import check_packages
from .sources import SourcePlan, plan_sources
from .sql import check_sql

__all__ = ["Analysis", "Finding", "Outcome", "analyze", "downstream"]


@dataclass
class Analysis:
    mode: str
    policy: str
    findings: list[Finding]
    sources: dict[str, SourcePlan]
    var_overrides: dict[str, str]
    materializations: dict[str, str | None]
    stripped_configs: dict[str, list[str]]
    disabled_hooks: list[dict]
    targets: dict[str, str] = field(default_factory=dict)      # write mode
    blocked: list[str] = field(default_factory=list)            # subjects with a blocking finding
    excluded: list[str] = field(default_factory=list)           # nodes not run (policy exclude)

    @property
    def runnable(self) -> bool:
        return self.policy == "exclude" or not self.blocked

    def counts(self) -> dict[str, int]:
        counter = Counter(f.outcome.value for f in self.findings)
        return {o.value: counter.get(o.value, 0) for o in Outcome}

    def findings_for(self, subject: str) -> list[Finding]:
        return [f for f in self.findings if f.subject == subject]

    def worst(self, subject: str) -> Outcome:
        found = self.findings_for(subject)
        return max((f.outcome for f in found), key=lambda o: o.rank) if found else Outcome.OK


def downstream(start: set[str], child_map: dict[str, list[str]]) -> set[str]:
    """`start` and everything that depends on it (models, tests, snapshots, ...)."""
    seen, queue = set(start), deque(start)
    while queue:
        for child in child_map.get(queue.popleft(), []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def analyze(inv: Inventory, cfg: AppConfig, catalog_tables: set[str] | None,
            mode: str | None = None) -> Analysis:
    mode = mode or cfg.mode
    sources, var_overrides, findings = plan_sources(inv, list(cfg.source_mapping), catalog_tables)
    materializations, more = plan_materializations(inv, mode)
    findings += more
    stripped, more = strip_configs(inv)
    findings += more
    disabled_hooks, more = plan_hooks(inv)
    findings += more
    findings += check_packages(inv)
    findings += check_macro_overrides(inv, mode)
    findings += check_sql(inv)

    targets: dict[str, str] = {}
    if mode == "write":
        targets, more = plan_targets(inv, cfg.target_naming, materializations, sources)
        findings += more

    blocked = sorted({f.subject for f in findings if f.outcome.blocking and f.subject != "project"})
    node_ids = {n.unique_id for n in inv.nodes}
    excluded = (sorted(downstream(set(blocked), inv.child_map) & node_ids)
                if cfg.unsupported_policy == "exclude" else [])
    findings.sort(key=lambda f: (-f.outcome.rank, f.subject, f.rule))
    return Analysis(mode=mode, policy=cfg.unsupported_policy, findings=findings, sources=sources,
                    var_overrides=var_overrides, materializations=materializations,
                    stripped_configs=stripped, disabled_hooks=disabled_hooks, targets=targets,
                    blocked=blocked, excluded=excluded)
