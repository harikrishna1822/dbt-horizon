{{ config(materialized='incremental', unique_key='day', cluster_by=['day'],
          post_hook="grant select on {{ this }} to role REPORTER") }}
select date_trunc('day', ts) as day, count(*) as n
from {{ source('events', 'clicks') }}
group by 1
