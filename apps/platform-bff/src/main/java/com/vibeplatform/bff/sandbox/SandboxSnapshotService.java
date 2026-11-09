package com.vibeplatform.bff.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.function.Supplier;
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
 * 项目快照代理：浏览器 -> Java BFF -> Python AI Core。
 *
 * <p>快照不引入额外数据库，完全复用沙盒内的 Git 仓库：
 * commit 即存档，reset --hard 即回滚。BFF 只做参数校验和状态透传。</p>
 */
@Service
public class SandboxSnapshotService {

    private static final Logger log = LoggerFactory.getLogger(SandboxSnapshotService.class);
    private static final String SNAPSHOTS_PATH =
            "/api/v1/internal/sandbox/{sandboxId}/snapshots";
    private static final String RESTORE_PATH =
            "/api/v1/internal/sandbox/{sandboxId}/snapshots/{commitHash}/restore";
    private static final String DIFF_PATH =
            "/api/v1/internal/sandbox/{sandboxId}/snapshots/{commitHash}/diff?mode={mode}";

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;

    public SandboxSnapshotService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
    }

    /** 创建快照：AI Core 执行 git commit 并返回 commit hash。 */
    public ResponseEntity<JsonNode> create(
            String sandboxId,
            String message,
            String projectId,
            String tenantId,
            String latestChanges
    ) {
        ObjectNode body = objectMapper.createObjectNode();
        body.put("message", message);
        // 上下文记忆作用域：旧客户端不传时 Python 侧回退到 sandbox 作用域。
        if (projectId != null && !projectId.isBlank()) {
            body.put("projectId", projectId.trim());
        }
        if (tenantId != null && !tenantId.isBlank()) {
            body.put("tenantId", tenantId.trim());
        }
        // 本次快照的改动说明：即使尚无 local_summary，也能据此生成全局摘要。
        if (latestChanges != null && !latestChanges.isBlank()) {
            body.put("latestChanges", latestChanges.trim());
        }
        return proxy("create", sandboxId, () -> aiCoreRunRestClient.post()
                .uri(SNAPSHOTS_PATH, sandboxId)
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.APPLICATION_JSON)
                .body(body)
                .exchange((clientRequest, clientResponse) -> relayBody(
                        clientResponse.getStatusCode(),
                        clientResponse.bodyTo(JsonNode.class))));
    }

    /** 按时间倒序列出快照。 */
    public ResponseEntity<JsonNode> list(String sandboxId, int limit) {
        return proxy("list", sandboxId, () -> aiCoreRunRestClient.get()
                .uri(SNAPSHOTS_PATH + "?limit={limit}", sandboxId, limit)
                .accept(MediaType.APPLICATION_JSON)
                .exchange((clientRequest, clientResponse) -> relayBody(
                        clientResponse.getStatusCode(),
                        clientResponse.bodyTo(JsonNode.class))));
    }

    /** 硬回滚到指定快照。 */
    public ResponseEntity<JsonNode> restore(String sandboxId, String commitHash) {
        return proxy("restore", sandboxId, () -> aiCoreRunRestClient.post()
                .uri(RESTORE_PATH, sandboxId, commitHash)
                .accept(MediaType.APPLICATION_JSON)
                .exchange((clientRequest, clientResponse) -> relayBody(
                        clientResponse.getStatusCode(),
                        clientResponse.bodyTo(JsonNode.class))));
    }

    /** 快照只读视图的 unified diff。 */
    public ResponseEntity<JsonNode> diff(String sandboxId, String commitHash, String mode) {
        return proxy("diff", sandboxId, () -> aiCoreRunRestClient.get()
                .uri(DIFF_PATH, sandboxId, commitHash, mode)
                .accept(MediaType.APPLICATION_JSON)
                .exchange((clientRequest, clientResponse) -> relayBody(
                        clientResponse.getStatusCode(),
                        clientResponse.bodyTo(JsonNode.class))));
    }

    /** 统一透传 AI Core 的状态码与响应体；连接失败映射为 503。 */
    private ResponseEntity<JsonNode> proxy(
            String action,
            String sandboxId,
            Supplier<ResponseEntity<JsonNode>> call
    ) {
        try {
            ResponseEntity<JsonNode> response = call.get();
            log.info("snapshot {} proxy sandboxId={} status={}",
                    action, sandboxId, response.getStatusCode());
            return response;
        } catch (RestClientException ex) {
            log.error("snapshot {} proxy failed sandboxId={}: {}",
                    action, sandboxId, ex.getMessage());
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(errorBody(
                            "AI_CORE_UNAVAILABLE",
                            "快照服务暂不可用: " + ex.getMessage()
                    ));
        }
    }

    private ResponseEntity<JsonNode> relayBody(HttpStatusCode status, JsonNode body) {
        if (body == null) {
            body = errorBody("EMPTY_AI_CORE_RESPONSE", "AI Core 返回了空响应");
        }
        return ResponseEntity.status(status).body(body);
    }

    private JsonNode errorBody(String code, String message) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("code", code);
        node.put("message", message);
        return node;
    }
}
