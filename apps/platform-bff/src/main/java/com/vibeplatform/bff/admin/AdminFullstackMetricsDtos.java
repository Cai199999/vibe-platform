package com.vibeplatform.bff.admin;

import java.time.Instant;

/**
 * 全栈生成核心指标的 BFF 透传契约。
 *
 * <p>Python AI Core 的指标键使用稳定的 PascalCase（如
 * {@code Applier_Block_Rate}），BFF 在这里转换为前端友好的 camelCase，
 * 避免前端直接耦合内部指标命名。字段只包含计数与比率，不包含 runId
 * 或源码。</p>
 */
public final class AdminFullstackMetricsDtos {

    private AdminFullstackMetricsDtos() {
    }

    public record FullstackMetricsResponse(
            FullstackMetricsData data,
            String requestId
    ) {
    }

    public record FullstackMetricsData(
            Instant generatedAt,
            boolean available,
            String upstream,
            Ratio applierBlockRate,
            long contractMisretryCount,
            Ratio multiTableAstPassRate,
            Ratio contractRevisionRate,
            BlockDetails details
    ) {
    }

    /**
     * 比率快照；分母为 0 时 rate = 0，前端按“暂无样本”展示。
     */
    public record Ratio(double rate, long numerator, long denominator) {
    }

    /**
     * 拦截明细：JOIN 关系错误 / 其他 ORM 缺陷。
     */
    public record BlockDetails(
            long applierBlockedByJoin,
            long applierBlockedOther
    ) {
    }
}
