package com.vibeplatform.bff.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
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
 * 沙盒生命周期代理：浏览器 -> Java BFF -> Python AI Core。
 *
 * <p>浏览器不直接访问 Python 的 internal 接口，避免内部 API 暴露到公网。
 * BFF 只做路径与状态透传，真实的容器销毁由 LocalDockerSandbox 执行。</p>
 */
@Service
public class SandboxService {

    private static final Logger log = LoggerFactory.getLogger(SandboxService.class);
    private static final String DESTROY_PATH = "/api/v1/internal/sandbox/{sandboxId}";

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;

    public SandboxService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
    }

    /**
     * 销毁沙盒。AI Core 对不存在的沙盒返回 404，BFF 原样透传，
     * 前端按幂等语义处理（重复关闭 / 刷新后的清理都安全）。
     */
    public ResponseEntity<JsonNode> destroy(String sandboxId) {
        try {
            ResponseEntity<JsonNode> response = aiCoreRunRestClient.delete()
                    .uri(DESTROY_PATH, sandboxId)
                    .accept(MediaType.APPLICATION_JSON)
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

            log.info("sandbox destroy proxy sandboxId={} status={}",
                    sandboxId, response.getStatusCode());
            return response;
        } catch (RestClientException ex) {
            log.error("sandbox destroy proxy failed sandboxId={}: {}",
                    sandboxId, ex.getMessage());
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(errorBody(
                            "AI_CORE_UNAVAILABLE",
                            "销毁沙盒失败: " + ex.getMessage()
                    ));
        }
    }

    private JsonNode errorBody(String code, String message) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("code", code);
        node.put("message", message);
        return node;
    }
}
