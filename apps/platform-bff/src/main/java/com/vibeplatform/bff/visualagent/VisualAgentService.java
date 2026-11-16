package com.vibeplatform.bff.visualagent;

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

/** VisualAgent 非流式代理：只透传 JSON 信封与状态码。 */
@Service
public class VisualAgentService {

    private static final Logger log = LoggerFactory.getLogger(VisualAgentService.class);
    private static final String NODES_PATH =
            "/api/v1/internal/visual-agent/nodes";

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;

    public VisualAgentService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
    }

    public ResponseEntity<JsonNode> generateNodes(JsonNode body) {
        long startedAt = System.nanoTime();
        final String jsonBody;
        try {
            jsonBody = objectMapper.writeValueAsString(
                    body == null ? objectMapper.createObjectNode() : body
            );
        } catch (JsonProcessingException ex) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(errorBody(
                    "REQUEST_SERIALIZATION_FAILED",
                    "VisualAgent 请求序列化失败"
            ));
        }
        try {
            ResponseEntity<JsonNode> response = aiCoreRunRestClient.post()
                    .uri(NODES_PATH)
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
            log.info("visual agent nodes status={} elapsedMs={}",
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
