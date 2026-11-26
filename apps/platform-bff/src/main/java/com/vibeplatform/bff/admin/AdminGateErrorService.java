package com.vibeplatform.bff.admin;

import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * 透传 Python AI Core 的门禁错误高频统计。
 *
 * <p>门禁错误目录与账本在 Python 进程内维护（同时写 JSONL）；
 * BFF 只做管理员可见的只读代理，不缓存，避免与 30 秒轮询叠加。</p>
 */
@Service
public class AdminGateErrorService {

    private static final Logger log =
            LoggerFactory.getLogger(AdminGateErrorService.class);

    private static final String STATS_PATH =
            "/api/v1/internal/collaboration/errors/stats";

    private final RestClient aiCoreRestClient;

    public AdminGateErrorService(RestClient aiCoreRestClient) {
        this.aiCoreRestClient = aiCoreRestClient;
    }

    public JsonNode stats(int top) {
        int capped = Math.max(1, Math.min(50, top));
        try {
            JsonNode body = aiCoreRestClient.get()
                    .uri(uriBuilder -> uriBuilder
                            .path(STATS_PATH)
                            .queryParam("top", capped)
                            .build())
                    .retrieve()
                    .body(JsonNode.class);
            if (body == null || !body.isObject()) {
                throw new GateErrorStatsUnavailableException(
                        "AI Core 返回了空的门禁错误统计",
                        new IllegalStateException("empty body"));
            }
            return body;
        } catch (RestClientException ex) {
            log.warn("读取门禁错误统计失败: {}", ex.toString());
            throw new GateErrorStatsUnavailableException(
                    "AI Core 门禁错误统计暂不可用", ex);
        }
    }

}
