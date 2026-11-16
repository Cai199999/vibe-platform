package com.vibeplatform.bff.run;

import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 只处理建立 SSE 流之前的 AI Core 错误。
 *
 * <p>流一旦开始，响应已提交，不能再切换 HTTP 状态；此时前端依赖
 * {@code afterSeq} 重连恢复。</p>
 */
@Order(Ordered.HIGHEST_PRECEDENCE)
@RestControllerAdvice(assignableTypes = RunController.class)
public class RunStreamExceptionHandler {

    @ExceptionHandler(AiCoreStreamException.class)
    public ResponseEntity<String> handle(AiCoreStreamException ex) {
        return ResponseEntity.status(ex.getStatus())
                .contentType(MediaType.APPLICATION_JSON)
                .body(ex.getBody());
    }
}
