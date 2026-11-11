# Python requests 快速参考

## 1. 基本请求与超时

```python
import requests

response = requests.get(
    "https://api.example.com/v1/items",
    params={"page": 1, "size": 20},
    headers={"Accept": "application/json"},
    timeout=(3.05, 15),  # (连接超时, 读取超时)
)
response.raise_for_status()
payload = response.json()
```

- 必须设置 `timeout`，否则故障下游会把线程永久挂住。
- 连接超时与读取超时分开配置；批量任务建议 3s / 15s。
- `raise_for_status()` 只处理 4xx / 5xx，网络异常仍需捕获
  `requests.Timeout`、`requests.ConnectionError`。

## 2. 重试与退避

```python
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

session = requests.Session()
retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
)
session.mount("https://", HTTPAdapter(max_retries=retry))
```

- 只对幂等方法自动重试；POST 重试必须由业务层保证幂等键。
- 尊重 `Retry-After` 头，避免在限流时继续打满下游。

## 3. POST JSON 与鉴权

```python
response = session.post(
    "https://api.example.com/v1/items",
    json={"name": "demo"},
    headers={"Authorization": f"Bearer {token}"},
    timeout=(3.05, 15),
)
```

- 使用 `json=` 而不是手动 `data=json.dumps(...)`，自动设置 Content-Type。
- Token 只从环境变量 / 密钥管理系统读取，禁止写入源码或日志。
- 重定向可能把 Authorization 带到其它域；敏感请求设置
  `allow_redirects=False` 或校验最终 URL。

## 4. 流式下载与大文件

```python
with session.get(url, stream=True, timeout=(3.05, 60)) as response:
    response.raise_for_status()
    with open(target, "wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
```

- 大文件禁止直接 `response.content`，避免内存翻倍。
- 下载前校验 `Content-Length` 与目标目录剩余空间。
- 解压压缩包前限制解压后总大小与文件数量，防止 zip bomb。

## 5. 错误处理模板

```python
try:
    response = session.get(url, timeout=(3.05, 15))
    response.raise_for_status()
except requests.Timeout as exc:
    raise RuntimeError(f"请求超时: {url}") from exc
except requests.HTTPError as exc:
    raise RuntimeError(
        f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
    ) from exc
except requests.RequestException as exc:
    raise RuntimeError(f"网络错误: {exc}") from exc
```

- 日志中保留方法、URL、状态码、耗时与 request-id，不打印完整响应体。
- 对第三方接口做熔断 / 限速；批量并发使用 `Session` 复用连接。
