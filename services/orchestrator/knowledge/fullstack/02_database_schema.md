# 数据库 Schema 示例

## 1. 通用规范

- 主键使用 `BIGINT UNSIGNED` 或 `VARCHAR(64)`，按业务选择。
- 每张业务表包含：`id`、`tenant_id`、`created_at`、`updated_at`。
- 软删除使用 `deleted_at`，唯一索引需考虑删除态。
- 禁止物理外键；关联字段建立索引，由业务代码保证一致性。
- 金额使用 `DECIMAL(18,2)`，禁止浮点。
- 状态字段使用短字符串或 `TINYINT`，并在契约中列出枚举。

## 2. 用户与租户

```sql
CREATE TABLE tenants (
  id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(128) NOT NULL,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL
);

CREATE TABLE users (
  id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  tenant_id BIGINT UNSIGNED NOT NULL,
  email VARCHAR(255) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  display_name VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uk_users_tenant_email (tenant_id, email),
  KEY idx_users_tenant (tenant_id)
);
```

## 3. 商品与库存

```sql
CREATE TABLE products (
  id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  tenant_id BIGINT UNSIGNED NOT NULL,
  sku VARCHAR(64) NOT NULL,
  name VARCHAR(255) NOT NULL,
  price DECIMAL(18,2) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uk_products_tenant_sku (tenant_id, sku),
  KEY idx_products_tenant (tenant_id)
);

CREATE TABLE inventory (
  id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  tenant_id BIGINT UNSIGNED NOT NULL,
  product_id BIGINT UNSIGNED NOT NULL,
  available_quantity INT NOT NULL DEFAULT 0,
  reserved_quantity INT NOT NULL DEFAULT 0,
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uk_inventory_product (tenant_id, product_id),
  KEY idx_inventory_product (product_id)
);
```

## 4. 订单

```sql
CREATE TABLE orders (
  id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  tenant_id BIGINT UNSIGNED NOT NULL,
  order_no VARCHAR(64) NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
  total_amount DECIMAL(18,2) NOT NULL,
  idempotency_key VARCHAR(128) NULL,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uk_orders_tenant_order_no (tenant_id, order_no),
  UNIQUE KEY uk_orders_tenant_idem (tenant_id, idempotency_key),
  KEY idx_orders_tenant_user (tenant_id, user_id)
);

CREATE TABLE order_items (
  id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  tenant_id BIGINT UNSIGNED NOT NULL,
  order_id BIGINT UNSIGNED NOT NULL,
  product_id BIGINT UNSIGNED NOT NULL,
  quantity INT NOT NULL,
  unit_price DECIMAL(18,2) NOT NULL,
  created_at DATETIME(3) NOT NULL,
  KEY idx_order_items_order (tenant_id, order_id),
  KEY idx_order_items_product (product_id)
);
```

## 5. 库存扣减事务

创建订单时必须：

1. 按 `(tenant_id, product_id)` 锁定库存行；
2. 校验 `available_quantity >= quantity`；
3. 扣减 `available_quantity`，写入订单与订单项；
4. 任一步失败整体回滚；
5. 使用 `idempotency_key` 防止重复扣减。

## 6. 轻量 Web 数据模型

`lightweight_web` 不需要数据库 DDL。契约中应描述：

- LocalStorage / IndexedDB 的 key 与版本；
- 数据对象字段；
- 迁移策略（版本升级时如何兼容旧数据）；
- 清理 / 导出入口（如需要）。
