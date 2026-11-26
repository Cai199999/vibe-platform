package com.vibeplatform.bff.admin;

import com.fasterxml.jackson.databind.JsonNode;
import com.vibeplatform.bff.run.RunSandboxRegistry;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import com.vibeplatform.bff.admin.AdminMetricsDtos.UsageSummary;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import com.vibeplatform.bff.admin.AdminMetricsDtos.AgentShare;
import com.vibeplatform.bff.admin.AdminMetricsDtos.MetricsData;
import com.vibeplatform.bff.admin.AdminMetricsDtos.Overview;
import com.vibeplatform.bff.admin.AdminMetricsDtos.RecentRun;

/**
 * 管理中台指标聚合（M3.1 进程内实现）。
 *
 * <p>BFF 目前不落库 Run，因此这里维护一个最近 Run 的轻量内存表：
 * 由 SSE 透传层与 Supervisor 派发层写入，Admin 看板读取快照。容量固定，
 * 超出后优先淘汰已结束的旧记录，避免长跑实例内存无限增长。</p>
 *
 * <p>M3.2 接入 {@code run} / {@code run_event} 落库后，本服务可以替换为
 * 读聚合表，但 {@link AdminMetricsDtos} 的对外契约保持不变。</p>
 */
@Service
public class AdminMetricsService {

    private static final Logger log = LoggerFactory.getLogger(AdminMetricsService.class);

    /** 内置 Agent ID：与 ai_core 各模块的 manifest.json 保持一致。 */
    public static final String UI_AGENT_ID = "ui_graph";
    public static final String ARCHITECT_AGENT_ID = "architect_graph";
    public static final String FULLSTACK_AGENT_ID = "fullstack_graph";

    /** 内存中最多保留的 Run 数；看板只展示最近 10 条，其余用于聚合。 */
    private static final int MAX_RUNS = 5_000;

    private static final int RECENT_LIMIT = 10;

    private static final Map<String, String> BUILTIN_AGENT_NAMES = Map.of(
            UI_AGENT_ID, "局部 UI 工程师",
            ARCHITECT_AGENT_ID, "全局架构师",
            FULLSTACK_AGENT_ID, "全栈生成工程师",
            "backend_coder", "后端工程师（预留）",
            "script_agent", "脚本工程师（预留）"
    );

    private final RunSandboxRegistry sandboxRegistry;

    /** 插入顺序 = 首次观测顺序；淘汰时从最早记录开始。 */
    private final Map<String, MutableRun> runs = new LinkedHashMap<>();

    private final Map<String, String> agentNames =
            new HashMap<>(BUILTIN_AGENT_NAMES);

    private Instant lastEventAt;

    private boolean demo;

    public AdminMetricsService(RunSandboxRegistry sandboxRegistry) {
        this.sandboxRegistry = sandboxRegistry;
    }

    // --------------------------------------------------------
    // 写入入口
    // --------------------------------------------------------

    /**
     * 处理一条 SSE RunEvent。
     *
     * @param runId          Run ID（事件 envelope 中的 runId）
     * @param type           事件类型，例如 run.succeeded
     * @param ts             事件时间戳；为空时使用当前时间
     * @param payload        事件 payload，用于提取 sandboxId
     * @param defaultAgentId 当前事件流所属 Agent；Supervisor 派发信息缺失时的兜底
     */
    public synchronized void observeEvent(
            String runId,
            String type,
            Instant ts,
            JsonNode payload,
            String defaultAgentId
    ) {
        if (isBlank(runId) || isBlank(type)) {
            return;
        }
        Instant eventAt = ts == null ? Instant.now() : ts;
        MutableRun run = touch(runId, defaultAgentId, eventAt);

        if (payload != null) {
            JsonNode sandboxNode = payload.get("sandboxId");
            if (sandboxNode != null && sandboxNode.isTextual()
                    && !sandboxNode.asText().isBlank()) {
                run.sandboxId = sandboxNode.asText();
            }
            accumulateUsage(run, type, eventAt, payload);
        }

        lastEventAt = later(lastEventAt, eventAt);

        String status = statusFor(type);
        if (status == null || run.terminal) {
            // 未知事件忽略；终态之后的重放 / 重复事件不再改写结果。
            return;
        }
        run.status = status;
        if (isTerminal(status)) {
            run.terminal = true;
            run.finishedAt = eventAt;
            run.durationMs = durationMillis(run.startedAt, run.finishedAt);
        }
    }

    /**
     * Supervisor dispatch / gate release 返回后登记子 Run 的真实 Agent。
     *
     * <p>dispatch 先于子图 SSE 启动，因此这里写入的归属优先于事件流兜底值。</p>
     */
    public synchronized void registerDispatches(JsonNode dispatchBody) {
        if (dispatchBody == null) {
            return;
        }
        JsonNode dispatches = dispatchBody.path("dispatches");
        if (!dispatches.isArray()) {
            return;
        }
        for (JsonNode dispatch : dispatches) {
            String runId = text(dispatch, "runId");
            String agentId = text(dispatch, "agentId");
            if (isBlank(runId) || isBlank(agentId)) {
                continue;
            }
            attributeRun(runId, agentId, Instant.now());
        }
    }

    /**
     * 从 {@code /supervisor/agents} 响应缓存自定义 Agent 的展示名。
     */
    public synchronized void registerAgentNames(JsonNode agentsBody) {
        if (agentsBody == null) {
            return;
        }
        JsonNode agents = agentsBody.path("agents");
        if (!agents.isArray()) {
            return;
        }
        for (JsonNode agent : agents) {
            String agentId = text(agent, "agentId");
            String displayName = text(agent, "displayName");
            if (!isBlank(agentId) && !isBlank(displayName)) {
                agentNames.put(agentId, displayName);
            }
        }
    }

    /**
     * 兼容 M1 同步 Run 路径：没有 SSE 时由 RunService 直接登记开始与终态。
     */
    public synchronized void recordSynchronousRun(
            String runId,
            String agentId,
            Instant startedAt,
            String status,
            String sandboxId
    ) {
        if (isBlank(runId) || isBlank(status)) {
            return;
        }
        Instant effectiveStart = startedAt == null ? Instant.now() : startedAt;
        attributeRun(runId, agentId, effectiveStart);
        MutableRun run = runs.get(runId);
        if (run == null) {
            return;
        }
        if (!isBlank(sandboxId)) {
            run.sandboxId = sandboxId;
        }
        Instant now = Instant.now();
        lastEventAt = later(lastEventAt, now);
        run.status = status.toUpperCase(Locale.ROOT);
        if (isTerminal(run.status)) {
            run.terminal = true;
            run.finishedAt = now;
            run.durationMs = durationMillis(run.startedAt, run.finishedAt);
        }
    }

    /**
     * 预置一套演示数据，仅在显式开启 {@code ADMIN_METRICS_DEMO_SEED=true}
     * 且内存中还没有真实 Run 时生效。响应中的 {@code demo=true} 会明确
     * 告知前端“这不是生产指标”，避免把样例数据误读为线上数据。
     */
    public synchronized boolean seedDemoData() {
        if (!runs.isEmpty()) {
            log.info("skip admin demo seed: {} real runs already recorded", runs.size());
            return false;
        }
        Instant now = Instant.now();
        ZoneId zone = ZoneId.systemDefault();
        Instant startOfDay = now.atZone(zone).toLocalDate()
                .atStartOfDay(zone)
                .toInstant();
        // 演示数据必须落在“今天”，否则今日累计 Run 会受执行时间影响。
        long maxOffsetSeconds = Math.max(
                1,
                Duration.between(startOfDay, now).getSeconds() - 5
        );
        List<DemoSpec> specs = List.of(
                // 3 个进行中的 Run，对应 3 个活跃沙盒。
                new DemoSpec("run_demo_ui_01", UI_AGENT_ID, "RUNNING", 95, 0, "sandbox_demo_ui_01"),
                new DemoSpec("run_demo_ui_02", UI_AGENT_ID, "RUNNING", 45, 0, "sandbox_demo_ui_02"),
                new DemoSpec("run_demo_arch_01", ARCHITECT_AGENT_ID, "RUNNING", 240, 0, "sandbox_demo_arch_01"),

                // UI Graph 最近成功 / 失败记录。
                new DemoSpec("run_demo_ui_03", UI_AGENT_ID, "SUCCEEDED", 9 * 60, 18_200, null),
                new DemoSpec("run_demo_ui_04", UI_AGENT_ID, "SUCCEEDED", 14 * 60, 12_400, null),
                new DemoSpec("run_demo_ui_05", UI_AGENT_ID, "SUCCEEDED", 22 * 60, 27_800, null),
                new DemoSpec("run_demo_ui_06", UI_AGENT_ID, "SUCCEEDED", 31 * 60, 9_600, null),
                new DemoSpec("run_demo_ui_07", UI_AGENT_ID, "SUCCEEDED", 47 * 60, 21_300, null),
                new DemoSpec("run_demo_ui_08", UI_AGENT_ID, "SUCCEEDED", 63 * 60, 15_800, null),
                new DemoSpec("run_demo_ui_09", UI_AGENT_ID, "FAILED", 88 * 60, 33_400, null),
                new DemoSpec("run_demo_ui_10", UI_AGENT_ID, "SUCCEEDED", 120 * 60, 11_900, null),
                new DemoSpec("run_demo_ui_11", UI_AGENT_ID, "SUCCEEDED", 150 * 60, 24_600, null),
                new DemoSpec("run_demo_ui_12", UI_AGENT_ID, "SUCCEEDED", 190 * 60, 17_200, null),

                // Architect 全栈设计记录，耗时长于局部 UI 修改。
                new DemoSpec("run_demo_arch_02", ARCHITECT_AGENT_ID, "SUCCEEDED", 12 * 60, 41_200, null),
                new DemoSpec("run_demo_arch_03", ARCHITECT_AGENT_ID, "SUCCEEDED", 26 * 60, 36_800, null),
                new DemoSpec("run_demo_arch_04", ARCHITECT_AGENT_ID, "SUCCEEDED", 55 * 60, 28_900, null),
                new DemoSpec("run_demo_arch_05", ARCHITECT_AGENT_ID, "SUCCEEDED", 95 * 60, 44_100, null),
                new DemoSpec("run_demo_arch_06", ARCHITECT_AGENT_ID, "SUCCEEDED", 160 * 60, 31_700, null)
        );

        for (DemoSpec spec : specs) {
            long offsetSeconds = Math.min(spec.startedSecondsAgo(), maxOffsetSeconds);
            Instant startedAt = now.minusSeconds(offsetSeconds);
            MutableRun run = new MutableRun(spec.runId(), spec.agentId(),
                    agentName(spec.agentId()), startedAt);
            run.status = spec.status();
            run.sandboxId = spec.sandboxId();
            if (isTerminal(spec.status())) {
                run.terminal = true;
                Instant finishedAt = startedAt.plusMillis(spec.durationMs());
                // 刚过零点时所有样例会被压缩到最近几秒，结束时间不能晚于 now。
                run.finishedAt = finishedAt.isAfter(now) ? now : finishedAt;
                run.durationMs = durationMillis(startedAt, run.finishedAt);
                // 演示 token / 成本：架构师链路显著高于局部 UI 修改。
                boolean architect = ARCHITECT_AGENT_ID.equals(spec.agentId());
                run.inputTokens = architect ? 12_400 : 3_200;
                run.outputTokens = architect ? 4_100 : 1_100;
                run.calls = architect ? 9 : 3;
                run.cost = architect ? 0.135 : 0.032;
            } else if (!isBlank(spec.sandboxId())) {
                // 让“活跃沙盒”卡片与进行中的 Run 对得上。
                sandboxRegistry.record(spec.runId(), spec.sandboxId());
            }
            runs.put(spec.runId(), run);
        }
        demo = true;
        lastEventAt = now;
        log.info("admin demo metrics seeded: {} runs", specs.size());
        return true;
    }

    /**
     * 从事件 payload 累计 token / 成本。
     *
     * <p>Architect 按节点下发增量 usage，UI Run 在终态下发汇总 usage。
     * 断线重放会重复投递同一事件，因此用
     * {@code type + node + ts + tokens + cost + calls} 指纹去重。</p>
     */
    private void accumulateUsage(
            MutableRun run,
            String type,
            Instant eventAt,
            JsonNode payload
    ) {
        JsonNode usage = payload.get("usage");
        if (usage == null || !usage.isObject()) {
            return;
        }
        long input = Math.max(0, usage.path("inputTokens").asLong(0));
        long output = Math.max(0, usage.path("outputTokens").asLong(0));
        long cached = Math.max(0, usage.path("cachedInputTokens").asLong(0));
        long calls = Math.max(0, usage.path("calls").asLong(0));
        double cost = Math.max(0.0, usage.path("cost").asDouble(0.0));
        if (input == 0 && output == 0 && calls == 0 && cost == 0.0) {
            return;
        }
        String node = payload.path("node").asText("");
        String fingerprint = type + "|" + node + "|" + eventAt
                + "|" + input + "|" + output + "|" + calls + "|" + cost;
        if (!run.usageKeys.add(fingerprint)) {
            return;
        }
        run.inputTokens += input;
        run.outputTokens += output;
        run.cachedInputTokens += cached;
        run.calls += calls;
        run.cost += cost;
        String currency = usage.path("currency").asText("");
        if (!currency.isBlank()) {
            run.currency = currency;
        }
    }

    // --------------------------------------------------------
    // 快照
    // --------------------------------------------------------

    public synchronized MetricsData snapshot() {
        Instant now = Instant.now();
        ZoneId zone = ZoneId.systemDefault();
        LocalDate today = now.atZone(zone).toLocalDate();

        long totalRunsToday = 0;
        long succeeded = 0;
        long failed = 0;
        long durationSum = 0;
        long durationCount = 0;
        for (MutableRun run : runs.values()) {
            if (run.startedAt != null
                    && run.startedAt.atZone(zone).toLocalDate().equals(today)) {
                totalRunsToday++;
            }
            if (!run.terminal) {
                continue;
            }
            if ("SUCCEEDED".equals(run.status)) {
                succeeded++;
            } else if ("FAILED".equals(run.status)) {
                failed++;
            }
            // p50/p95/平均耗时口径：只统计成功与失败，排除用户取消 / 拒绝。
            if (("SUCCEEDED".equals(run.status) || "FAILED".equals(run.status))
                    && run.durationMs >= 0) {
                durationSum += run.durationMs;
                durationCount++;
            }
        }

        long sampleSize = succeeded + failed;
        Overview overview = new Overview(
                sandboxRegistry.activeCount(),
                totalRunsToday,
                runs.size(),
                ratio(succeeded, sampleSize),
                sampleSize,
                durationCount == 0 ? 0 : durationSum / durationCount
        );

        long inputTokens = 0;
        long outputTokens = 0;
        long cachedInputTokens = 0;
        long calls = 0;
        double cost = 0.0;
        long runsWithUsage = 0;
        String currency = "CNY";
        for (MutableRun run : runs.values()) {
            if (run.calls <= 0 && run.inputTokens <= 0 && run.outputTokens <= 0) {
                continue;
            }
            runsWithUsage++;
            inputTokens += run.inputTokens;
            outputTokens += run.outputTokens;
            cachedInputTokens += run.cachedInputTokens;
            calls += run.calls;
            cost += run.cost;
            if (!isBlank(run.currency)) {
                currency = run.currency;
            }
        }
        UsageSummary usage = new UsageSummary(
                inputTokens,
                outputTokens,
                cachedInputTokens,
                inputTokens + outputTokens,
                calls,
                roundCost(cost),
                currency,
                runsWithUsage,
                runsWithUsage == 0 ? 0.0 : roundCost(cost / runsWithUsage)
        );

        return new MetricsData(
                now,
                freshnessSeconds(now),
                demo,
                zone.getId(),
                overview,
                agentDistribution(),
                recentRuns(now),
                usage
        );
    }

    private List<AgentShare> agentDistribution() {
        Map<String, Long> counts = new HashMap<>();
        Map<String, String> names = new HashMap<>();
        for (MutableRun run : runs.values()) {
            String agentId = isBlank(run.agentId) ? "unknown" : run.agentId;
            counts.merge(agentId, 1L, Long::sum);
            names.putIfAbsent(agentId, isBlank(run.agentName) ? agentId : run.agentName);
        }
        long total = runs.size();
        List<AgentShare> result = new ArrayList<>(counts.size());
        counts.forEach((agentId, count) -> result.add(new AgentShare(
                agentId,
                names.getOrDefault(agentId, agentId),
                count,
                ratio(count, total)
        )));
        result.sort(Comparator.comparingLong(AgentShare::runs)
                .reversed()
                .thenComparing(AgentShare::agentId));
        return result;
    }

    private List<RecentRun> recentRuns(Instant now) {
        List<MutableRun> ordered = new ArrayList<>(runs.values());
        ordered.sort(Comparator.comparing(
                (MutableRun run) -> run.startedAt == null ? Instant.EPOCH : run.startedAt
        ).reversed());

        List<RecentRun> result = new ArrayList<>(Math.min(RECENT_LIMIT, ordered.size()));
        for (MutableRun run : ordered) {
            if (result.size() >= RECENT_LIMIT) {
                break;
            }
            long duration = run.terminal
                    ? run.durationMs
                    : (run.startedAt == null
                            ? 0
                            : Math.max(0, Duration.between(run.startedAt, now).toMillis()));
            result.add(new RecentRun(
                    run.runId,
                    run.agentId,
                    run.agentName,
                    run.status,
                    duration,
                    run.startedAt,
                    run.finishedAt,
                    run.sandboxId
            ));
        }
        return result;
    }

    private long freshnessSeconds(Instant now) {
        if (lastEventAt == null) {
            return 0;
        }
        return Math.max(0, Duration.between(lastEventAt, now).getSeconds());
    }

    // --------------------------------------------------------
    // 内部工具
    // --------------------------------------------------------

    private void attributeRun(String runId, String agentId, Instant at) {
        Instant effectiveAt = at == null ? Instant.now() : at;
        MutableRun run = runs.get(runId);
        if (run == null) {
            runs.put(runId, new MutableRun(
                    runId,
                    agentId,
                    agentName(agentId),
                    effectiveAt
            ));
            evictIfNeeded();
            lastEventAt = later(lastEventAt, effectiveAt);
            return;
        }
        // dispatch 是权威归属，允许覆盖事件流按端点推断的兜底值。
        if (!isBlank(agentId)) {
            run.agentId = agentId;
            run.agentName = agentName(agentId);
        }
        if (run.startedAt == null || effectiveAt.isBefore(run.startedAt)) {
            run.startedAt = effectiveAt;
            if (run.terminal && run.finishedAt != null) {
                run.durationMs = durationMillis(run.startedAt, run.finishedAt);
            }
        }
    }

    private MutableRun touch(String runId, String defaultAgentId, Instant eventAt) {
        MutableRun run = runs.get(runId);
        if (run == null) {
            run = new MutableRun(
                    runId,
                    defaultAgentId,
                    agentName(defaultAgentId),
                    eventAt
            );
            runs.put(runId, run);
            evictIfNeeded();
            return run;
        }
        if (isBlank(run.agentId) && !isBlank(defaultAgentId)) {
            run.agentId = defaultAgentId;
            run.agentName = agentName(defaultAgentId);
        }
        if (run.startedAt == null || eventAt.isBefore(run.startedAt)) {
            run.startedAt = eventAt;
            if (run.terminal && run.finishedAt != null) {
                run.durationMs = durationMillis(run.startedAt, run.finishedAt);
            }
        }
        return run;
    }

    /**
     * 超出容量时优先淘汰最早的已结束 Run；全部为进行中则暂不淘汰，
     * 避免把看板正在统计的活跃任务删掉。
     */
    private void evictIfNeeded() {
        if (runs.size() <= MAX_RUNS) {
            return;
        }
        Iterator<Map.Entry<String, MutableRun>> iterator = runs.entrySet().iterator();
        while (runs.size() > MAX_RUNS && iterator.hasNext()) {
            Map.Entry<String, MutableRun> entry = iterator.next();
            if (entry.getValue().terminal) {
                iterator.remove();
            }
        }
    }

    private String agentName(String agentId) {
        if (isBlank(agentId)) {
            return "未知 Agent";
        }
        return agentNames.getOrDefault(agentId, agentId);
    }

    private static String statusFor(String type) {
        return switch (type) {
            case "run.started", "architect.started", "fullstack.started" -> "RUNNING";
            case "run.succeeded", "architect.contract.confirmed", "fullstack.completed" ->
                    "SUCCEEDED";
            case "run.failed", "architect.failed", "fullstack.failed" -> "FAILED";
            case "run.cancelled", "architect.cancelled" -> "CANCELLED";
            case "run.rejected" -> "REJECTED";
            case "approval.required" -> "AWAITING_APPROVAL";
            case "clarification.required", "architect.clarification.required" ->
                    "AWAITING_CLARIFICATION";
            case "architect.contract.ready" -> "AWAITING_CONTRACT";
            default -> null;
        };
    }

    private static boolean isTerminal(String status) {
        return Objects.equals(status, "SUCCEEDED")
                || Objects.equals(status, "FAILED")
                || Objects.equals(status, "CANCELLED")
                || Objects.equals(status, "REJECTED");
    }

    private static long durationMillis(Instant startedAt, Instant finishedAt) {
        if (startedAt == null || finishedAt == null) {
            return 0;
        }
        return Math.max(0, Duration.between(startedAt, finishedAt).toMillis());
    }

    private static double ratio(long numerator, long denominator) {
        if (denominator <= 0) {
            return 0.0;
        }
        return Math.round((double) numerator / denominator * 10_000d) / 10_000d;
    }

    private static double roundCost(double value) {
        return Math.round(value * 1_000_000d) / 1_000_000d;
    }

    private static Instant later(Instant current, Instant candidate) {
        if (candidate == null) {
            return current;
        }
        return current == null || candidate.isAfter(current) ? candidate : current;
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value != null && value.isTextual() ? value.asText() : null;
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }

    private record DemoSpec(
            String runId,
            String agentId,
            String status,
            long startedSecondsAgo,
            long durationMs,
            String sandboxId
    ) {
    }

    /** 可变聚合记录；只在 {@link AdminMetricsService} 的同步块内访问。 */
    private static final class MutableRun {

        private final String runId;
        private String agentId;
        private String agentName;
        private String status = "RUNNING";
        private String sandboxId;
        private Instant startedAt;
        private Instant finishedAt;
        private long durationMs = -1;
        private boolean terminal;
        private long inputTokens;
        private long outputTokens;
        private long cachedInputTokens;
        private long calls;
        private double cost;
        private String currency = "CNY";
        private final Set<String> usageKeys = new HashSet<>();

        private MutableRun(
                String runId,
                String agentId,
                String agentName,
                Instant startedAt
        ) {
            this.runId = runId;
            this.agentId = agentId;
            this.agentName = agentName;
            this.startedAt = startedAt;
        }
    }
}
