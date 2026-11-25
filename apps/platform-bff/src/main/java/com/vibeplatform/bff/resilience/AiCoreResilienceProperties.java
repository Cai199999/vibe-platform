package com.vibeplatform.bff.resilience;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * AI Core 调用的舱壁（Bulkhead）与熔断器配置。
 *
 * <p>设计目标：单个下游变慢或挂掉时，最多占用固定数量的 BFF 线程；
 * 其余线程继续服务健康检查、源码持久化等不依赖 AI Core 的接口。</p>
 */
@ConfigurationProperties(prefix = "ai-core.resilience")
public record AiCoreResilienceProperties(
        Boolean enabled,
        Integer shortMaxConcurrent,
        Integer shortAcquireTimeoutMs,
        Integer interactiveMaxConcurrent,
        Integer interactiveAcquireTimeoutMs,
        Integer runMaxConcurrent,
        Integer runAcquireTimeoutMs,
        Integer failureThreshold,
        Integer openDurationMs,
        Integer halfOpenMaxCalls
) {

    public AiCoreResilienceProperties {
        if (enabled == null) {
            enabled = true;
        }
        shortMaxConcurrent = positive(shortMaxConcurrent, 64);
        shortAcquireTimeoutMs = nonNegative(shortAcquireTimeoutMs, 0);
        interactiveMaxConcurrent = positive(interactiveMaxConcurrent, 64);
        interactiveAcquireTimeoutMs = nonNegative(interactiveAcquireTimeoutMs, 0);
        runMaxConcurrent = positive(runMaxConcurrent, 48);
        runAcquireTimeoutMs = nonNegative(runAcquireTimeoutMs, 0);
        failureThreshold = positive(failureThreshold, 4);
        openDurationMs = positive(openDurationMs, 15_000);
        halfOpenMaxCalls = positive(halfOpenMaxCalls, 2);
    }

    private static int positive(Integer value, int fallback) {
        return value == null || value <= 0 ? fallback : value;
    }

    private static int nonNegative(Integer value, int fallback) {
        return value == null || value < 0 ? fallback : value;
    }
}
