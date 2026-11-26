"""测试执行器。

当前实现是标准库 HTTP 级验收：预览 shell、登录、集合 CRUD 闭环。
它是 ``TestRunner`` 协议的最小可用实现；后续 Playwright / Verify 沙盒
只需实现同一协议，TestAgent 与报告结构不变。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol
from urllib.parse import urlparse

from ai_core.testing.models import CheckResult, TestCheck, TestRequest

_DEFAULT_USER = "admin"
_DEFAULT_PASSWORD = "admin123"


class TestRunner(Protocol):
    def run(self, check: TestCheck, request: TestRequest) -> CheckResult:
        """执行一条检查并返回结构化结果。"""


class HttpTestRunner:
    """基于 urllib 的 HTTP 验收执行器（无第三方依赖）。"""

    def __init__(self, *, poll_interval: float = 0.5) -> None:
        self.poll_interval = max(0.1, poll_interval)
        self._token = ""
        self._api_base = ""
        self._steps: list[dict[str, Any]] = []
        self._sandbox_containers: dict[int, Any] = {}

    # ---- 协议入口 -------------------------------------------------
    def run(self, check: TestCheck, request: TestRequest) -> CheckResult:
        started = time.monotonic()
        self._api_base = (request.api_base_url or request.preview_url).rstrip("/")
        self._steps = []
        try:
            if check.kind == "preview_shell":
                result = self._preview_shell(check, request)
            elif check.kind == "login":
                result = self._login(check, request)
            elif check.kind == "crud_flow":
                result = self._crud_flow(check, request)
            elif check.kind == "ui_flow":
                result = self._ui_flow(check, request)
            elif check.kind == "production_artifact":
                result = CheckResult(
                    check_id=check.check_id,
                    kind=check.kind,
                    status="SKIPPED",
                    message=(
                        "L3 生产仿真沙盒尚未接入：需要独立构建产物、正式迁移、"
                        "权限与故障验收。当前只记录计划，不伪造通过。"
                    ),
                )
            else:
                result = CheckResult(
                    check_id=check.check_id,
                    kind=check.kind,
                    status="ERROR",
                    message=f"未知检查类型: {check.kind}",
                    error_code="TEST_CHECK_UNSUPPORTED",
                )
        except Exception as exc:  # noqa: BLE001 - 单条检查失败不能中断报告
            result = CheckResult(
                check_id=check.check_id,
                kind=check.kind,
                status="ERROR",
                message=f"检查执行异常: {exc}",
                error_code="TEST_CHECK_EXCEPTION",
            )
        result = result.model_copy(
            update={
                "duration_ms": int((time.monotonic() - started) * 1000),
                "evidence": {**result.evidence, "steps": list(self._steps)},
            }
        )
        return result

    # ---- L1：预览 shell ------------------------------------------
    def _preview_shell(self, check: TestCheck, request: TestRequest) -> CheckResult:
        url = request.preview_url or check.target
        if not url:
            return CheckResult(
                check_id=check.check_id,
                kind=check.kind,
                status="ERROR",
                message="缺少 previewUrl，无法验收",
                error_code="TEST_PREVIEW_MISSING",
            )
        deadline = time.monotonic() + request.timeout_seconds
        last_status = 0
        last_error = ""
        body = ""
        while time.monotonic() < deadline:
            # Vite 对 Accept: application/json 的 HTML 请求会返回 404；
            # 预览必须按浏览器语义请求 text/html。
            status, body, error = self._request(
                "GET", url, headers={"Accept": "text/html"}
            )
            if status == 0:
                # python-ai 在容器内，沙盒端口只发布到宿主 127.0.0.1；
                # 直接不可达时改为在沙盒容器内 wget，与浏览器同源。
                sandbox_status, sandbox_body, sandbox_error = self._sandbox_preview(url)
                if sandbox_status:
                    status, body, error = (
                        sandbox_status,
                        sandbox_body,
                        sandbox_error,
                    )
            last_status = status
            last_error = error
            self._steps.append(
                {
                    "step": "preview",
                    "status": status,
                    "error": error,
                    "bytes": len(body),
                }
            )
            if status == 200 and 'id="root"' in body:
                return CheckResult(
                    check_id=check.check_id,
                    kind=check.kind,
                    status="PASSED",
                    message="预览返回 200 且包含 React 根节点",
                    evidence={"status": status, "bytes": len(body), "url": url},
                )
            time.sleep(self.poll_interval)
        message = "预览未在超时前就绪"
        if last_status == 200:
            message = "预览返回 200，但未找到 React 根节点"
        elif last_error:
            message = f"预览不可达: {last_error}"
        return CheckResult(
            check_id=check.check_id,
            kind=check.kind,
            status="FAILED",
            message=message,
            error_code=(
                "TEST_UI_SMOKE_FAILED"
                if last_status == 200
                else "TEST_PREVIEW_NOT_READY"
            ),
            evidence={"status": last_status, "url": url},
        )

    # ---- L2：登录 ------------------------------------------------
    def _login(self, check: TestCheck, request: TestRequest) -> CheckResult:
        path = check.target or "/api/v1/users/login"
        url = path if path.startswith("http") else f"{self._api_base}{path}"
        username = str(request.auth.get("username") or _DEFAULT_USER)
        password = str(request.auth.get("password") or _DEFAULT_PASSWORD)
        status, body, error = self._request(
            "POST",
            url,
            payload={"username": username, "password": password},
        )
        self._steps.append({"step": "login", "status": status, "error": error})
        payload = _json(body)
        token = _token_from(payload)
        if status >= 400 or not token:
            return CheckResult(
                check_id=check.check_id,
                kind=check.kind,
                status="FAILED",
                message=f"登录失败 status={status} {error or ''}".strip(),
                error_code="TEST_AUTH_FAILED",
                evidence={
                    "status": status,
                    "body": body[:300],
                    "username": username,
                },
            )
        self._token = token
        return CheckResult(
            check_id=check.check_id,
            kind=check.kind,
            status="PASSED",
            message="登录成功并获取 JWT",
            evidence={"status": status, "token": f"{token[:8]}…"},
        )

    # ---- L2：CRUD 业务闭环 --------------------------------------
    def _crud_flow(self, check: TestCheck, request: TestRequest) -> CheckResult:
        resource = check.target.rstrip("/")
        if not resource:
            return CheckResult(
                check_id=check.check_id,
                kind=check.kind,
                status="ERROR",
                message="契约未声明集合根路径",
                error_code="TEST_CONTRACT_INCOMPLETE",
            )
        base = f"{self._api_base}{resource}"
        sample = dict(check.metadata.get("samplePayload") or {})
        if not sample:
            return CheckResult(
                check_id=check.check_id,
                kind=check.kind,
                status="FAILED",
                message="无法从契约 DDL 推导请求体，CRUD 验收无法执行",
                error_code="TEST_CONTRACT_INCOMPLETE",
            )
        headers = self._auth_headers()

        status, body, error = self._request("GET", base, headers=headers)
        self._steps.append({"step": "list", "status": status, "error": error})
        if status >= 400:
            return self._fail(
                check, "集合查询失败", "TEST_API_FLOW_FAILED", status, body, error
            )

        marker = _marker(sample)
        status, body, error = self._request(
            "POST", base, payload=sample, headers=headers
        )
        self._steps.append({"step": "create", "status": status, "error": error})
        if status >= 300:
            return self._fail(
                check, "创建记录失败", "TEST_API_FLOW_FAILED", status, body, error
            )

        payload = _json(body)
        record_id = _record_id(payload)
        if not record_id:
            status2, body2, error2 = self._request("GET", base, headers=headers)
            self._steps.append({"step": "locate", "status": status2, "error": error2})
            if not _contains_marker(body2, marker):
                return self._fail(
                    check,
                    "创建后无法在集合中找到测试数据",
                    "TEST_CRUD_VERIFY_FAILED",
                    status2,
                    body2,
                    error2,
                )
        else:
            detail_url = f"{base}/{urllib.parse.quote(str(record_id))}"
            status2, body2, error2 = self._request("GET", detail_url, headers=headers)
            self._steps.append({"step": "detail", "status": status2, "error": error2})
            if status2 >= 400 or not _contains_marker(body2, marker):
                return self._fail(
                    check,
                    "创建后详情查询不到测试数据",
                    "TEST_CRUD_VERIFY_FAILED",
                    status2,
                    body2,
                    error2,
                )
            update_field, update_value = _update_pair(sample)
            if update_field:
                status3, body3, error3 = self._request(
                    "PATCH",
                    detail_url,
                    payload={update_field: update_value},
                    headers=headers,
                )
                self._steps.append(
                    {"step": "update", "status": status3, "error": error3}
                )
                if status3 >= 400:
                    return self._fail(
                        check,
                        "更新记录失败",
                        "TEST_CRUD_VERIFY_FAILED",
                        status3,
                        body3,
                        error3,
                    )
                status5, body5, error5 = self._request(
                    "GET", detail_url, headers=headers
                )
                self._steps.append(
                    {"step": "verify_update", "status": status5, "error": error5}
                )
                if status5 >= 400 or not _contains_marker(body5, str(update_value)):
                    return self._fail(
                        check,
                        "更新后详情未返回新值",
                        "TEST_CRUD_VERIFY_FAILED",
                        status5,
                        body5,
                        error5,
                    )
            status4, body4, error4 = self._request(
                "DELETE", detail_url, headers=headers
            )
            self._steps.append({"step": "delete", "status": status4, "error": error4})
            if status4 >= 400:
                return self._fail(
                    check,
                    "删除记录失败",
                    "TEST_CRUD_VERIFY_FAILED",
                    status4,
                    body4,
                    error4,
                )

        return CheckResult(
            check_id=check.check_id,
            kind=check.kind,
            status="PASSED",
            message=f"{resource} 创建→查询→更新→删除闭环通过",
            evidence={"resource": resource, "recordId": record_id},
        )

    # ---- 沙盒内回退 ---------------------------------------------
    def _sandbox_http(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, str, str] | None:
        """在沙盒容器内用 Node fetch 发请求；无法回退时返回 None。"""
        parsed = urlparse(url)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
            return None
        container = self._sandbox_container(parsed.port)
        if container is None:
            return None
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        internal_url = f"http://127.0.0.1:5173{path}"
        request_spec = json.dumps(
            {
                "method": method,
                "url": internal_url,
                "body": json.dumps(payload) if payload is not None else "",
                "headers": headers,
                "timeoutMs": int(timeout * 1000),
            }
        )
        try:
            result = container.exec_run(
                ["node", "-e", _SANDBOX_HTTP_SCRIPT],
                environment={"VIBE_TEST_REQUEST": request_spec},
            )
            output = result.output
            text = (
                output.decode("utf-8", errors="replace")
                if isinstance(output, bytes)
                else str(output or "")
            )
            payload_json = _last_json_line(text)
            if not payload_json:
                return 0, text, f"沙盒内 Node 请求失败: {text[:200]}"
            return (
                int(payload_json.get("status") or 0),
                str(payload_json.get("body") or ""),
                str(payload_json.get("error") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 回退失败按不可达处理
            return 0, "", f"沙盒内请求失败: {exc}"

    def _sandbox_preview(self, url: str) -> tuple[int, str, str]:
        """在沙盒容器内访问 127.0.0.1:5173，解决容器/宿主端口隔离。"""
        parsed = urlparse(url)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
            return 0, "", ""
        container = self._sandbox_container(parsed.port)
        if container is None:
            return 0, "", "未找到对应的沙盒容器"
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        try:
            result = container.exec_run(
                ["wget", "-qO-", "-T", "5", f"http://127.0.0.1:5173{path}"]
            )
            output = result.output
            if isinstance(output, bytes):
                body = output.decode("utf-8", errors="replace")
            else:
                body = str(output or "")
            code = int(result.exit_code)
            if code == 0:
                return 200, body, ""
            return 0, body, f"沙盒内 wget 退出码 {code}"
        except Exception as exc:  # noqa: BLE001 - 回退失败按不可达处理
            return 0, "", f"沙盒内访问失败: {exc}"

    def _sandbox_container(self, port: int) -> Any | None:
        if port in self._sandbox_containers:
            return self._sandbox_containers[port]
        try:
            import docker  # noqa: PLC0415 - 仅部署环境需要

            client = docker.from_env()
            containers = client.containers.list(filters={"label": "vibe.managed=true"})
        except Exception:  # noqa: BLE001 - 无 Docker 时直接失败
            return None
        for container in containers:
            ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
            for binding in ports.get("5173/tcp") or []:
                try:
                    host_port = int(binding.get("HostPort") or 0)
                except (TypeError, ValueError):
                    continue
                if host_port == port:
                    self._sandbox_containers[port] = container
                    return container
        return None

    # ---- L2：Playwright UI 断言 ---------------------------------
    def _ui_flow(self, check: TestCheck, request: TestRequest) -> CheckResult:
        runner_url = str(check.metadata.get("runnerUrl") or "").rstrip("/")
        if not runner_url:
            return CheckResult(
                check_id=check.check_id,
                kind=check.kind,
                status="SKIPPED",
                message="UI runner 未配置（TEST_UI_RUNNER_URL）",
            )
        payload = {
            "previewUrl": request.preview_url,
            "path": check.metadata.get("path") or check.target or "/",
            "username": request.auth.get("username") or _DEFAULT_USER,
            "password": request.auth.get("password") or _DEFAULT_PASSWORD,
            "timeoutMs": int(request.timeout_seconds * 1000),
        }
        status, body, error = self._request(
            "POST", f"{runner_url}/run", payload=payload
        )
        self._steps.append({"step": "ui_flow", "status": status, "error": error})
        data = _json(body)
        if status >= 400 or not isinstance(data, dict):
            return CheckResult(
                check_id=check.check_id,
                kind=check.kind,
                status="FAILED",
                message=f"UI runner 调用失败 status={status} {error or ''}".strip(),
                error_code="UI_RUNNER_UNAVAILABLE",
                evidence={"status": status, "body": body[:400]},
            )
        ui_status = str(data.get("status") or "FAILED").upper()
        if ui_status == "PASSED":
            return CheckResult(
                check_id=check.check_id,
                kind=check.kind,
                status="PASSED",
                message=str(data.get("message") or "UI 表单提交、刷新持久化通过"),
                evidence=dict(data.get("evidence") or {}),
            )
        return CheckResult(
            check_id=check.check_id,
            kind=check.kind,
            status="FAILED",
            message=str(data.get("message") or "UI 断言失败"),
            error_code=str(data.get("errorCode") or "UI_ASSERTION_FAILED"),
            evidence=dict(data.get("evidence") or {}),
        )

    # ---- 工具 ----------------------------------------------------
    def _fail(
        self,
        check: TestCheck,
        message: str,
        code: str,
        status: int,
        body: str,
        error: str,
    ) -> CheckResult:
        return CheckResult(
            check_id=check.check_id,
            kind=check.kind,
            status="FAILED",
            message=message,
            error_code=code,
            evidence={
                "status": status,
                "error": error,
                "body": body[:500],
            },
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, str, str]:
        data = None
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, body, ""
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, body, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - 网络错误作为检查失败
            sandbox = self._sandbox_http(
                method,
                url,
                payload=payload,
                headers=request_headers,
                timeout=timeout,
            )
            if sandbox is not None:
                return sandbox
            return 0, "", str(exc)


def _json(body: str) -> Any:
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return None


def _token_from(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    token = payload.get("token") or payload.get("accessToken")
    if not token and isinstance(payload.get("data"), dict):
        token = payload["data"].get("token") or payload["data"].get("accessToken")
    return str(token or "")


def _record_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("id") is not None:
        return str(payload["id"])
    data = payload.get("data")
    if isinstance(data, dict) and data.get("id") is not None:
        return str(data["id"])
    if isinstance(data, dict) and data.get("record") is not None:
        return _record_id(data["record"])
    if isinstance(data, list) and data:
        return _record_id(data[0])
    return ""


def _contains_marker(body: str, marker: str) -> bool:
    """HTTP 响应可能把中文转义成 \\uXXXX；先按 JSON 解码再比较。"""
    if not marker:
        return True
    if marker in body:
        return True
    payload = _json(body)
    if payload is None:
        return False
    try:
        return marker in json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


def _marker(sample: dict[str, Any]) -> str:
    for key in ("name", "title", "subject", "code", "sku"):
        value = sample.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in sample.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _update_pair(sample: dict[str, Any]) -> tuple[str, Any]:
    for key in ("status", "state", "name", "title"):
        if key in sample:
            value = sample[key]
            if isinstance(value, str):
                return key, f"{value}-updated"
            if isinstance(value, bool):
                return key, not value
            if isinstance(value, (int, float)):
                return key, value + 1
    # 没有通用状态字段时，挑第一个可写字段，保证“更新”步骤真实执行。
    for key, value in sample.items():
        lowered = key.lower()
        if lowered == "id" or lowered.endswith("_id"):
            continue
        if isinstance(value, str):
            return key, f"{value}-updated"
        if isinstance(value, bool):
            return key, not value
        if isinstance(value, (int, float)):
            return key, value + 1
    return "", None


_SANDBOX_HTTP_SCRIPT = """
const spec = JSON.parse(process.env.VIBE_TEST_REQUEST);
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), spec.timeoutMs || 15000);
fetch(spec.url, {
  method: spec.method,
  headers: spec.headers || {},
  body: spec.body || undefined,
  signal: controller.signal,
})
  .then(async (response) => {
    const text = await response.text();
    console.log(JSON.stringify({ status: response.status, body: text }));
  })
  .catch((error) => {
    console.log(JSON.stringify({ status: 0, body: "", error: String(error) }));
  })
  .finally(() => clearTimeout(timer));
""".strip()


def _last_json_line(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict) and "status" in payload:
            return payload
    return None


__all__ = ["HttpTestRunner", "TestRunner"]
