select id, parent_id
from {{ ref('stg_customers') }}
start with parent_id is null
connect by prior id = parent_id
