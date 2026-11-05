package com.vibeplatform.bff.architect;

import org.springframework.http.HttpStatusCode;

/** Architect SSE 建流前的上游错误，保留 AI Core 原始状态与响应体。 */
public class ArchitectStreamException extends RuntimeException {

    private final HttpStatusCode status;
    private final String body;

    public ArchitectStreamException(HttpStatusCode status, String body) {
        super("Architect AI Core stream failed: " + status);
        this.status = status;
        this.body = body;
    }

    public HttpStatusCode getStatus() {
        return status;
    }

    public String getBody() {
        return body;
    }
}
