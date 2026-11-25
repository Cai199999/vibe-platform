#!/usr/bin/env python3
"""多服务 compose 沙盒 E2E：真实构建、启动、迁移、消费、幂等与交付打包。

不接真实 LLM；验证生成物能在多容器拓扑中真实运行。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from ai_core.prototype.delivery import package_delivery  # noqa: E402
from ai_core.prototype.generator import generate_workspace  # noqa: E402
from ai_core.prototype.planner import plan_topology  # noqa: E402
from ai_core.prototype.spec import default_ticket_spec  # noqa: E402

API = "http://127.0.0.1:18080"
WEB = "http://127.0.0.1:15173"
AGENT = "http://127.0.0.1:18001"
ENV = os.environ.copy()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(
    cmd: list[str], *, check: bool = True, timeout: int = 1200
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
        env=ENV,
    )


def wait_http(url: str, *, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(f"等待 {url} 超时: {last}")


def post_ticket() -> dict:
    payload = json.dumps(
        {
            "id": "t_compose",
            "title": "打印机无法连接",
            "content": "办公室打印机无法连接网络",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{API}/api/tickets",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def db_state(compose: list[str]) -> dict:
    code = (
        "import json,sqlite3;"
        "c=sqlite3.connect('/app/data/app.db');"
        "print(json.dumps({"
        "'tickets':c.execute('select count(*) from tickets').fetchone()[0],"
        "'processed':c.execute('select count(*) from processed_events').fetchone()[0],"
        "'category':(c.execute(\"select ai_category from tickets where id='t_compose'\").fetchone() or [None])[0]"
        "}))"
    )
    result = run(compose + ["exec", "-T", "consumer", "python", "-c", code])
    return json.loads(result.stdout.strip())


def wait_log(
    compose: list[str],
    service: str,
    needle: str,
    *,
    timeout: float = 45.0,
) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        result = run(compose + ["logs", service], check=False)
        last = result.stdout + result.stderr
        if needle in last:
            return last
        time.sleep(3)
    raise RuntimeError(f"等待 {service} 日志包含 {needle} 超时: {last[-800:]}")


def wait_row(compose: list[str], *, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = db_state(compose)
        if last.get("category"):
            return last
        time.sleep(2)
    raise RuntimeError(f"等待工单 AI 字段超时: {last}")


def main() -> int:
    global API, WEB, AGENT
    ports = {
        "API_PORT": free_port(),
        "WEB_PORT": free_port(),
        "AGENT_PORT": free_port(),
        "OTEL_PORT": free_port(),
        "KAFKA_PORT": free_port(),
    }
    ENV.update({key: str(value) for key, value in ports.items()})
    API = f"http://127.0.0.1:{ports['API_PORT']}"
    WEB = f"http://127.0.0.1:{ports['WEB_PORT']}"
    AGENT = f"http://127.0.0.1:{ports['AGENT_PORT']}"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ROOT / "reports" / "compose-e2e" / f"{stamp}-{uuid.uuid4().hex[:6]}"
    workspace = run_dir / "workspace"
    spec = default_ticket_spec()
    topology = plan_topology(spec)
    generate_workspace(workspace, spec, topology)
    project = f"vibeproto-{uuid.uuid4().hex[:8]}"
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(workspace / "infra" / "docker-compose.yml"),
        "--project-directory",
        str(workspace),
    ]
    report: dict = {
        "project": project,
        "runDir": str(run_dir),
        "stages": [],
        "ok": False,
    }
    try:
        up = run(compose + ["up", "-d", "--build"], check=False, timeout=1800)
        if up.returncode != 0:
            raise RuntimeError(f"compose up 失败:\n{up.stderr[-2000:]}")
        report["stages"].append({"name": "compose_up", "ok": True})
        wait_http(f"{API}/health")
        wait_http(f"{WEB}/health")
        wait_http(f"{AGENT}/health")
        report["stages"].append({"name": "health", "ok": True})

        created = post_ticket()
        event_id = created["event"]["eventId"]
        state = wait_row(compose)
        report["stages"].append(
            {"name": "consume", "ok": True, "eventId": event_id, "state": state}
        )

        before = db_state(compose)
        duplicate_code = (
            "import json;from kafka import KafkaProducer;"
            "producer=KafkaProducer(bootstrap_servers='redpanda:9092');"
            f"event={{'eventId':'{event_id}','ticketId':'t_compose',"
            "'title':'打印机无法连接','content':'办公室打印机无法连接网络'};"
            "producer.send('ticket.created', json.dumps(event).encode());"
            "producer.flush();print('duplicate-sent')"
        )
        run(compose + ["exec", "-T", "consumer", "python", "-c", duplicate_code])
        consumer_logs = wait_log(compose, "consumer", '"duplicate": true')
        if '"duplicate": true' not in consumer_logs:
            raise RuntimeError("重复事件未被幂等处理")
        after = db_state(compose)
        if (
            after["processed"] != before["processed"]
            or after["tickets"] != before["tickets"]
        ):
            raise RuntimeError(f"重复事件写回: before={before} after={after}")
        report["stages"].append(
            {
                "name": "idempotency",
                "ok": True,
                "before": before,
                "after": after,
            }
        )

        otel_logs = wait_log(compose, "otel", "ticket-consumer")
        if "consume_event" not in otel_logs:
            raise RuntimeError("Collector 未收到 consume_event span")
        api_logs = wait_log(compose, "otel", "ticket-api")
        if "POST" not in api_logs and "/api/tickets" not in api_logs:
            raise RuntimeError("Collector 未收到 API 请求 span")
        report["stages"].append(
            {
                "name": "otel_spans",
                "ok": True,
                "needles": [
                    "ticket-consumer",
                    "consume_event",
                    "ticket-api",
                ],
            }
        )

        delivery = package_delivery(workspace, spec)
        report["delivery"] = delivery
        report["stages"].append({"name": "delivery", "ok": delivery["verify"]["ok"]})
        report["ok"] = True
    finally:
        run(compose + ["down", "-v", "--remove-orphans"], check=False)
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        (run_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"[compose-e2e] project={project} ok={report['ok']}")
    print(f"[compose-e2e] stages={[item['name'] for item in report['stages']]}")
    print(f"[compose-e2e] report={run_dir / 'report.json'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
