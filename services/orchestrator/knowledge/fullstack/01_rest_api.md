# RESTful API 设计规范

## 1. 资源与路径

- 使用名词复数：`/api/v1/orders`、`/api/v1/products`。
- 层级不超过两层：`/api/v1/orders/{orderId}/items`。
- 动作使用 HTTP Method 表达，不在路径中使用 `create` / `update` 动词。
- 写操作统一使用 JSON；时间使用 ISO-8601 UTC。

## 2. 方法语义

| Method | 语义 | 幂等 |
|---|---|---|
| `GET` | 查询资源 | 是 |
| `POST` | 创建资源 / 执行命令 | 否（需 Idempotency-Key） |
| `PUT` | 全量更新 | 是 |
| `PATCH` | 局部更新 | 否 |
| `DELETE` | 删除资源 | 是 |

## 3. 状态码

- `200` 查询 / 更新成功
- `201` 创建成功
- `202` 异步任务已接受
- `204` 删除成功且无响应体
- `400` 参数错误
- `401` 未认证
- `403` 无权限
- `404` 资源不存在
- `409` 状态冲突 / 重复提交
- `422` 业务校验失败
- `429` 限流
- `500` 服务内部错误

## 4. 统一响应信封

成功：

```json
{
  "data": {},
  "requestId": "req_..."
}
```

失败：

```json
{
  "code": "ORDER_STOCK_INSUFFICIENT",
  "message": "库存不足",
  "details": {
    "productId": "p_123",
    "available": 2,
    "requested": 5
  },
  "requestId": "req_..."
}
```

## 5. 分页与过滤

- 游标分页优先：`?cursor=...&limit=20`。
- 页码分页仅用于后台管理：`?page=1&pageSize=20`。
- 响应必须包含 `nextCursor` 或 `total`。
- 排序参数使用白名单，禁止把前端字段直接拼进 SQL。

## 6. 鉴权与租户

- 使用 `Authorization: Bearer <token>`。
- 所有业务表带 `tenant_id`。
- Controller 不得信任前端传入的 `tenantId`，必须从登录上下文注入。
- 资源级授权：资源不存在与无权限统一返回 `404`，避免枚举。

## 7. 幂等与异步

- 创建订单、扣库存、支付等写操作必须支持 `Idempotency-Key`。
- 耗时任务返回 `202` + `taskId`，通过 SSE / 轮询查询进度。
- 重复提交同一幂等键必须返回第一次的结果，不得重复扣减。

## 8. 契约要求

API 路由表至少包含：

| Method | Path | Request | Response | 说明 |
|---|---|---|---|---|
| POST | /api/v1/orders | CreateOrderRequest | OrderResponse | 创建订单并扣减库存 |

每个路由必须能对应到至少一个前端组件交互或脚本步骤。
