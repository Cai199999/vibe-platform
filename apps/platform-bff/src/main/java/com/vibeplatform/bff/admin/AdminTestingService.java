package com.vibeplatform.bff.admin;

import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/** 透传 Python AI Core 的 TestAgent 指标与报告。 */
@Service
public class AdminTestingService {

    private static final Logger log =
            LoggerFactory.getLogger(AdminTestingService.class);

    private static final String METRICS_PATH = "/api/v1/internal/testing/metrics";
    private static final String REPORTS_PATH = "/api/v1/internal/testing/reports";

    private final RestClient aiCoreRestClient;

    public AdminTestingService(RestClient aiCoreRestClient) {
        this.aiCoreRestClient = aiCoreRestClient;
    }

    public JsonNode metrics() {
        return get(METRICS_PATH, "测试指标");
    }

    public JsonNode reports(int limit) {
        int capped = Math.max(1, Math.min(50, limit));
        return get(REPORTS_PATH + "?limit=" + capped, "测试报告");
    }

    private JsonNode get(String path, String label) {
        try {
            JsonNode body = aiCoreRestClient.get()
                    .uri(path)
                    .retrieve()
                    .body(JsonNode.class);
            if (body == null || !body.isObject()) {
                throw new TestingMetricsUnavailableException(
                        "AI Core 返回了空的" + label, new IllegalStateException("empty"));
            }
            return body;
        } catch (RestClientException ex) {
            log.warn("读取{}失败: {}", label, ex.toString());
            throw new TestingMetricsUnavailableException(
                    "AI Core " + label + "暂不可用", ex);
        }
    }
}
