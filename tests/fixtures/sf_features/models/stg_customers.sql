{{ config(materialized='view') }}
-- iff( and flatten( in a comment are not code
select id, parent_id, 'key:value' as label
from {{ source('raw', 'customers') }}
