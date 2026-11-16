package com.vibeplatform.bff.common;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.http.codec.ServerSentEvent;

/** SSE 建连后出错也必须给出可解析的 terminal 事件。 */
class SseErrorFramesTest {

    private final ObjectMapper objectMapper = new ObjectMapper();

    @Test
    void terminalEventCarriesErrorEnvelope() throws Exception {
        ServerSentEvent<String> event = SseErrorFrames.terminal(
                objectMapper,
                "run_test",
                "run.failed",
                "STREAM_UPSTREAM_ERROR",
                "生成服务连接中断，请重试或稍后继续。"
        );

        assertThat(event.event()).isEqualTo("run.event");
        JsonNode root = objectMapper.readTree(event.data());
        assertThat(root.path("type").asText()).isEqualTo("run.failed");
        assertThat(root.path("runId").asText()).isEqualTo("run_test");
        JsonNode payload = root.path("payload");
        assertThat(payload.path("status").asText()).isEqualTo("FAILED");
        assertThat(payload.path("userMessage").asText())
                .isEqualTo("生成服务连接中断，请重试或稍后继续。");
        assertThat(payload.path("errorOptions").isArray()).isTrue();
        assertThat(payload.path("errorEnvelope").path("code").asText())
                .isEqualTo("STREAM_UPSTREAM_ERROR");
        assertThat(payload.path("errorEnvelope").path("traceId").asText())
                .startsWith("trc_");
    }
}
