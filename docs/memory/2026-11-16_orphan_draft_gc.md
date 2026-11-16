# 2026-11-16 孤儿 Draft 回收

## 背景

草稿登记表 `LocalDockerSandbox._drafts` 是进程内存态。python-ai 重启后登记
丢失，但沙盒容器内的 git worktree 与 5174 草稿预览进程仍在，形成孤儿：

- 长期占用磁盘与预览进程；
- 新 Run 在同一沙盒创建草稿时会抢 5174 端口，导致预览启动失败。

## 方案

1. `SandboxProvider.reap_orphan_drafts(ttl_days=None)`：默认空实现，保持
   其它 Provider 兼容。
2. `LocalDockerSandbox.reap_orphan_drafts`：
   - 通过 `vibe.managed=true` 标签枚举容器，不依赖进程内 `_sandboxes`；
   - 扫描容器内 `/app-drafts/drf_*`，用目录 `mtime` 判定年龄；
   - 跳过 `_drafts` 已登记的活跃草稿（TTL 再大也不碰）；
   - 超过 `SANDBOX_DRAFT_ORPHAN_TTL_DAYS`（默认 7 天）才执行
     `_stop_draft_preview` + `git worktree remove --force` + `prune` + `rm -rf`；
   - 单个沙盒 / 草稿失败只记日志，不中断整轮。
3. `begin_draft` 前主动回收同沙盒残留草稿，保留 60 秒并发宽限
   （`DRAFT_RECLAIM_GRACE_SECONDS`）。这解决了“重启后第一次 Run 因 5174
   端口被旧 worktree 占用而失败”的真实故障，同时避免误删另一线程刚创建、
   尚未登记的新草稿。
4. `ai_core/sandbox_gc.py` + FastAPI lifespan：daemon 线程启动即执行一次，
   之后每 `SANDBOX_DRAFT_GC_INTERVAL_SECONDS`（默认 6h）执行；任何异常只
   记日志，不阻塞服务启动与健康检查。
5. 配置开关：`SANDBOX_DRAFT_GC_ENABLED=false` 或 TTL<=0 可完全停用。

## 取舍

- **7 天 TTL**：保留现场给人工排障，不在一重启时立刻删数据；端口阻塞由
  `begin_draft` 的即时回收兜底，两条路径职责分离。
- **不引入新存储**：重启后唯一可信的年龄来源是容器内目录 mtime；若未来需要
  精确审计，再引入 MySQL/Redis 记录。
- **多 worker 限制**：当前 uvicorn 单进程，未登记即可判定为孤儿。若未来改为
  多 worker / 多副本，`_drafts` 必须外置（Redis），否则一个进程会误删另一个
  进程仍在审批的草稿。

## 验证

- 新增 8 项 `tests/test_draft_gc.py`：过期回收、未过期保留、活跃保护、
  TTL<=0 停用、Docker 不可用降级、单草稿失败隔离、调度器启停、
  `begin_draft` 前回收。
- 全量 Python unittest：544 项通过。
- 在真实 `vibe-platform/sandbox-react:dev` 镜像中验证 `stat -c %Y` 循环
  命令兼容（busybox）。
