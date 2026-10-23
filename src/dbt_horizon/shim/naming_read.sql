{#- dbt-horizon shim, read mode: every model is built into the local DuckDB file
    (the Snowflake databases are attached read-only through Horizon). Schemas keep the
    project's own naming. A project generate_database_name is renamed to
    project_generate_database_name and not used in read mode. -#}
{% macro generate_database_name(custom_database_name=none, node=none) -%}
    {{ target.database }}
{%- endmacro %}
