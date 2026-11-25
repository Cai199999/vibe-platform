"""多产物生成：把拓扑落成可运行的骨架与交付物。"""

from __future__ import annotations

import json
from pathlib import Path

from ai_core.prototype.planner import Topology
from ai_core.prototype.spec import ApplicationSpec


def generate_workspace(
    root: Path,
    spec: ApplicationSpec,
    topology: Topology,
) -> list[str]:
    root.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {
        "README.md": _readme(spec),
        "requirements.txt": _requirements_txt(),
        "observability/__init__.py": '"""生成应用的观测辅助。"""\n',
        "observability/otel.py": _otel_py(),
        "apps/web/index.html": _index_html(spec),
        "apps/web/server.mjs": _web_server(),
        "apps/api/package.json": _api_package_json(),
        "apps/api/tracing.mjs": _api_tracing(),
        "apps/api/server.mjs": _api_server(spec),
        "services/agent/agent.py": _agent_py(),
        "services/consumer/consumer.py": _consumer_py(),
        "infra/docker-compose.yml": _compose(spec),
        "infra/Dockerfile.api": _dockerfile_node(),
        "infra/Dockerfile.agent": _dockerfile_python(),
        "infra/Dockerfile.web": _dockerfile_web(),
        "db/migrate.py": _migrate_py(),
        "db/migrations/001_init.sql": _migration_001(),
        "db/migrations/002_ai_fields.sql": _migration_002(),
        "events/ticket.created.json": _sample_event(),
        f"helm/{spec.name}/Chart.yaml": _chart_yaml(spec),
        f"helm/{spec.name}/values.yaml": _values_yaml(spec),
        f"helm/{spec.name}/templates/deployment.yaml": _deployment_yaml(spec),
        f"helm/{spec.name}/templates/service.yaml": _service_yaml(spec),
        f"helm/{spec.name}/templates/migration-job.yaml": _migration_job_yaml(spec),
        f"helm/{spec.name}/templates/configmap.yaml": _configmap_yaml(spec),
        f"helm/{spec.name}/templates/pvc.yaml": _pvc_yaml(spec),
        f"helm/{spec.name}/templates/otel-collector.yaml": _otel_k8s_yaml(spec),
        "observability/otel-collector.yaml": _otel_yaml(),
        "tests/smoke_test.py": _smoke_test(),
        "contract/topology.json": json.dumps(
            topology.to_dict(), ensure_ascii=False, indent=2
        )
        + "\n",
        "contract/contract.json": json.dumps(
            spec.to_dict(), ensure_ascii=False, indent=2
        )
        + "\n",
    }
    written: list[str] = []
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(relative)
    return written


def _readme(spec: ApplicationSpec) -> str:
    return f"""# {spec.name}

{spec.goal}

## 本地运行

```bash
python db/migrate.py
python services/consumer/consumer.py --db data/app.db --event events/ticket.created.json --agent services/agent/agent.py
```

## 结构

- `apps/web`：页面
- `apps/api`：业务接口
- `services/consumer`：事件消费者（幂等）
- `services/agent`：Agent 服务
- `db/migrations`：表结构演进
- `helm/`：交付清单
"""


def _index_html(spec: ApplicationSpec) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8" /><title>{spec.name}</title></head>
  <body>
    <main>
      <h1>{spec.name}</h1>
      <p>{spec.goal}</p>
      <section id="tickets"></section>
    </main>
  </body>
</html>
"""


def _api_server(spec: ApplicationSpec) -> str:
    return """import './tracing.mjs'
import { trace } from '@opentelemetry/api'
import crypto from 'node:crypto'
import { Kafka } from 'kafkajs'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'

const port = Number(process.env.PORT || 8080)
const eventsDir = process.env.EVENTS_DIR || '/app/events'
const tickets = []
const kafkaBrokers = (process.env.KAFKA_BROKERS || '').trim()
const kafkaTopic = process.env.KAFKA_TOPIC || 'ticket.created'
const producer = kafkaBrokers
  ? new Kafka({ clientId: 'ticket-api', brokers: kafkaBrokers.split(',') }).producer()
  : null
let connectPromise = null
let kafkaReady = false

async function ensureProducer() {
  if (!producer) return
  if (!connectPromise) {
    connectPromise = producer
      .connect()
      .then(() => { kafkaReady = true })
      .catch((error) => {
        connectPromise = null
        kafkaReady = false
        throw error
      })
  }
  await connectPromise
}

async function publish(ticket) {
  const event = {
    eventId: `evt_${crypto.randomUUID()}`,
    ticketId: ticket.id,
    title: ticket.title || '',
    content: ticket.content || '',
    createdAt: new Date().toISOString(),
  }
  if (producer) {
    await ensureProducer()
    await producer.send({
      topic: kafkaTopic,
      messages: [{ key: event.eventId, value: JSON.stringify(event) }],
    })
    return event
  }
  fs.mkdirSync(eventsDir, { recursive: true })
  fs.writeFileSync(
    path.join(eventsDir, `${event.eventId}.json`),
    JSON.stringify(event),
  )
  return event
}

const tracer = trace.getTracer('ticket-api')

const server = http.createServer((req, res) => {
  tracer.startActiveSpan(
    `${req.method} ${req.url}`,
    (span) => {
      span.setAttribute('http.method', req.method)
      span.setAttribute('http.target', req.url)
      res.on('finish', () => {
        span.setAttribute('http.status_code', res.statusCode)
        span.end()
      })
      handle(req, res)
    },
  )
})

function handle(req, res) {
  if (req.url === '/health') {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ status: 'UP', kafka: producer ? kafkaReady : 'file' }))
    return
  }
  if (req.url === '/api/tickets' && req.method === 'GET') {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify(tickets))
    return
  }
  if (req.url === '/api/tickets' && req.method === 'POST') {
    let body = ''
    req.on('data', (chunk) => { body += chunk })
    req.on('end', async () => {
      try {
      const ticket = JSON.parse(body || '{}')
      ticket.id = ticket.id || `t_${Date.now()}`
      tickets.push(ticket)
      const event = await publish(ticket)
      res.writeHead(202, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ ticket, event }))
      } catch (error) {
        res.writeHead(503, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ error: 'event publish failed', detail: String(error) }))
      }
    })
    return
  }
  res.writeHead(404)
  res.end('not found')
}

server.listen(port, () => {
  console.log(`api listening on ${port}`)
})
"""


def _agent_py() -> str:
    return '''#!/usr/bin/env python3
"""确定性 Agent 原型：根据关键词给出分类与建议。"""

from __future__ import annotations

import argparse
import json
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from observability.otel import setup_tracing

TRACER = None


def _span(name: str):
    return TRACER.start_as_current_span(name) if TRACER else nullcontext()


RULES = (
    (("退款", "报销", "发票"), "财务", "转财务复核并补充凭证"),
    (("打印机", "网络", "登录", "账号"), "IT", "转 IT 支持并附设备信息"),
    (("投诉", "态度"), "客诉", "转客服主管并优先回访"),
)


def classify(text: str) -> dict:
    for keywords, category, suggestion in RULES:
        if any(word in text for word in keywords):
            return {
                "category": category,
                "confidence": 0.88,
                "suggestion": suggestion,
            }
    return {
        "category": "其他",
        "confidence": 0.55,
        "suggestion": "补充信息后由人工分派",
    }


def serve(port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                payload = {"status": "UP"}
            else:
                text = parse_qs(parsed.query).get("text", [""])[0]
                with _span("classify"):
                    payload = classify(text)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if args.serve:
        global TRACER
        TRACER = setup_tracing("ticket-agent")
        serve(args.port)
        return 0
    if not args.event:
        parser.error("单次模式需要 --event")
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    text = f"{event.get('title', '')} {event.get('content', '')}"
    with _span("classify"):
        result = classify(text)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _consumer_py() -> str:
    return '''#!/usr/bin/env python3
"""事件消费者原型：幂等消费 → 调 Agent → 写回业务表。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from observability.otel import setup_tracing

TRACER = None


def _span(name: str):
    return TRACER.start_as_current_span(name) if TRACER else nullcontext()


def process_event(db: str, event_path: Path, agent: str) -> dict:
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event_id = str(event.get("eventId") or "")
    if not event_id:
        return {"error": "missing eventId"}

    conn = sqlite3.connect(db)
    exists = conn.execute(
        "SELECT 1 FROM processed_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if exists:
        conn.close()
        return {"duplicate": True, "eventId": event_id}

    proc = subprocess.run(
        [sys.executable, agent, "--event", str(event_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        conn.close()
        return {"error": "agent failed", "stderr": proc.stderr[:400]}
    result = json.loads(proc.stdout)
    now = datetime.now(timezone.utc).isoformat()
    ticket_id = str(event.get("ticketId") or "")

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO tickets (id, title, status, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title",
            (ticket_id, str(event.get("title") or ""), "new", now),
        )
        conn.execute(
            "UPDATE tickets SET ai_category = ?, ai_confidence = ?, ai_suggestion = ? "
            "WHERE id = ?",
            (
                result.get("category"),
                float(result.get("confidence") or 0),
                result.get("suggestion"),
                ticket_id,
            ),
        )
        conn.execute(
            "INSERT INTO processed_events (event_id, processed_at) VALUES (?, ?)",
            (event_id, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "duplicate": False,
        "eventId": event_id,
        "ticketId": ticket_id,
        "ai": result,
    }


def consume_kafka(
    brokers: str,
    topic: str,
    group: str,
    db: str,
    agent: str,
) -> None:
    from kafka import KafkaConsumer

    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=[item.strip() for item in brokers.split(",") if item.strip()],
        group_id=group,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
    )
    for message in consumer:
        event = message.value
        event_id = str(event.get("eventId") or "event")
        path = Path("/tmp") / f"kafka-{event_id}.json"
        path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        with _span("consume_event"):
            result = process_event(db, path, agent)
        print(json.dumps(result, ensure_ascii=False), flush=True)


def main() -> int:
    global TRACER
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/app/data/app.db")
    parser.add_argument("--event")
    parser.add_argument("--agent", default="services/agent/agent.py")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--event-dir", default="/app/events")
    parser.add_argument("--kafka-brokers", default="")
    parser.add_argument("--topic", default="ticket.created")
    parser.add_argument("--group", default="ticket-consumer")
    args = parser.parse_args()

    if args.kafka_brokers:
        TRACER = setup_tracing("ticket-consumer")
        consume_kafka(
            args.kafka_brokers,
            args.topic,
            args.group,
            args.db,
            args.agent,
        )
        return 0

    if args.watch:
        TRACER = setup_tracing("ticket-consumer")
        event_dir = Path(args.event_dir)
        event_dir.mkdir(parents=True, exist_ok=True)
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        while True:
            for path in sorted(event_dir.glob("*.json")):
                with _span("consume_event"):
                    result = process_event(args.db, path, args.agent)
                print(
                    json.dumps(result, ensure_ascii=False),
                    flush=True,
                )
            time.sleep(2)

    if not args.event:
        parser.error("单次模式需要 --event")
    with _span("consume_event"):
        result = process_event(args.db, Path(args.event), args.agent)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _compose(spec: ApplicationSpec) -> str:
    return """services:
  migration:
    build:
      context: .
      dockerfile: infra/Dockerfile.agent
    command: ["python", "db/migrate.py", "--db", "/app/data/app.db"]
    volumes:
      - app-data:/app/data
    restart: "no"
  redpanda:
    image: redpandadata/redpanda:v24.3.1
    command:
      - redpanda
      - start
      - --overprovisioned
      - --smp
      - "1"
      - --memory
      - 1G
      - --reserve-memory
      - 0M
      - --node-id
      - "0"
      - --check=false
      - --kafka-addr
      - PLAINTEXT://0.0.0.0:9092
      - --advertise-kafka-addr
      - PLAINTEXT://redpanda:9092
      - --set
      - redpanda.auto_create_topics_enabled=true
    ports:
      - "${KAFKA_PORT:-19092}:9092"
    healthcheck:
      test: ["CMD-SHELL", "rpk cluster health | grep -q 'Healthy:.*true'"]
      interval: 5s
      timeout: 3s
      retries: 30
  api:
    build:
      context: .
      dockerfile: infra/Dockerfile.api
    environment:
      PORT: "8080"
      EVENTS_DIR: "/app/events"
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel:4317"
      KAFKA_BROKERS: "redpanda:9092"
      KAFKA_TOPIC: "ticket.created"
    volumes:
      - app-events:/app/events
    ports:
      - "${API_PORT:-18080}:8080"
    depends_on:
      redpanda:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/health"]
      interval: 5s
      timeout: 3s
      retries: 20
  consumer:
    build:
      context: .
      dockerfile: infra/Dockerfile.agent
    command:
      - "python"
      - "services/consumer/consumer.py"
      - "--db"
      - "/app/data/app.db"
      - "--agent"
      - "services/agent/agent.py"
      - "--kafka-brokers"
      - "redpanda:9092"
      - "--topic"
      - "ticket.created"
      - "--group"
      - "ticket-consumer"
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel:4317"
    volumes:
      - app-data:/app/data
      - app-events:/app/events
    depends_on:
      migration:
        condition: service_completed_successfully
      redpanda:
        condition: service_healthy
  agent:
    build:
      context: .
      dockerfile: infra/Dockerfile.agent
    command: ["python", "services/agent/agent.py", "--serve", "--port", "8001"]
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel:4317"
    ports:
      - "${AGENT_PORT:-18001}:8001"
  otel:
    image: otel/opentelemetry-collector:0.111.0
    command: ["--config=/etc/otel/config.yaml"]
    volumes:
      - ./observability/otel-collector.yaml:/etc/otel/config.yaml:ro
    ports:
      - "${OTEL_PORT:-14317}:4317"
  web:
    build:
      context: .
      dockerfile: infra/Dockerfile.web
    ports:
      - "${WEB_PORT:-15173}:5173"
volumes:
  app-data:
  app-events:
"""


def _api_package_json() -> str:
    return """{
  "name": "ticket-api",
  "private": true,
  "type": "module",
  "dependencies": {
    "@opentelemetry/api": "^1.9.0",
    "@opentelemetry/exporter-trace-otlp-grpc": "^0.57.2",
    "@opentelemetry/resources": "^1.30.1",
    "@opentelemetry/sdk-node": "^0.57.2",
    "@opentelemetry/semantic-conventions": "^1.30.1",
    "kafkajs": "^2.2.4"
  }
}
"""


def _api_tracing() -> str:
    return """import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-grpc'
import { Resource } from '@opentelemetry/resources'
import { NodeSDK } from '@opentelemetry/sdk-node'
import { ATTR_SERVICE_NAME } from '@opentelemetry/semantic-conventions'

const sdk = new NodeSDK({
  resource: new Resource({
    [ATTR_SERVICE_NAME]: 'ticket-api',
  }),
  traceExporter: new OTLPTraceExporter({
    url: process.env.OTEL_EXPORTER_OTLP_ENDPOINT || 'http://otel:4317',
  }),
})

sdk.start()

process.on('SIGTERM', () => {
  sdk.shutdown().finally(() => process.exit(0))
})
"""


def _dockerfile_node() -> str:
    return """FROM node:20-alpine
WORKDIR /app
COPY apps/api/package.json ./apps/api/package.json
RUN cd apps/api && npm install --omit=dev
COPY apps/api ./apps/api
EXPOSE 8080
CMD ["node", "apps/api/server.mjs"]
"""


def _web_server() -> str:
    return """import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const port = Number(process.env.PORT || 5173)
const root = path.dirname(fileURLToPath(import.meta.url))

const server = http.createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ status: 'UP' }))
    return
  }
  const file = path.join(root, req.url === '/' ? 'index.html' : req.url)
  if (!file.startsWith(root) || !fs.existsSync(file)) {
    res.writeHead(404)
    res.end('not found')
    return
  }
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  res.end(fs.readFileSync(file))
})

server.listen(port, () => {
  console.log(`web listening on ${port}`)
})
"""


def _dockerfile_web() -> str:
    return """FROM node:20-alpine
WORKDIR /app
COPY apps/web ./apps/web
EXPOSE 5173
CMD ["node", "apps/web/server.mjs"]
"""


def _requirements_txt() -> str:
    return """opentelemetry-api==1.28.2
opentelemetry-sdk==1.28.2
opentelemetry-exporter-otlp==1.28.2
kafka-python==2.2.15
"""


def _otel_py() -> str:
    return '''"""OpenTelemetry 初始化；依赖缺失时静默降级为空 tracer。"""

from __future__ import annotations

import os
from typing import Any


def setup_tracing(service_name: str) -> Any | None:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        return None

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317")
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
'''


def _dockerfile_python() -> str:
    return """FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY services ./services
COPY db ./db
COPY events ./events
COPY observability ./observability
CMD ["python", "services/consumer/consumer.py", "--watch"]
"""


def _migrate_py() -> str:
    return '''#!/usr/bin/env python3
"""表结构演进：按版本顺序执行，已执行的不重复。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/app.db")
    parser.add_argument("--migrations", default="db/migrations")
    args = parser.parse_args()

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = []
    for path in sorted(Path(args.migrations).glob("*.sql")):
        version = path.name
        exists = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if exists:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        applied.append(version)
    conn.close()
    print(json.dumps({"applied": applied}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _sample_event() -> str:
    return """{
  "eventId": "evt_001",
  "ticketId": "t_001",
  "title": "打印机无法连接",
  "content": "办公室打印机无法连接网络，影响日常办公"
}
"""


def _migration_001() -> str:
    return """CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
  event_id TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL
);
"""


def _migration_002() -> str:
    return """ALTER TABLE tickets ADD COLUMN ai_category TEXT;
ALTER TABLE tickets ADD COLUMN ai_confidence REAL;
ALTER TABLE tickets ADD COLUMN ai_suggestion TEXT;

CREATE INDEX IF NOT EXISTS idx_tickets_category ON tickets(ai_category);
"""


def _chart_yaml(spec: ApplicationSpec) -> str:
    return f"""apiVersion: v2
name: {spec.name}
description: {spec.goal}
type: application
version: 0.1.0
appVersion: "0.1.0"
"""


def _values_yaml(spec: ApplicationSpec) -> str:
    return f"""image:
  api: registry.local/{spec.name}-api:0.1.0
  worker: registry.local/{spec.name}-worker:0.1.0
replicaCount: 2
resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 256Mi
migration:
  enabled: true
  jobName: {spec.name}-migrate
"""


def _deployment_yaml(spec: ApplicationSpec) -> str:
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {spec.name}-api
spec:
  replicas: {{{{ .Values.replicaCount }}}}
  selector:
    matchLabels:
      app: {spec.name}-api
  template:
    metadata:
      labels:
        app: {spec.name}-api
    spec:
      containers:
        - name: api
          image: {{{{ .Values.image.api }}}}
          ports:
            - containerPort: 8080
          resources: {{{{- toYaml .Values.resources | nindent 12 }}}}
"""


def _service_yaml(spec: ApplicationSpec) -> str:
    return f"""apiVersion: v1
kind: Service
metadata:
  name: {spec.name}-api
spec:
  selector:
    app: {spec.name}-api
  ports:
    - name: http
      port: 8080
      targetPort: 8080
"""


def _migration_job_yaml(spec: ApplicationSpec) -> str:
    return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {spec.name}-migrate
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: {{{{ .Values.image.worker }}}}
          command: ["python", "db/migrate.py", "--db", "/app/data/app.db"]
          volumeMounts:
            - name: data
              mountPath: /app/data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: {spec.name}-data
"""


def _configmap_yaml(spec: ApplicationSpec) -> str:
    return f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: {spec.name}-config
data:
  APP_NAME: "{spec.name}"
  EVENTS_DIR: "/app/events"
  OTEL_EXPORTER_OTLP_ENDPOINT: "http://{spec.name}-otel:4317"
"""


def _pvc_yaml(spec: ApplicationSpec) -> str:
    return f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {spec.name}-data
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
"""


def _otel_k8s_yaml(spec: ApplicationSpec) -> str:
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {spec.name}-otel
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {spec.name}-otel
  template:
    metadata:
      labels:
        app: {spec.name}-otel
    spec:
      containers:
        - name: otel
          image: otel/opentelemetry-collector:0.111.0
          args: ["--config=/etc/otel/config.yaml"]
          ports:
            - containerPort: 4317
            - containerPort: 4318
          volumeMounts:
            - name: config
              mountPath: /etc/otel
      volumes:
        - name: config
          configMap:
            name: {spec.name}-otel-config
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {spec.name}-otel-config
data:
  config.yaml: |
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318
    exporters:
      debug:
        verbosity: detailed
    service:
      pipelines:
        traces:
          receivers: [otlp]
          exporters: [debug]
        metrics:
          receivers: [otlp]
          exporters: [debug]
"""


def _otel_yaml() -> str:
    return """receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
exporters:
  debug:
    verbosity: detailed
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      exporters: [debug]
"""


def _smoke_test() -> str:
    return '''#!/usr/bin/env python3
"""生成物自带冒烟：重复事件只写回一次。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(event_path: Path, db: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "services/consumer/consumer.py"),
            "--db",
            str(db),
            "--event",
            str(event_path),
            "--agent",
            str(ROOT / "services/agent/agent.py"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def main() -> int:
    db = ROOT / "data" / "smoke.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    subprocess.run(
        [sys.executable, str(ROOT / "db/migrate.py"), "--db", str(db)],
        check=True,
    )
    event = ROOT / "events" / "ticket.created.json"
    first = run(event, db)
    second = run(event, db)
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0]
    conn.close()
    assert first["duplicate"] is False, first
    assert second["duplicate"] is True, second
    assert count == 1, count
    print(json.dumps({"ok": True, "events": count}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


__all__ = ["generate_workspace"]
