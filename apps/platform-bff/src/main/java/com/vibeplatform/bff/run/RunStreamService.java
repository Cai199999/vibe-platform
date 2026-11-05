package com.vibeplatform.bff.run;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vibeplatform.bff.admin.AdminMetricsService;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Service;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.reactive.function.client.WebClientRequestException;
import reactor.core.publisher.Flux;

/**
 * SSE 透传：Java BFF -> Python AI Core。
 *
 * <p>使用 WebClient（Reactor Netty）而不是 RestClient：BFF 不阻塞 Servlet
 * 线程等待整个 Run，只把上游的 {@code id/event/data} 帧转发给浏览器。
 * seq 由 Python 唯一分配，BFF 不重新编号、不聚合。</p>
 */
@Service
public class RunStreamService {

    private static final Logger log = LoggerFactory.getLogger(RunStreamService.class);
    private static final String STREAM_START_PATH =
            "/api/v1/internal/runs/execute/stream";
    private static final String STREAM_EVENTS_PATH =
            "/api/v1/internal/runs/{runId}/events/stream";
    private static final ParameterizedTypeReference<ServerSentEvent<String>> SSE_TYPE =
            new ParameterizedTypeReference<>() {
            };

    private final WebClient aiCoreStreamWebClient;
    private final ObjectMapper objectMapper;
    private final RunSandboxRegistry runSandboxRegistry;
    private final AdminMetricsService adminMetricsService;

    public RunStreamService(
            @Qualifier("aiCoreStreamWebClient") WebClient aiCoreStreamWebClient,
            ObjectMapper objectMapper,
            RunSandboxRegistry runSandboxRegistry,
            AdminMetricsService adminMetricsService
    ) {
        this.aiCoreStreamWebClient = aiCoreStreamWebClient;
        this.objectMapper = objectMapper;
        this.runSandboxRegistry = runSandboxRegistry;
        this.adminMetricsService = adminMetricsService;
    }

    /**
     * 启动 Run 并直接返回上游 SSE。
     */
    public Flux<ServerSentEvent<String>> start(CreateRunRequest request) {
        final String jsonBody;
        try {
            jsonBody = objectMapper.writeValueAsString(request);
        } catch (Exception ex) {
            return Flux.error(new AiCoreStreamException(
                    HttpStatus.BAD_REQUEST,
                    errorJson(
                            "RUN_REQUEST_SERIALIZATION_FAILED",
                            "Run 请求序列化失败: " + ex.getMessage()
                    )
            ));
        }

        long startedAt = System.nanoTime();
        return aiCoreStreamWebClient.post()
                .uri(STREAM_START_PATH)
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM)
                .bodyValue(jsonBody)
                .exchangeToFlux(response -> {
                    if (response.statusCode().isError()) {
                        return response.bodyToMono(String.class)
                                .defaultIfEmpty("{}")
                                .flatMapMany(body -> Flux.error(
                                        new AiCoreStreamException(
                                                response.statusCode(), body
                                        )
                                ));
                    }
                    return response.bodyToFlux(SSE_TYPE);
                })
                .doOnNext(this::observeEvent)
                .doOnComplete(() -> log.info(
                        "run stream completed elapsedMs={}",
                        elapsedMillis(startedAt)
                ))
                .onErrorMap(
                        WebClientRequestException.class,
                        ex -> new AiCoreStreamException(
                                HttpStatus.BAD_GATEWAY,
                                errorJson(
                                        "STREAM_UPSTREAM_ERROR",
                                        "连接 AI Core 事件流失败: " + ex.getMessage()
                                )
                        )
                );
    }

    /**
     * 断线重放：先回放 {@code seq > afterSeq}，再继续订阅。
     */
    public Flux<ServerSentEvent<String>> replay(
            String runId,
            Integer afterSeq,
            String lastEventId
    ) {
        int effectiveAfterSeq = afterSeq == null ? 0 : Math.max(0, afterSeq);
        if (effectiveAfterSeq == 0 && lastEventId != null && !lastEventId.isBlank()) {
            try {
                effectiveAfterSeq = Math.max(0, Integer.parseInt(lastEventId));
            } catch (NumberFormatException ignored) {
                effectiveAfterSeq = 0;
            }
        }
        final int resumeFrom = effectiveAfterSeq;
        return aiCoreStreamWebClient.get()
                .uri(uriBuilder -> uriBuilder
                        .path(STREAM_EVENTS_PATH)
                        .queryParam("afterSeq", resumeFrom)
                        .build(runId))
                .accept(MediaType.TEXT_EVENT_STREAM)
                .exchangeToFlux(response -> {
                    if (response.statusCode().isError()) {
                        return response.bodyToMono(String.class)
                                .defaultIfEmpty("{}")
                                .flatMapMany(body -> Flux.error(
                                        new AiCoreStreamException(
                                                response.statusCode(), body
                                        )
                                ));
                    }
                    return response.bodyToFlux(SSE_TYPE);
                })
                .doOnNext(this::observeEvent)
                .onErrorMap(
                        WebClientRequestException.class,
                        ex -> new AiCoreStreamException(
                                HttpStatus.BAD_GATEWAY,
                                errorJson(
                                        "STREAM_UPSTREAM_ERROR",
                                        "连接 AI Core 事件流失败: " + ex.getMessage()
                                )
                        )
                );
    }

    /**
     * 从事件中提取 Run 生命周期指标与 sandboxId。
     *
     * <p>本服务只处理 UI Graph 的 Run，因此事件流兜底归属为
     * {@code ui_graph}；Supervisor dispatch 会在此之前写入权威 Agent ID。
     * 只在 payload.sandboxId 是文本时覆盖，避免 run.started 的 null 把之前
     * 已记录的 ID 抹掉。</p>
     */
    private void observeEvent(ServerSentEvent<String> event) {
        String data = event.data();
        if (data == null || data.isBlank()) {
            return;
        }
        try {
            JsonNode node = objectMapper.readTree(data);
            JsonNode runIdNode = node.get("runId");
            if (runIdNode == null || !runIdNode.isTextual()) {
                return;
            }
            String runId = runIdNode.asText();
            JsonNode payload = node.path("payload");

            JsonNode sandboxNode = payload.get("sandboxId");
            if (sandboxNode != null && sandboxNode.isTextual()) {
                runSandboxRegistry.record(runId, sandboxNode.asText());
            }

            adminMetricsService.observeEvent(
                    runId,
                    text(node, "type"),
                    parseTimestamp(text(node, "ts")),
                    payload,
                    AdminMetricsService.UI_AGENT_ID
            );
        } catch (Exception ex) {
            // 事件数据异常不能中断转发；下游仍按 seq 重连恢复。
            log.warn("failed to parse run event for metrics: {}", ex.getMessage());
        }
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value != null && value.isTextual() ? value.asText() : null;
    }

    private static Instant parseTimestamp(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        try {
            return Instant.parse(value);
        } catch (DateTimeParseException ex) {
            return null;
        }
    }

    private String errorJson(String code, String message) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("code", code);
        node.put("message", message);
        return node.toString();
    }

    private static long elapsedMillis(long startedAt) {
        return TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt);
    }
}
