package com.vibeplatform.bff.supervisor;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vibeplatform.bff.admin.AdminMetricsService;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * Supervisor 非流式接口代理。
 *
 * <p>路由 / 派发 / 注册中心都是短请求；子图自身的 SSE 由客户端按
 * {@code dispatches[].streamPath} 直接订阅，BFF 不在本层合并事件流。</p>
 */
@Service
public class SupervisorProxyService {

    private static final Logger log =
            LoggerFactory.getLogger(SupervisorProxyService.class);
    private static final String ROUTE_PATH =
            "/api/v1/internal/supervisor/route";
    private static final String DISPATCH_PATH =
            "/api/v1/internal/supervisor/dispatch";
    private static final String GATE_PATH =
            "/api/v1/internal/supervisor/gates/";
    private static final String AGENTS_PATH =
            "/api/v1/internal/supervisor/agents";
    private static final String RELOAD_PATH =
            "/api/v1/internal/supervisor/agents/reload";

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;
    private final AdminMetricsService adminMetricsService;

    public SupervisorProxyService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper,
            AdminMetricsService adminMetricsService
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
        this.adminMetricsService = adminMetricsService;
    }

    public ResponseEntity<JsonNode> route(JsonNode body) {
        return postJson(ROUTE_PATH, body, "supervisor route");
    }

    public ResponseEntity<JsonNode> dispatch(JsonNode body) {
        ResponseEntity<JsonNode> response =
                postJson(DISPATCH_PATH, body, "supervisor dispatch");
        // 先于子图 SSE 写入 runId -> agentId，看板才能把 Run 归到正确 Agent。
        adminMetricsService.registerDispatches(response.getBody());
        return response;
    }

    public ResponseEntity<JsonNode> gate(String gateId) {
        return getJson(GATE_PATH + gateId, "supervisor gate");
    }

    public ResponseEntity<JsonNode> releaseGate(String gateId) {
        ResponseEntity<JsonNode> response = postJson(
                GATE_PATH + gateId + "/release",
                null,
                "supervisor gate release"
        );
        // both 模式下 UI Run 在 gate release 时才真正启动。
        adminMetricsService.registerDispatches(response.getBody());
        return response;
    }

    public ResponseEntity<JsonNode> agents() {
        ResponseEntity<JsonNode> response =
                getJson(AGENTS_PATH, "supervisor agents");
        // 缓存自定义 Agent 展示名，供看板表格与占比图使用。
        adminMetricsService.registerAgentNames(response.getBody());
        return response;
    }

    public ResponseEntity<JsonNode> reloadAgents() {
        return postJson(RELOAD_PATH, null, "supervisor agents reload");
    }

    private ResponseEntity<JsonNode> postJson(
            String path,
            JsonNode body,
            String operation
    ) {
        long startedAt = System.nanoTime();
        final String jsonBody;
        try {
            jsonBody = body == null ? "{}" : objectMapper.writeValueAsString(body);
        } catch (JsonProcessingException ex) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                    .body(errorBody(
                            "REQUEST_SERIALIZATION_FAILED",
                            operation + " 请求序列化失败"
                    ));
        }
        try {
            ResponseEntity<JsonNode> response = aiCoreRunRestClient.post()
                    .uri(path)
                    .contentType(MediaType.APPLICATION_JSON)
                    .accept(MediaType.APPLICATION_JSON)
                    .body(jsonBody)
                    .exchange((clientRequest, clientResponse) -> {
                        JsonNode responseBody = clientResponse.bodyTo(JsonNode.class);
                        if (responseBody == null) {
                            responseBody = errorBody(
                                    "EMPTY_AI_CORE_RESPONSE",
                                    "AI Core 返回了空响应"
                            );
                        }
                        return ResponseEntity.status(clientResponse.getStatusCode())
                                .body(responseBody);
                    });
            log.info(
                    "{} proxy completed status={} elapsedMs={}",
                    operation,
                    response.getStatusCode(),
                    elapsedMillis(startedAt)
            );
            return response;
        } catch (RestClientException ex) {
            long elapsedMs = elapsedMillis(startedAt);
            log.error(
                    "{} proxy failed elapsedMs={}: {}",
                    operation,
                    elapsedMs,
                    ex.getMessage()
            );
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(errorBody(
                            "AI_CORE_UNAVAILABLE",
                            "调用 AI Core 失败（" + elapsedMs + "ms）: "
                                    + ex.getMessage()
                    ));
        }
    }

    private ResponseEntity<JsonNode> getJson(String path, String operation) {
        long startedAt = System.nanoTime();
        try {
            ResponseEntity<JsonNode> response = aiCoreRunRestClient.get()
                    .uri(path)
                    .accept(MediaType.APPLICATION_JSON)
                    .exchange((clientRequest, clientResponse) -> {
                        JsonNode responseBody = clientResponse.bodyTo(JsonNode.class);
                        if (responseBody == null) {
                            responseBody = errorBody(
                                    "EMPTY_AI_CORE_RESPONSE",
                                    "AI Core 返回了空响应"
                            );
                        }
                        return ResponseEntity.status(clientResponse.getStatusCode())
                                .body(responseBody);
                    });
            log.info(
                    "{} proxy completed status={} elapsedMs={}",
                    operation,
                    response.getStatusCode(),
                    elapsedMillis(startedAt)
            );
            return response;
        } catch (RestClientException ex) {
            long elapsedMs = elapsedMillis(startedAt);
            log.error(
                    "{} proxy failed elapsedMs={}: {}",
                    operation,
                    elapsedMs,
                    ex.getMessage()
            );
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(errorBody(
                            "AI_CORE_UNAVAILABLE",
                            "调用 AI Core 失败（" + elapsedMs + "ms）: "
                                    + ex.getMessage()
                    ));
        }
    }

    private JsonNode errorBody(String code, String message) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("code", code);
        node.put("message", message);
        return node;
    }

    private static long elapsedMillis(long startedAt) {
        return TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt);
    }
}
