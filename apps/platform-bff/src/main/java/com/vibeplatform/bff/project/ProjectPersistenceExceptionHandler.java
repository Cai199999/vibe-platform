package com.vibeplatform.bff.project;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vibeplatform.bff.project.ProjectRepository.ProjectException;
import jakarta.servlet.http.HttpServletRequest;
import java.util.UUID;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** 项目 / 契约持久化错误统一信封。 */
@Order(Ordered.HIGHEST_PRECEDENCE)
@RestControllerAdvice
public class ProjectPersistenceExceptionHandler {

    private final ObjectMapper objectMapper;

    public ProjectPersistenceExceptionHandler(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @ExceptionHandler(ProjectException.class)
    public ResponseEntity<ObjectNode> handle(
            ProjectException ex,
            HttpServletRequest request
    ) {
        HttpStatus status = "PROJECT_NOT_FOUND".equals(ex.code())
                ? HttpStatus.NOT_FOUND
                : HttpStatus.BAD_REQUEST;
        ObjectNode body = objectMapper.createObjectNode();
        body.put("code", ex.code());
        body.put("message", ex.getMessage());
        body.put("requestId", resolveRequestId(request));
        return ResponseEntity.status(status).body(body);
    }

    private static String resolveRequestId(HttpServletRequest request) {
        String header = request.getHeader("X-Request-Id");
        return header == null || header.isBlank()
                ? UUID.randomUUID().toString()
                : header;
    }
}
