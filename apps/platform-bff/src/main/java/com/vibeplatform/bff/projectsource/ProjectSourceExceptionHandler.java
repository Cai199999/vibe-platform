package com.vibeplatform.bff.projectsource;

import java.util.LinkedHashMap;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 源码持久化错误信封。
 *
 * <p>只暴露稳定错误码与可操作文案；MySQL / MinIO 的底层异常只进日志。</p>
 */
@RestControllerAdvice
@Order(Ordered.HIGHEST_PRECEDENCE)
public class ProjectSourceExceptionHandler {

    private static final Logger log =
            LoggerFactory.getLogger(ProjectSourceExceptionHandler.class);

    @ExceptionHandler(ProjectSourceException.class)
    public ResponseEntity<Map<String, Object>> handle(ProjectSourceException ex) {
        if (ex.status() >= 500) {
            log.error(
                    "project source storage failed code={} message={}",
                    ex.code(),
                    ex.getMessage(),
                    ex.getCause()
            );
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", ex.code());
        body.put("message", ex.getMessage());
        body.put("userMessage", ex.getMessage());
        body.put("retryable", ex.retryable());
        return ResponseEntity.status(ex.status()).body(body);
    }
}
