# pandas 数据处理快速参考

## 1. 读取与写出

```python
import pandas as pd

df = pd.read_csv("input.csv", encoding="utf-8", dtype=str)
df.to_csv("output.csv", index=False, encoding="utf-8-sig")

# Excel 需要 openpyxl；大文件优先 CSV / Parquet。
sheets = pd.read_excel("input.xlsx", sheet_name=None)
```

- 读取时显式指定 `dtype` / `parse_dates`，避免 pandas 推断出错误类型。
- 导出 CSV 供 Excel 打开时使用 `utf-8-sig`，防止中文乱码。
- 只读取需要的列：`usecols=["id", "name", "created_at"]`。

## 2. 缺失值与类型

```python
df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")

missing = df.isna().sum()
df = df.dropna(subset=["id", "amount"])
df["amount"] = df["amount"].fillna(0)
```

- 金额 / 数量禁止用 `astype(float)` 直接强转，先 `to_numeric(errors="coerce")`。
- 时间统一为 UTC 后再入库：`pd.to_datetime(..., utc=True)`。
- 修改 DataFrame 时使用 `.loc` 明确行列，避免 `SettingWithCopyWarning`。

## 3. 去重与分组

```python
df = df.drop_duplicates(subset=["external_id"], keep="last")

summary = (
    df.groupby(["tenant_id", "status"], dropna=False)
      .agg(count=("id", "size"), amount=("amount", "sum"))
      .reset_index()
)
```

- 去重键必须是业务唯一键；不要用整行 `drop_duplicates()` 猜业务语义。
- `groupby` 默认丢弃 NaN 分组，统计口径需要保留时设置 `dropna=False`。
- 聚合结果显式 `reset_index()`，避免后续写出时索引变成多余列。

## 4. 合并

```python
merged = orders.merge(
    customers[["id", "name"]],
    left_on="customer_id",
    right_on="id",
    how="left",
    validate="many_to_one",
)
```

- 必须声明 `how`；默认 inner join 静默丢行是常见事故。
- 使用 `validate=` 校验基数，避免一对多导致行数膨胀。
- 合并后检查行数变化：`len(merged) >= len(orders)` 只有在左连接时才成立。

## 5. 性能与内存

- 只选必要列，尽早过滤：`df = df[df["status"] == "active"]`。
- 低基数字符串列转 `category`，可显著降低内存。
- 大文件分块读取：
  ```python
  for chunk in pd.read_csv("big.csv", chunksize=100_000):
      process(chunk)
  ```
- 避免 `iterrows()`；优先向量化、`map`、`apply` 仅用于复杂逐行逻辑。
- 中间结果落 Parquet 而不是反复 CSV 往返。

## 6. 质量报告

```python
report = {
    "rows": int(len(df)),
    "columns": int(len(df.columns)),
    "duplicates": int(df.duplicated(subset=["external_id"]).sum()),
    "missing": {key: int(value) for key, value in df.isna().sum().items()},
}
```

- 每个 ETL 任务输出行数、去重数、缺失值与异常值统计。
- 清洗规则必须可复现：固定编码、时区、排序与随机种子。
