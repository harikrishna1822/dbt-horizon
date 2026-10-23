{#- dbt-horizon shim (generated into the workspace, never into the customer repo).
    dbt-duckdb's run_hooks, plus: hooks that are Snowflake administration statements
    (GRANT, USE ROLE, ALTER WAREHOUSE, ...) are skipped. The pattern is
    dbt_horizon.rules.hooks.SNOWFLAKE_STATEMENT, written in when the shim is generated. -#}
{% macro run_hooks(hooks, inside_transaction=True) %}
  {% set snowflake_statement %}__SNOWFLAKE_STATEMENT__{% endset %}
  {% for hook in hooks | selectattr('transaction', 'equalto', inside_transaction)  %}
    {% set rendered = render(hook.get('sql')) | trim %}
    {% if (rendered | length) > 0 and not modules.re.search(snowflake_statement, rendered, modules.re.I) %}
      {% call statement(auto_begin=inside_transaction) %}
        {{ rendered }}
      {% endcall %}
    {% endif %}
  {% endfor %}
{% endmacro %}
