package com.vibeplatform.bff.directops;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
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

/** Direct Ops 非流式代理：只透传信封与状态码。 */
@Service
public class DirectOpsService {

    private static final Logger log = LoggerFactory.getLogger(DirectOpsService.class);
    private static final String ACTIONS_PATH = "/api/v1/internal/direct-ops/actions";
    private static final String ANALYZE_DELETE_PATH =
            "/api/v1/internal/direct-ops/analyze-delete";
    private static final String UNDO_PATH = "/api/v1/internal/direct-ops/undo";
    private static final String REVISION_PATH = "/api/v1/internal/direct-ops/revision";

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;

    public DirectOpsService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
    }

    public ResponseEntity<JsonNode> execute(JsonNode body) {
        return postJson(ACTIONS_PATH, body, "direct ops");
    }

    public ResponseEntity<JsonNode> analyzeDelete(JsonNode body) {
        return postJson(ANALYZE_DELETE_PATH, body, "direct ops delete analysis");
    }

    public ResponseEntity<JsonNode> undo(JsonNode body) {
        return postJson(UNDO_PATH, body, "direct ops undo");
    }

    private ResponseEntity<JsonNode> postJson(
            String path,
            JsonNode body,
            String operation
    ) {
        long startedAt = System.nanoTime();
        final String jsonBody;
        try {
            jsonBody = objectMapper.writeValueAsString(body == null ? objectMapper.createObjectNode() : body);
        } catch (JsonProcessingException ex) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(errorBody(
                    "REQUEST_SERIALIZATION_FAILED",
                    "Direct Ops 请求序列化失败"
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
            log.info("direct ops proxy status={} elapsedMs={}",
                    response.getStatusCode(), elapsedMillis(startedAt));
            return response;
        } catch (RestClientException ex) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(errorBody(
                    "AI_CORE_UNAVAILABLE",
                    "调用 AI Core 失败（" + elapsedMillis(startedAt) + "ms）"
            ));
        }
    }

    public ResponseEntity<JsonNode> revision(String projectId, String sandboxId) {
        long startedAt = System.nanoTime();
        try {
            ResponseEntity<JsonNode> response = aiCoreRunRestClient.get()
                    .uri(uriBuilder -> uriBuilder
                            .path(REVISION_PATH)
                            .queryParam("projectId", projectId)
                            .queryParam("sandboxId", sandboxId)
                            .build())
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
            log.info("direct ops revision status={} elapsedMs={}",
                    response.getStatusCode(), elapsedMillis(startedAt));
            return response;
        } catch (RestClientException ex) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(errorBody(
                    "AI_CORE_UNAVAILABLE",
                    "调用 AI Core 失败（" + elapsedMillis(startedAt) + "ms）"
            ));
        }
    }

    private JsonNode errorBody(String code, String message) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("code", code);
        node.put("message", message);
        node.put("userMessage", message);
        node.put("category", "TRANSPORT");
        node.put("severity", "RECOVERABLE");
        node.put("traceId", "trc_"
                + java.util.UUID.randomUUID().toString().replace("-", ""));
        node.put("retryable", false);
        node.put("degraded", false);
        node.putArray("errorOptions");
        ObjectNode detail = node.putObject("detail");
        detail.put("code", code);
        detail.put("message", message);
        return node;
    }

    private static long elapsedMillis(long startedAt) {
        return TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt);
    }
}
