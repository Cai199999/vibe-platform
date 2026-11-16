package com.vibeplatform.bff.common;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import jakarta.servlet.http.HttpServletRequest;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * BFF 全局异常边界：只做“透传 + 统一信封”，不改写业务语义。
 *
 * <p>流式接口在建立连接后不能依赖本处理器（响应已提交），由
 * {@link SseErrorFrames} 发送 terminal 事件。这里仅兜底非流式接口与
 * 建流前异常。响应中禁止出现堆栈、绝对路径和上游技术消息。</p>
 */
@RestControllerAdvice
@Order(Ordered.LOWEST_PRECEDENCE)
public class GlobalExceptionHandler {

    private static final Logger log =
            LoggerFactory.getLogger(GlobalExceptionHandler.class);
    private static final String TRACE_HEADER = "X-Trace-Id";

    private final ObjectMapper objectMapper;

    public GlobalExceptionHandler(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    /** 请求体字段校验失败：返回字段级提示，不重试。 */
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ObjectNode> handleValidation(
            MethodArgumentNotValidException ex,
            HttpServletRequest request
    ) {
        String traceId = traceId(request);
        ObjectNode body = envelope(
                "REQUEST_VALIDATION_FAILED",
                "提交内容不完整或格式不正确，请检查后重试。",
                "INPUT",
                "FATAL",
                traceId
        );
        ArrayNode fields = body.putObject("diagnostic").putArray("fields");
        for (FieldError error : ex.getBindingResult().getFieldErrors()) {
            ObjectNode field = fields.addObject();
            field.put("loc", error.getField());
            field.put("message", truncate(error.getDefaultMessage(), 200));
        }
        return response(HttpStatus.UNPROCESSABLE_ENTITY, body, traceId);
    }

    /** 请求体无法解析。 */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<ObjectNode> handleUnreadable(
            HttpMessageNotReadableException ex,
            HttpServletRequest request
    ) {
        String traceId = traceId(request);
        return response(
                HttpStatus.BAD_REQUEST,
                envelope(
                        "REQUEST_BODY_INVALID",
                        "请求内容无法解析，请刷新页面后重试。",
                        "INPUT",
                        "FATAL",
                        traceId
                ),
                traceId
        );
    }

    /** 未预期异常：记录诊断编号，返回通用业务文案。 */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<ObjectNode> handleUnhandled(
            Exception ex,
            HttpServletRequest request
    ) {
        String traceId = traceId(request);
        // 只记录类型与消息，不打印堆栈；完整诊断由日志平台按 traceId 聚合。
        log.error(
                "bff unhandled exception traceId={} type={} message={}",
                traceId,
                ex.getClass().getSimpleName(),
                truncate(ex.getMessage(), 500)
        );
        return response(
                HttpStatus.INTERNAL_SERVER_ERROR,
                envelope(
                        "BFF_INTERNAL_ERROR",
                        "服务暂时不可用，已记录诊断编号，请稍后重试。",
                        "INTERNAL",
                        "FATAL",
                        traceId
                ),
                traceId
        );
    }

    private static ResponseEntity<ObjectNode> response(
            HttpStatus status,
            ObjectNode body,
            String traceId
    ) {
        return ResponseEntity.status(status)
                .header(TRACE_HEADER, traceId)
                .body(body);
    }

    private ObjectNode envelope(
            String code,
            String userMessage,
            String category,
            String severity,
            String traceId
    ) {
        ObjectNode root = objectMapper.createObjectNode();
        root.put("code", code);
        // message 为旧前端兼容字段；新前端统一读 userMessage。
        root.put("message", userMessage);
        root.put("userMessage", userMessage);
        root.put("category", category);
        root.put("severity", severity);
        root.put("stage", "bff");
        root.put("traceId", traceId);
        root.put("runId", "");
        root.put("retryable", false);
        root.put("degraded", false);
        root.putArray("errorOptions");
        ObjectNode detail = root.putObject("detail");
        detail.put("code", code);
        detail.put("message", userMessage);
        return root;
    }

    private static String traceId(HttpServletRequest request) {
        String header = request.getHeader(TRACE_HEADER);
        if (header != null && !header.isBlank()) {
            return header.length() > 80 ? header.substring(0, 80) : header;
        }
        return "trc_" + UUID.randomUUID().toString().replace("-", "");
    }

    private static String truncate(String value, int max) {
        if (value == null) {
            return "";
        }
        return value.length() <= max ? value : value.substring(0, max);
    }
}
