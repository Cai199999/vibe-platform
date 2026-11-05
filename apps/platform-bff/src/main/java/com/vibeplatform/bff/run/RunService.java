package com.vibeplatform.bff.run;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * Run 代理服务：Java BFF -> Python AI Core。
 *
 * <p>M1 使用同步代理：前端 loading 等待，BFF 线程阻塞到 AI Core 返回。
 * M2 改为接收 202 后通过事件流推送状态，BFF 不再占用长连接线程。</p>
 */
@Service
public class RunService {

    private static final Logger log = LoggerFactory.getLogger(RunService.class);
    private static final String EXECUTE_PATH = "/api/v1/internal/runs/execute";
    private static final String ROLLBACK_PATH = "/api/v1/internal/runs/rollback";
    private static final String RUN_PATH = "/api/v1/internal/runs/{runId}";
    private static final String CLARIFICATIONS_PATH =
            "/api/v1/internal/runs/{runId}/clarifications";
    private static final String CLARIFICATION_ANSWER_PATH =
            "/api/v1/internal/runs/{runId}/clarifications/{clarificationId}/answers";
    private static final String APPROVALS_PATH =
            "/api/v1/internal/runs/{runId}/approvals";
    private static final String APPROVAL_DECISION_PATH =
            "/api/v1/internal/runs/{runId}/approvals/{approvalId}/decision";
    private static final String CANCEL_PATH = "/api/v1/internal/runs/{runId}/cancel";

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;
    private final RunSandboxRegistry runSandboxRegistry;

    public RunService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper,
            RunSandboxRegistry runSandboxRegistry
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
        this.runSandboxRegistry = runSandboxRegistry;
    }

    /**
     * 调用 AI Core 执行 Run，并原样透传其 HTTP 状态与 JSON 响应。
     *
     * <p>AI Core 对业务失败（构建重试耗尽）返回 200 + status=FAILED，
     * 对请求/沙盒错误返回 4xx/5xx；BFF 不重新解释这些语义，避免前端
     * 需要理解两套错误模型。</p>
     */
    public ResponseEntity<JsonNode> execute(CreateRunRequest request) {
        long startedAt = System.nanoTime();

        // 显式序列化再以 String 发送：
        // 1) 保证 AI Core 一定收到 JSON 请求体（避免 POJO 转换器差异）；
        // 2) 便于记录请求体大小，排查跨语言契约问题。
        final String jsonBody;
        try {
            jsonBody = objectMapper.writeValueAsString(request);
        } catch (JsonProcessingException ex) {
            log.error("failed to serialize run request: {}", ex.getMessage());
            return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                    .body(errorBody("RUN_REQUEST_SERIALIZATION_FAILED", "Run 请求序列化失败"));
        }

        try {
            ResponseEntity<JsonNode> response = aiCoreRunRestClient.post()
                    .uri(EXECUTE_PATH)
                    .contentType(MediaType.APPLICATION_JSON)
                    .accept(MediaType.APPLICATION_JSON)
                    .body(jsonBody)
                    .exchange((clientRequest, clientResponse) -> {
                        HttpStatusCode status = clientResponse.getStatusCode();
                        JsonNode body = clientResponse.bodyTo(JsonNode.class);
                        if (body == null) {
                            body = errorBody(
                                    "EMPTY_AI_CORE_RESPONSE",
                                    "AI Core 返回了空响应"
                            );
                        }
                        return ResponseEntity.status(status).body(body);
                    });

            // 记录 runId -> sandboxId，供后续导出接口只凭 runId 定位沙盒。
            JsonNode body = response.getBody();
            if (body != null) {
                String runId = body.path("runId").asText(null);
                String sandboxId = body.path("sandboxId").asText(null);
                runSandboxRegistry.record(runId, sandboxId);
            }

            log.info(
                    "run proxy completed requestBytes={} status={} elapsedMs={}",
                    jsonBody.length(),
                    response.getStatusCode(),
                    elapsedMillis(startedAt)
            );
            return response;
        } catch (RestClientException ex) {
            long elapsedMs = elapsedMillis(startedAt);
            log.error("run proxy failed elapsedMs={}: {}", elapsedMs, ex.getMessage());
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(errorBody(
                            "AI_CORE_UNAVAILABLE",
                            "调用 AI Core 失败（" + elapsedMs + "ms）: " + ex.getMessage()
                    ));
        }
    }

    /**
     * 代理单标注回滚：前端 -> BFF -> AI Core -> 沙盒 git reset --hard。
     *
     * <p>与 execute 一样保留 AI Core 的 HTTP 语义：commit 不存在 / checkpoint
     * 不匹配返回 4xx，前端据此保留原有聊天记录并提示失败。</p>
     */
    public ResponseEntity<JsonNode> rollback(RollbackRunRequest request) {
        long startedAt = System.nanoTime();
        final String jsonBody;
        try {
            jsonBody = objectMapper.writeValueAsString(request);
        } catch (JsonProcessingException ex) {
            log.error("failed to serialize rollback request: {}", ex.getMessage());
            return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                    .body(errorBody(
                            "ROLLBACK_REQUEST_SERIALIZATION_FAILED",
                            "回滚请求序列化失败"
                    ));
        }

        try {
            ResponseEntity<JsonNode> response = aiCoreRunRestClient.post()
                    .uri(ROLLBACK_PATH)
                    .contentType(MediaType.APPLICATION_JSON)
                    .accept(MediaType.APPLICATION_JSON)
                    .body(jsonBody)
                    .exchange((clientRequest, clientResponse) -> {
                        JsonNode body = clientResponse.bodyTo(JsonNode.class);
                        if (body == null) {
                            body = errorBody(
                                    "EMPTY_AI_CORE_RESPONSE",
                                    "AI Core 返回了空回滚响应"
                            );
                        }
                        return ResponseEntity.status(clientResponse.getStatusCode()).body(body);
                    });

            log.info(
                    "rollback proxy completed threadId={} status={} elapsedMs={}",
                    request.threadId(),
                    response.getStatusCode(),
                    elapsedMillis(startedAt)
            );
            return response;
        } catch (RestClientException ex) {
            long elapsedMs = elapsedMillis(startedAt);
            log.error("rollback proxy failed elapsedMs={}: {}", elapsedMs, ex.getMessage());
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(errorBody(
                            "AI_CORE_UNAVAILABLE",
                            "调用 AI Core 回滚失败（" + elapsedMs + "ms）: "
                                    + ex.getMessage()
                    ));
        }
    }

    /**
     * 提交澄清答案并后台恢复状态机，AI Core 返回 202。
     *
     * <p>答案接口不持有长连接；前端随后用 lastSeq 重放事件流。</p>
     */
    public ResponseEntity<JsonNode> answerClarification(
            String runId,
            String clarificationId,
            JsonNode body
    ) {
        return postJson(
                CLARIFICATION_ANSWER_PATH,
                body,
                "clarification answer",
                runId,
                clarificationId
        );
    }

    public ResponseEntity<JsonNode> getRun(String runId) {
        return getJson(RUN_PATH, "run snapshot", runId);
    }

    public ResponseEntity<JsonNode> getClarifications(String runId) {
        return getJson(CLARIFICATIONS_PATH, "clarifications", runId);
    }

    /**
     * 代理 Diff 审批决策；保持 AI Core 的 4xx 语义（重复决策 / 过期）。
     */
    public ResponseEntity<JsonNode> decideApproval(
            String runId,
            String approvalId,
            JsonNode body
    ) {
        return postJson(
                APPROVAL_DECISION_PATH,
                body,
                "approval decision",
                runId,
                approvalId
        );
    }

    public ResponseEntity<JsonNode> getApprovals(String runId) {
        return getJson(APPROVALS_PATH, "approvals", runId);
    }

    /**
     * 代理取消 Run；暂停中的 Run 会立即回滚并在事件流中收到终态。
     */
    public ResponseEntity<JsonNode> cancelRun(String runId, JsonNode body) {
        return postJson(CANCEL_PATH, body, "run cancel", runId);
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
                    .body(errorBody("REQUEST_SERIALIZATION_FAILED",
                            operation + " 请求序列化失败"));
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
