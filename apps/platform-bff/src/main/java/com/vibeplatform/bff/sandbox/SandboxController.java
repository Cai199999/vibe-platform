package com.vibeplatform.bff.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.regex.Pattern;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 前端可调用的沙盒管理入口。
 */
@RestController
@RequestMapping("/api/v1/sandboxes")
public class SandboxController {

    // 沙盒 ID 由 Python 生成（sbx_xxx），只允许安全字符，避免路径注入。
    private static final Pattern SANDBOX_ID_PATTERN = Pattern.compile("[A-Za-z0-9_-]{1,80}");

    private final SandboxService sandboxService;
    private final ObjectMapper objectMapper;

    public SandboxController(SandboxService sandboxService, ObjectMapper objectMapper) {
        this.sandboxService = sandboxService;
        this.objectMapper = objectMapper;
    }

    /**
     * 关闭项目时销毁对应沙盒容器，释放内存、CPU 与随机端口。
     * 由 BFF 调用 Python 的 DELETE /api/v1/internal/sandbox/{sandboxId}。
     */
    @DeleteMapping("/{sandboxId}")
    public ResponseEntity<JsonNode> destroy(@PathVariable String sandboxId) {
        if (!SANDBOX_ID_PATTERN.matcher(sandboxId).matches()) {
            ObjectNode body = objectMapper.createObjectNode();
            body.put("code", "INVALID_SANDBOX_ID");
            body.put("message", "非法的 sandboxId");
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(body);
        }
        return sandboxService.destroy(sandboxId);
    }
}
