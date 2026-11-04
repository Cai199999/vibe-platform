package com.vibeplatform.bff.health;

import java.time.Instant;
import java.util.Map;

/**
 * BFF 健康检查响应。
 *
 * @param status        UP / DEGRADED
 * @param service       服务名
 * @param version       应用版本
 * @param timestamp     检查时间
 * @param dependencies  下游依赖检查结果
 */
public record HealthResponse(
        String status,
        String service,
        String version,
        Instant timestamp,
        Map<String, DependencyStatus> dependencies
) {

    /**
     * 单个依赖的健康状态。
     *
     * @param status   UP / DOWN
     * @param target   依赖地址或服务名
     * @param details  下游返回的原始摘要
     * @param error    失败原因，成功时为 null
     */
    public record DependencyStatus(
            String status,
            String target,
            Object details,
            String error
    ) {
    }
}
