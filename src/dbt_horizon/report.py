"""Compatibility report: JSON for machines, Markdown for people."""
from __future__ import annotations

import json
from pathlib import Path

from .inventory import Inventory
from .rules import Analysis, Outcome
from .workspace import Workspace


def report_dict(ws: Workspace, inv: Inventory, analysis: Analysis) -> dict:
    nodes = []
    for node in inv.of_type("model", "seed", "snapshot"):
        nodes.append({
            "unique_id": node.unique_id, "name": node.name, "type": node.resource_type,
            "materialized": node.materialized,
            "planned": analysis.materializations.get(node.unique_id),
            "target": analysis.targets.get(node.unique_id),
            "outcome": analysis.worst(node.unique_id).value,
            "excluded": node.unique_id in analysis.excluded,
        })
    return {
        "repo": ws.checkout.url, "commit": ws.checkout.commit, "project_dir": ws.project_dir,
        "project": inv.project_name, "dbt_version": inv.dbt_version, "mode": analysis.mode,
        "unsupported_policy": analysis.policy, "runnable": analysis.runnable,
        "snowflake_target": f"{inv.target_database}.{inv.target_schema}",
        "counts": analysis.counts(),
        "sources": [p.to_dict() for p in analysis.sources.values()],
        "var_overrides": analysis.var_overrides,
        "nodes": nodes,
        "tests": len(inv.of_type("test")),
        "findings": [f.to_dict() for f in analysis.findings],
        "blocked": analysis.blocked,
        "excluded": analysis.excluded,
        "disabled_hooks": analysis.disabled_hooks,
        "stripped_configs": analysis.stripped_configs,
    }


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    esc = lambda v: str(v).replace("|", "\\|")
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    return lines


def report_markdown(data: dict) -> str:
    c = data["counts"]
    lines = [
        f"# Compatibility report: {data['project']} ({data['mode']} mode)",
        "",
        f"- Repo: `{data['repo']}` @ `{data['commit'][:12]}`, project `{data['project_dir']}`",
        f"- Snowflake target: `{data['snowflake_target']}`, dbt {data['dbt_version']}",
        f"- Findings: {c['unsupported']} unsupported, {c['needs_mapping']} needs mapping, "
        f"{c['review']} review, {c['auto_fixed']} auto-fixed",
        f"- Runnable: **{'yes' if data['runnable'] else 'no'}** (policy `{data['unsupported_policy']}`"
        + (f", {len(data['excluded'])} node(s) excluded" if data["excluded"] else "") + ")",
        "",
        "## Sources",
        "",
    ]
    verified = {True: "in Horizon", False: "**not in Horizon**", None: "not checked"}
    lines += _table(["Source", "Snowflake run reads", "DuckDB reads", "Status"],
                    [[s["source"].split(".", 2)[-1], s["original"], s["reads"], verified[s["verified"]]]
                     for s in data["sources"]])
    if data["var_overrides"]:
        lines += ["", "Passed as `--vars`: " + ", ".join(f"`{k}: {v}`" for k, v in data["var_overrides"].items())]

    lines += ["", "## Models, seeds, snapshots", ""]
    headers = ["Name", "Type", "Snowflake", "DuckDB", "Outcome"] + (["Writes to"] if data["mode"] == "write" else [])
    rows = []
    for n in data["nodes"]:
        row = [n["name"], n["type"], n["materialized"] or "", n["planned"] or "-",
               ("excluded" if n["excluded"] else n["outcome"])]
        if data["mode"] == "write":
            row.append(n["target"] or "")
        rows.append(row)
    lines += _table(headers, rows)
    excluded_tests = sum(1 for uid in data["excluded"] if uid.startswith("test."))
    lines += ["", f"Tests: {data['tests']}" + (f", {excluded_tests} excluded with the nodes they test."
                                                if excluded_tests else ".")]

    lines += ["", "## Findings", ""]
    if data["findings"]:
        lines += _table(["Outcome", "Subject", "Finding", "Action"],
                        [[f["outcome"], f["subject"].split(".")[-1], f["message"], f["action"]]
                         for f in data["findings"]])
    else:
        lines.append("None: the project runs unchanged.")
    if data["excluded"]:
        lines += ["", "## Excluded (blocked, or downstream of a blocked node)", ""]
        lines += [f"- `{uid}`" for uid in data["excluded"]]
    return "\n".join(lines) + "\n"


def write_report(ws: Workspace, inv: Inventory, analysis: Analysis) -> tuple[Path, Path]:
    data = report_dict(ws, inv, analysis)
    json_path = ws.metadata_path / f"compat_report_{analysis.mode}.json"
    md_path = ws.metadata_path / f"compat_report_{analysis.mode}.md"
    json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(report_markdown(data), encoding="utf-8")
    return json_path, md_path


def summary_lines(analysis: Analysis) -> list[str]:
    c = analysis.counts()
    lines = [f"Findings   {c['unsupported']} unsupported, {c['needs_mapping']} needs mapping, "
             f"{c['review']} review, {c['auto_fixed']} auto-fixed"]
    for f in analysis.findings:
        if f.outcome in (Outcome.UNSUPPORTED, Outcome.NEEDS_MAPPING):
            lines.append(f"  {f.outcome.value:13} {f.subject.split('.')[-1]}: {f.message}")
    if analysis.excluded:
        lines.append(f"Excluded   {len(analysis.excluded)} node(s) (policy exclude)")
    lines.append(f"Runnable   {'yes' if analysis.runnable else 'no'}")
    return lines
