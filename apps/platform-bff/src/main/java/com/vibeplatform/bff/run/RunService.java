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
