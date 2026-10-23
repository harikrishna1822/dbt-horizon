select o.order_id, f.value:sku::string as sku
from {{ ref('stg_orders') }} o, lateral flatten(input => o.items) f
