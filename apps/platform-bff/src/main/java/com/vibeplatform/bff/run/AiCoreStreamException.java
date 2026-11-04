package com.vibeplatform.bff.run;

import org.springframework.http.HttpStatusCode;

/**
 * 建立 SSE 流之前的上游错误。
 *
 * <p>保存 AI Core 原始状态码与 JSON body，由
 * {@link RunStreamExceptionHandler} 原样返回给前端，避免 BFF 重新定义
 * 第二套错误码。流已建立后的错误只关闭连接。</p>
 */
public class AiCoreStreamException extends RuntimeException {

    private final HttpStatusCode status;
    private final String body;

    public AiCoreStreamException(HttpStatusCode status, String body) {
        super("AI Core stream request failed: " + status);
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
