{{ config(materialized='view') }}
select order_id, iff(amount > 0, amount, 0) as amount, nvl(status, 'none') as status, items
from {{ source('raw', 'orders') }}
