import pytest

from dbt_horizon.config import SourceMappingRule, TargetNaming
from dbt_horizon.inventory import Inventory, NodeInfo, SourceInfo
from dbt_horizon.rules import downstream
from dbt_horizon.rules.base import Outcome
from dbt_horizon.rules.hooks import is_snowflake_statement
from dbt_horizon.rules.naming import plan_targets
from dbt_horizon.rules.sources import map_location, plan_sources
from dbt_horizon.rules.sql import detect, sql_body


def inventory(sources=(), nodes=(), project_vars=None) -> Inventory:
    return Inventory(project_name="p", profile_name="p", dbt_version="1.12", sources=list(sources),
                     nodes=list(nodes), project_hooks={"on-run-start": [], "on-run-end": []},
                     root_macro_overrides=[], root_materializations=[], packages=[], child_map={},
                     project_vars=project_vars or {})


def rule(match: dict, to: dict) -> SourceMappingRule:
    return SourceMappingRule.model_validate({"match": match, "to": to})


# --- source mapping -------------------------------------------------------------------

def test_map_location_first_match_wins_case_insensitive():
    rules = [rule({"database": "RAW", "schema": "core"}, {"schema": "CORE_ICEBERG"}),
             rule({"schema": "CORE"}, {"database": "LAKE"})]
    assert map_location("raw", "CORE", rules) == ("raw", "CORE_ICEBERG", 0)
    assert map_location("OTHER", "CORE", rules) == ("LAKE", "CORE", 1)
    assert map_location("OTHER", "OPS", rules) == ("OTHER", "OPS", None)


def src(name, database="DB", schema="CORE", vars=None, file="models/sources.yml"):
    return SourceInfo(f"source.p.s.{name}", "s", name, database, schema, name.upper(), file, vars or {})


def test_var_override_when_all_sources_agree():
    inv = inventory([src("a", vars={"schema": "s_schema"}), src("b", vars={"schema": "s_schema"})],
                    project_vars={"s_schema": "CORE"})
    plans, overrides, findings = plan_sources(inv, [rule({"schema": "CORE"}, {"schema": "CORE_ICE"})],
                                              {"DB.CORE_ICE.A", "DB.CORE_ICE.B"})
    assert overrides == {"s_schema": "CORE_ICE"}
    assert [f.rule for f in findings] == ["sources.var_override"]
    assert all(p.verified for p in plans.values())


def test_var_conflict_falls_back_to_yml_rewrite():
    inv = inventory([src("a", schema="CORE", vars={"schema": "v"}), src("b", schema="OPS", vars={"schema": "v"})])
    rules = [rule({"schema": "CORE"}, {"schema": "CORE_ICE"})]
    _, overrides, findings = plan_sources(inv, rules, None)
    assert overrides == {}
    rules_hit = {f.rule for f in findings}
    assert {"sources.var_conflict", "sources.rewrite_yml"} <= rules_hit


def test_hardcoded_source_rewritten_and_missing_flagged():
    inv = inventory([src("a"), src("b", database=None)])
    plans, _, findings = plan_sources(inv, [rule({"schema": "CORE"}, {"schema": "CORE_ICE"})], set())
    outcomes = {(f.subject.split(".")[-1], f.rule): f.outcome for f in findings}
    assert outcomes[("a", "sources.not_in_horizon")] is Outcome.NEEDS_MAPPING
    assert outcomes[("a", "sources.rewrite_yml")] is Outcome.AUTO_FIXED
    assert outcomes[("b", "sources.no_database")] is Outcome.NEEDS_MAPPING


def test_var_also_used_by_model_is_flagged():
    model = NodeInfo("model.p.m", "m", "model", "p", "models/m.sql",
                     raw_code="select '{{ var(\"s_schema\") }}' as s")
    inv = inventory([src("a", vars={"schema": "s_schema"})], [model], {"s_schema": "CORE"})
    _, _, findings = plan_sources(inv, [rule({"schema": "CORE"}, {"schema": "CORE_ICE"})], None)
    assert any(f.rule == "sources.var_used_by_models" for f in findings)


# --- SQL feature detection ------------------------------------------------------------

@pytest.mark.parametrize("sql, expected", [
    ("select iff(a, 1, 2), nvl(b, 0), div0(c, d)", {"iff", "nvl", "div0"}),
    ("select v:field::string from t", {"variant_path"}),
    ("select a::int, b::varchar from t", set()),                       # casts are not paths
    ("select * from t, lateral flatten(input => t.arr) f", {"flatten"}),
    ("select 'iff(x)' as s -- nvl(\n/* flatten( */ from t", set()),     # literals and comments
    ("select {{ iff_macro(x) }} from {{ ref('m') }}", set()),          # Jinja is masked
    ("select dateadd(day, 1, d), try_to_number(x)", {"dateadd", "try_to"}),
    ("select * from t start with p is null connect by prior id = p", {"connect_by"}),
    ("select $1, $2 from @my_stage/file.csv", {"stage", "stage_columns"}),
    ("select '2024-01-01 10:00:00'::timestamp_ntz", {"timestamp_tz"}),
])
def test_detect(sql, expected):
    assert {f.id for f in detect(sql)} == expected


def test_sql_body_strips_dollar_quoted_strings():
    assert "connect" not in sql_body("select $$connect by$$ as s")


# --- hooks ----------------------------------------------------------------------------

@pytest.mark.parametrize("sql, snowflake", [
    ("grant select on {{ this }} to role R", True),
    ("  REVOKE usage on schema x from role R", True),
    ("alter warehouse WH set warehouse_size = 'XSMALL'", True),
    ("use role SYSADMIN", True),
    ("call my_proc()", True),
    ("select system$wait(1)", True),
    ("insert into audit select current_timestamp", False),
    ("{{ log('done') }}", False),
])
def test_hook_classification(sql, snowflake):
    assert is_snowflake_statement(sql) is snowflake


# --- exclusion and naming -------------------------------------------------------------

def test_downstream():
    child_map = {"source.a": ["model.b"], "model.b": ["model.c", "test.t1"], "model.c": [], "model.x": []}
    assert downstream({"source.a"}, child_map) == {"source.a", "model.b", "model.c", "test.t1"}
    assert downstream(set(), child_map) == set()


def test_targets_and_source_schema_guard():
    model = NodeInfo("model.p.fct", "fct", "model", "p", "f.sql", database="DB", schema="CORE", alias="fct")
    inv = inventory(nodes=[model])
    naming = TargetNaming.model_validate({"schema": "{schema}_ICEBERG"})
    targets, findings = plan_targets(inv, naming, {"model.p.fct": "iceberg_table"}, {})
    assert targets == {"model.p.fct": "DB.CORE_ICEBERG.FCT"} and findings == []

    from dbt_horizon.rules.sources import SourcePlan
    sources = {"s": SourcePlan("s", "DB.RAW.X", "DB", "CORE_ICEBERG", "X", 0, True)}
    _, findings = plan_targets(inv, naming, {"model.p.fct": "iceberg_table"}, sources)
    assert [f.rule for f in findings] == ["naming.overwrites_sources"]
