{{ config(materialized='dynamic_table', target_lag='1 hour', snowflake_warehouse='WH') }}
select count(*) as n from {{ ref('orders_flat') }}
