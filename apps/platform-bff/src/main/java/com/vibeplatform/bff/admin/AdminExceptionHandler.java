package com.vibeplatform.bff.admin;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import jakarta.servlet.http.HttpServletRequest;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 管理端统一错误结构。
 *
 * <p>与设计文档 §1.4 对齐：错误体包含 {@code code/message/requestId}，
 * 不向前端泄露角色列表等内部细节。</p>
 */
@Order(Ordered.HIGHEST_PRECEDENCE)
@RestControllerAdvice
public class AdminExceptionHandler {

    private static final Logger log =
            LoggerFactory.getLogger(AdminExceptionHandler.class);

    private final ObjectMapper objectMapper;

    public AdminExceptionHandler(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @ExceptionHandler(AdminAccessDeniedException.class)
    public ResponseEntity<ObjectNode> handleDenied(
            AdminAccessDeniedException ex,
            HttpServletRequest request
    ) {
        String requestId = resolveRequestId(request);
        log.warn("admin access denied role={} path={} requestId={}",
                ex.role(), request.getRequestURI(), requestId);

        ObjectNode body = objectMapper.createObjectNode();
        body.put("code", "ADMIN_ROLE_REQUIRED");
        body.put("message", "需要管理员权限，请使用管理员身份访问 /admin");
        body.put("requestId", requestId);
        return ResponseEntity.status(HttpStatus.FORBIDDEN).body(body);
    }

    /**
     * 指标代理失败必须显式报 502，禁止返回 200 + 全零数据：后者会让
     * 运维把“AI Core 挂了”误判成“系统健康且零拦截”。
     */
    @ExceptionHandler(FullstackMetricsUnavailableException.class)
    public ResponseEntity<ObjectNode> handleMetricsUnavailable(
            FullstackMetricsUnavailableException ex,
            HttpServletRequest request
    ) {
        String requestId = resolveRequestId(request);
        log.warn("admin fullstack metrics unavailable path={} requestId={}: {}",
                request.getRequestURI(), requestId, ex.getMessage());

        ObjectNode body = objectMapper.createObjectNode();
        body.put("code", "AI_CORE_METRICS_UNAVAILABLE");
        body.put("message", "AI Core 全栈指标暂不可用，请稍后重试");
        body.put("requestId", requestId);
        return ResponseEntity.status(HttpStatus.BAD_GATEWAY).body(body);
    }

    private String resolveRequestId(HttpServletRequest request) {
        String header = request.getHeader("X-Request-Id");
        return header == null || header.isBlank()
                ? UUID.randomUUID().toString()
                : header;
    }
}
