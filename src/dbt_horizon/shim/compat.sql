{#- dbt-horizon shim: DuckDB macros emulating Snowflake functions, so model SQL runs unchanged.
    Called from on-run-start. A macro is only created when DuckDB has no function of that name.
    Created in <database>.main, which is on DuckDB's default search path. -#}
{% macro dbt_horizon_install_compat() %}
  {% if execute %}
    {% set definitions = {
      'iff':        'iff(cond, a, b) AS CASE WHEN cond THEN a ELSE b END',
      'nvl':        'nvl(a, b) AS coalesce(a, b)',
      'nvl2':       'nvl2(a, b, c) AS CASE WHEN a IS NOT NULL THEN b ELSE c END',
      'div0':       'div0(a, b) AS CASE WHEN b = 0 THEN 0 ELSE a / b END',
      'div0null':   'div0null(a, b) AS CASE WHEN b = 0 OR b IS NULL THEN 0 ELSE a / b END',
      'zeroifnull': 'zeroifnull(a) AS coalesce(a, 0)',
      'nullifzero': 'nullifzero(a) AS nullif(a, 0)',
      'to_varchar': 'to_varchar(a) AS CAST(a AS VARCHAR)',
      'array_size': 'array_size(a) AS len(a)',
    } %}
    {% set names = "'" ~ (definitions.keys() | list | join("', '")) ~ "'" %}
    {% set existing = run_query(
         "select distinct lower(function_name) from duckdb_functions() where lower(function_name) in (" ~ names ~ ")"
       ).columns[0].values() %}
    {% set database = var('dbt_horizon_compat_database', target.database) %}
    {% for name, body in definitions.items() if name not in existing %}
      {% do run_query('create or replace macro "' ~ database ~ '".main.' ~ body) %}
    {% endfor %}
  {% endif %}
{% endmacro %}
