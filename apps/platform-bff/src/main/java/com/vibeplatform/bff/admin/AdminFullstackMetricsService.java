package com.vibeplatform.bff.admin;

import com.fasterxml.jackson.databind.JsonNode;
import com.vibeplatform.bff.admin.AdminFullstackMetricsDtos.BlockDetails;
import com.vibeplatform.bff.admin.AdminFullstackMetricsDtos.FullstackMetricsData;
import com.vibeplatform.bff.admin.AdminFullstackMetricsDtos.Ratio;
import java.time.Instant;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * 透传 Python AI Core 的全栈生成指标。
 *
 * <p>AI Core 指标是进程内聚合（服务重启归零），这里不做缓存：
 * Admin 看板 30 秒轮询一次，直接读取最新快照即可。超时使用
 * {@code aiCoreRestClient} 的短超时，下游卡死时快速返回 502，
 * 不拖垮管理端页面。</p>
 */
@Service
public class AdminFullstackMetricsService {

    private static final Logger log =
            LoggerFactory.getLogger(AdminFullstackMetricsService.class);

    private static final String METRICS_PATH =
            "/api/v1/internal/fullstack/metrics";

    private final RestClient aiCoreRestClient;

    public AdminFullstackMetricsService(RestClient aiCoreRestClient) {
        this.aiCoreRestClient = aiCoreRestClient;
    }

    public FullstackMetricsData snapshot() {
        JsonNode body;
        try {
            body = aiCoreRestClient.get()
                    .uri(METRICS_PATH)
                    .retrieve()
                    .body(JsonNode.class);
        } catch (RestClientException ex) {
            log.warn("读取 AI Core 全栈指标失败 path={}: {}", METRICS_PATH, ex.toString());
            throw new FullstackMetricsUnavailableException(
                    "AI Core 全栈指标暂不可用", ex);
        }
        if (body == null || !body.isObject()) {
            throw new FullstackMetricsUnavailableException(
                    "AI Core 返回了空指标", new IllegalStateException("empty body"));
        }
        JsonNode details = body.path("details");
        return new FullstackMetricsData(
                Instant.now(),
                true,
                "python-ai",
                ratio(body.path("Applier_Block_Rate")),
                body.path("Contract_Misretry_Count").asLong(0),
                ratio(body.path("MultiTable_AST_Pass_Rate")),
                ratio(body.path("Contract_Revision_Rate")),
                new BlockDetails(
                        details.path("applierBlockedByJoin").asLong(0),
                        details.path("applierBlockedOther").asLong(0)
                )
        );
    }

    /**
     * 容错解析比率：上游字段缺失时返回 0/0，而不是抛错丢弃整份快照。
     * 这样新增指标不会破坏旧版 BFF 的兼容性。
     */
    private static Ratio ratio(JsonNode node) {
        if (node == null || !node.isObject()) {
            return new Ratio(0.0, 0L, 0L);
        }
        return new Ratio(
                node.path("rate").asDouble(0.0),
                node.path("numerator").asLong(0),
                node.path("denominator").asLong(0)
        );
    }
}
