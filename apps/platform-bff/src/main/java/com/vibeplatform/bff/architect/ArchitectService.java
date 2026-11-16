package com.vibeplatform.bff.architect;

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

/** Architect 非流式接口代理：澄清回答、契约决策、取消与快照。 */
@Service
public class ArchitectService {

    private static final Logger log = LoggerFactory.getLogger(ArchitectService.class);
    private static final String RUN_PATH = "/api/v1/internal/architect/runs/{runId}";
    private static final String CLARIFICATION_ANSWER_PATH =
            "/api/v1/internal/architect/runs/{runId}/clarifications/"
                    + "{clarificationId}/answers";
    private static final String CONTRACT_DECISION_PATH =
            "/api/v1/internal/architect/runs/{runId}/contracts/"
                    + "{contractId}/decision";
    private static final String CONTRACT_CONFLICT_DECISION_PATH =
            "/api/v1/internal/architect/runs/{runId}/contract-conflicts/"
                    + "{conflictId}/decision";
    private static final String CANCEL_PATH =
            "/api/v1/internal/architect/runs/{runId}/cancel";
    private static final String HELPER_PATH =
            "/api/v1/internal/architect/runs/{runId}/helper";

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;

    public ArchitectService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
    }

    public ResponseEntity<JsonNode> answerClarification(
            String runId,
            String clarificationId,
            JsonNode body
    ) {
        return postJson(
                CLARIFICATION_ANSWER_PATH,
                body,
                "architect clarification answer",
                runId,
                clarificationId
        );
    }

    public ResponseEntity<JsonNode> decideContract(
            String runId,
            String contractId,
            JsonNode body
    ) {
        return postJson(
                CONTRACT_DECISION_PATH,
                body,
                "architect contract decision",
                runId,
                contractId
        );
    }

    /** 契约与 DDL 命名冲突：映射已有表 / 新建表 / 取消。 */
    public ResponseEntity<JsonNode> decideContractConflict(
            String runId,
            String conflictId,
            JsonNode body
    ) {
        return postJson(
                CONTRACT_CONFLICT_DECISION_PATH,
                body,
                "architect contract conflict decision",
                runId,
                conflictId
        );
    }

    public ResponseEntity<JsonNode> cancel(String runId, JsonNode body) {
        return postJson(CANCEL_PATH, body, "architect cancel", runId);
    }

    /** 影子助手只读问答；不改变主架构状态。 */
    public ResponseEntity<JsonNode> askHelper(String runId, JsonNode body) {
        return postJson(HELPER_PATH, body, "architect helper", runId);
    }

    public ResponseEntity<JsonNode> snapshot(String runId) {
        return getJson(RUN_PATH, "architect snapshot", runId);
    }

    private ResponseEntity<JsonNode> postJson(
            String path,
            JsonNode body,
            String operation,
            Object... uriVariables
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
                    .uri(path, uriVariables)
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
            log.error("{} proxy failed elapsedMs={}: {}",
                    operation, elapsedMs, ex.getMessage());
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(errorBody(
                            "AI_CORE_UNAVAILABLE",
                            "调用 AI Core 失败（" + elapsedMs + "ms）: "
                                    + ex.getMessage()
                    ));
        }
    }

    private ResponseEntity<JsonNode> getJson(
            String path,
            String operation,
            Object... uriVariables
    ) {
        long startedAt = System.nanoTime();
        try {
            ResponseEntity<JsonNode> response = aiCoreRunRestClient.get()
                    .uri(path, uriVariables)
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
            log.error("{} proxy failed elapsedMs={}: {}",
                    operation, elapsedMs, ex.getMessage());
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
