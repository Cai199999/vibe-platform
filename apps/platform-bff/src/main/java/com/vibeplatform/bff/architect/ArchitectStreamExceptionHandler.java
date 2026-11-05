package com.vibeplatform.bff.architect;

import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** 只处理 Architect SSE 建流前的 AI Core 错误。 */
@RestControllerAdvice(assignableTypes = ArchitectController.class)
public class ArchitectStreamExceptionHandler {

    @ExceptionHandler(ArchitectStreamException.class)
    public ResponseEntity<String> handle(ArchitectStreamException ex) {
        return ResponseEntity.status(ex.getStatus())
                .contentType(MediaType.APPLICATION_JSON)
                .body(ex.getBody());
    }
}
