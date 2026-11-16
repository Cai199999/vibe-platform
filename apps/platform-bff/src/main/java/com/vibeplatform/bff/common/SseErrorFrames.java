package com.vibeplatform.bff.common;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.time.Instant;
import java.util.UUID;
import org.springframework.http.codec.ServerSentEvent;

/**
 * SSE 已建连后的统一 terminal 事件。
 *
 * <p>响应提交后不能再修改 HTTP 状态；若不主动发送终态，浏览器只会看到
 * 连接断开并反复重连。这里生成与 Python AI Core 同构的 ErrorEnvelope，
 * BFF 只做传输层兜底，不重新解释业务错误。</p>
 */
public final class SseErrorFrames {

    private SseErrorFrames() {
    }

    /** 建流前 HTTP 错误也使用同一信封，避免 BFF 自造裸错误体。 */
    public static String httpEnvelope(
            ObjectMapper objectMapper,
            String code,
            String userMessage
    ) {
        String traceId = "trc_" + UUID.randomUUID().toString().replace("-", "");
        return baseEnvelope(
                objectMapper, code, userMessage, traceId, "", true
        ).toString();
    }

    public static ServerSentEvent<String> terminal(
            ObjectMapper objectMapper,
            String runId,
            String terminalEventType,
            String code,
            String userMessage
    ) {
        String resolvedRunId = runId == null ? "" : runId;
        String traceId = "trc_" + UUID.randomUUID().toString().replace("-", "");
        ObjectNode event = objectMapper.createObjectNode();
        event.put("eventId", "evt_" + UUID.randomUUID().toString().replace("-", ""));
        event.put("runId", resolvedRunId);
        // 不推进 seq：该事件未写入 AI Core 缓冲区，使用 0 避免前端以
        // 不存在的 seq 重连。终态事件到达后前端会立即关闭连接。
        event.put("seq", 0);
        event.put("type", terminalEventType);
        event.put("version", 1);
        event.put("ts", Instant.now().toString());
        event.put("traceId", traceId);

        ObjectNode payload = event.putObject("payload");
        payload.set(
                "errorEnvelope",
                baseEnvelope(
                        objectMapper,
                        code,
                        userMessage,
                        traceId,
                        resolvedRunId,
                        true
                )
        );
        payload.put("runId", resolvedRunId);
        payload.put("errorCode", code);
        payload.put("userMessage", userMessage);
        payload.put("traceId", traceId);
        payload.put("retryable", true);
        payload.putArray("errorOptions");
        if ("fullstack.failed".equals(terminalEventType)) {
            payload.put("status", "FAILED");
            payload.put("error", userMessage);
        } else if ("architect.failed".equals(terminalEventType)) {
            payload.put("error", userMessage);
        } else {
            payload.put("status", "FAILED");
            payload.put("code", code);
            payload.put("message", userMessage);
        }

        return ServerSentEvent.<String>builder()
                .event("run.event")
                .data(event.toString())
                .build();
    }

    private static ObjectNode baseEnvelope(
            ObjectMapper objectMapper,
            String code,
            String userMessage,
            String traceId,
            String runId,
            boolean retryable
    ) {
        ObjectNode envelope = objectMapper.createObjectNode();
        envelope.put("code", code);
        envelope.put("message", userMessage);
        envelope.put("userMessage", userMessage);
        envelope.put("category", "TRANSPORT");
        envelope.put("severity", retryable ? "RECOVERABLE" : "FATAL");
        envelope.put("stage", "bff.sse");
        envelope.put("traceId", traceId);
        envelope.put("runId", runId == null ? "" : runId);
        envelope.put("retryable", retryable);
        envelope.put("degraded", false);
        envelope.putArray("errorOptions");
        ObjectNode detail = envelope.putObject("detail");
        detail.put("code", code);
        detail.put("message", userMessage);
        return envelope;
    }
}
