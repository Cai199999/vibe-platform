package com.vibeplatform.bff.admin;

import java.time.Instant;
import java.util.List;

/**
 * 管理中台指标契约（M3.1）。
 *
 * <p>字段与 {@code docs/design/03.API接口设计.md} §14.5 对齐：
 * 顶层 {@code data + requestId}，时间统一 RFC 3339 UTC。</p>
 */
public final class AdminMetricsDtos {

    private AdminMetricsDtos() {
    }

    public record MetricsResponse(MetricsData data, String requestId) {
    }

    public record MetricsData(
            Instant generatedAt,
            long freshnessSeconds,
            boolean demo,
            String timezone,
            Overview overview,
            List<AgentShare> agentDistribution,
            List<RecentRun> recentRuns,
            UsageSummary usage
    ) {
    }

    /**
     * Token / 成本汇总。
     *
     * <p>来自 Run 事件 payload.usage（Python 侧按节点累计）；
     * 只统计带 usage 的 Run。货币与 Python 计价配置保持一致。</p>
     */
    public record UsageSummary(
            long inputTokens,
            long outputTokens,
            long cachedInputTokens,
            long totalTokens,
            long calls,
            double cost,
            String currency,
            long runsWithUsage,
            double averageCost
    ) {
    }

    /**
     * 看板顶部指标卡。
     *
     * <p>{@code successRate} 分母只包含 SUCCEEDED + FAILED；CANCELLED /
     * REJECTED / 暂停态不计入，与架构文档口径一致。样本数为 0 时返回 0，
     * 前端按“暂无数据”展示。</p>
     */
    public record Overview(
            int activeSandboxes,
            long totalRunsToday,
            long totalRuns,
            double successRate,
            long successRateSampleSize,
            long averageDurationMs
    ) {
    }

    public record AgentShare(
            String agentId,
            String displayName,
            long runs,
            double percentage
    ) {
    }

    public record RecentRun(
            String runId,
            String agentId,
            String agentName,
            String status,
            long durationMs,
            Instant startedAt,
            Instant finishedAt,
            String sandboxId
    ) {
    }
}
