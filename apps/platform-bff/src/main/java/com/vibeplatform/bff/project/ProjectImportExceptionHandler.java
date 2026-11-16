package com.vibeplatform.bff.project;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.multipart.MaxUploadSizeExceededException;

/**
 * 上传超限错误：Spring 在进入 Controller 前就会拒绝超大请求，
 * 这里统一转换为与其它导入错误一致的结构。
 */
@Order(Ordered.HIGHEST_PRECEDENCE)
@RestControllerAdvice
public class ProjectImportExceptionHandler {

    private final ObjectMapper objectMapper;

    public ProjectImportExceptionHandler(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    /**
     * multipart 超过 spring.servlet.multipart.max-file-size 时返回 413。
     */
    @ExceptionHandler(MaxUploadSizeExceededException.class)
    public ResponseEntity<JsonNode> handleMaxUploadSize(MaxUploadSizeExceededException ex) {
        ObjectNode body = objectMapper.createObjectNode();
        body.put("code", "IMPORT_FILE_TOO_LARGE");
        body.put("message", "上传文件超过大小限制");
        return ResponseEntity.status(HttpStatus.PAYLOAD_TOO_LARGE).body(body);
    }
}
