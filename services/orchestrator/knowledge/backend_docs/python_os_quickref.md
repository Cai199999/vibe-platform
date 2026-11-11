# Python os / pathlib 标准库速查

## 1. 路径与目录

优先使用 `pathlib.Path`，它比字符串拼接更安全，也天然跨平台：

```python
from pathlib import Path

root = Path("./data").resolve()
root.mkdir(parents=True, exist_ok=True)
target = root / "report.csv"
text = target.read_text(encoding="utf-8")
target.write_text("hello", encoding="utf-8")
```

- `Path.mkdir(parents=True, exist_ok=True)`：递归创建目录且允许已存在。
- `Path.glob("*.csv")` / `Path.rglob("*.json")`：当前目录 / 递归匹配。
- `Path.resolve()`：展开相对路径与符号链接；处理用户输入路径时必须配合
  `is_relative_to(base)` 做目录穿越校验。

## 2. 环境变量

```python
import os

database_url = os.environ.get("DATABASE_URL", "")
if not database_url:
    raise RuntimeError("DATABASE_URL 未配置")

# 只做本地开发兜底；生产密钥必须由部署平台注入。
debug = os.environ.get("APP_DEBUG", "false").lower() == "true"
```

- 读取密钥使用 `os.environ.get()`，禁止给真实密码写默认值。
- 修改环境变量只影响当前进程及其子进程：`os.environ["MODE"] = "test"`。
- 不要把 `os.environ` 整体序列化进日志，避免泄露密钥。

## 3. 进程与命令

```python
import subprocess

result = subprocess.run(
    ["python", "-m", "pytest", "-q"],
    cwd="/app",
    capture_output=True,
    text=True,
    timeout=120,
    check=False,
)
if result.returncode != 0:
    raise RuntimeError(result.stderr[-2000:])
```

- 参数必须用列表传递，禁止 `shell=True` 拼接用户输入，防止命令注入。
- 始终设置 `timeout`，并检查 `returncode`；只读取 `stdout` 会漏掉错误。
- 需要环境隔离时显式传入 `env={**os.environ, "APP_ENV": "test"}`。

## 4. 文件遍历与清理

```python
for path in sorted(Path("logs").glob("*.log")):
    if path.stat().st_size == 0:
        path.unlink(missing_ok=True)
```

- 删除前确认路径位于允许目录内，禁止对用户输入直接 `unlink`。
- 大文件读取使用流式：`with target.open("r", encoding="utf-8") as handle:`。
- 临时文件使用 `tempfile.TemporaryDirectory()`，退出时自动清理。

## 5. 常见错误

| 现象 | 原因 | 修复 |
| --- | --- | --- |
| `FileNotFoundError` | 父目录不存在 | `mkdir(parents=True, exist_ok=True)` |
| `PermissionError` | 容器非 root / 挂载只读 | 写入 `/tmp` 或修正 volume 权限 |
| `UnicodeDecodeError` | 未指定编码 | 显式 `encoding="utf-8"` |
| `FileExistsError` | 重复创建目录 | `exist_ok=True` |
