{{ config(materialized='dynamic_table', target_lag='1 hour', snowflake_warehouse='WH',
          post_hook="grant select on {{ this }} to role REPORTER") }}
select status, count(*) as n, div0(sum(amount), count(*)) as avg_amount
from {{ ref('stg_orders') }}
group by status
