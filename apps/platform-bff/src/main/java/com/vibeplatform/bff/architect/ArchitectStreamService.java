package com.vibeplatform.bff.architect;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.vibeplatform.bff.admin.AdminMetricsService;
import com.vibeplatform.bff.common.SseErrorFrames;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
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
 * Architect SSE 透传：BFF 不解析事件，也不重新分配 seq。
 *
 * <p>与局部 UI Run 使用同一套 {@code ServerSentEvent} 透传模型，但路径
 * 与上游服务完全独立，避免两类任务在状态、超时和事件语义上耦合。</p>
 */
@Service
public class ArchitectStreamService {

    private static final Logger log =
            LoggerFactory.getLogger(ArchitectStreamService.class);
    private static final String STREAM_START_PATH =
            "/api/v1/internal/architect/runs/stream";
    private static final String STREAM_EVENTS_PATH =
            "/api/v1/internal/architect/runs/{runId}/events/stream";
    private static final ParameterizedTypeReference<ServerSentEvent<String>> SSE_TYPE =
            new ParameterizedTypeReference<>() {
            };

    private final WebClient aiCoreStreamWebClient;
    private final ObjectMapper objectMapper;
    private final AdminMetricsService adminMetricsService;

    public ArchitectStreamService(
            @Qualifier("aiCoreStreamWebClient") WebClient aiCoreStreamWebClient,
            ObjectMapper objectMapper,
            AdminMetricsService adminMetricsService
    ) {
        this.aiCoreStreamWebClient = aiCoreStreamWebClient;
        this.objectMapper = objectMapper;
        this.adminMetricsService = adminMetricsService;
    }

    public Flux<ServerSentEvent<String>> start(ArchitectStartRequest request) {
        final String jsonBody;
        try {
            jsonBody = objectMapper.writeValueAsString(request);
        } catch (Exception ex) {
            return Flux.error(new ArchitectStreamException(
                    HttpStatus.BAD_REQUEST,
                    errorJson(
                            "ARCHITECT_REQUEST_SERIALIZATION_FAILED",
                            "Architect 请求序列化失败: " + ex.getMessage()
                    )
            ));
        }

        long startedAt = System.nanoTime();
        AtomicBoolean streamStarted = new AtomicBoolean(false);
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
                                        new ArchitectStreamException(
                                                response.statusCode(), body
                                        )
                                ));
                    }
                    return response.bodyToFlux(SSE_TYPE);
                })
                .doOnNext(event -> {
                    streamStarted.set(true);
                    observeEvent(event);
                })
                .doOnComplete(() -> log.info(
                        "architect stream completed elapsedMs={}",
                        elapsedMillis(startedAt)
                ))
                .onErrorMap(
                        WebClientRequestException.class,
                        ex -> new ArchitectStreamException(
                                HttpStatus.BAD_GATEWAY,
                                errorJson(
                                        "ARCHITECT_STREAM_UPSTREAM_ERROR",
                                        "连接 AI Core Architect 事件流失败: "
                                                + ex.getMessage()
                                )
                        )
                )
                .onErrorResume(ex -> {
                    // 建流前错误仍交给全局/局部 RestControllerAdvice。
                    if (!streamStarted.get() && ex instanceof ArchitectStreamException) {
                        return Flux.error(ex);
                    }
                    log.warn(
                            "bff architect stream interrupted runId={} type={} message={}",
                            request.runId(),
                            ex.getClass().getSimpleName(),
                            ex.getMessage()
                    );
                    return Flux.just(SseErrorFrames.terminal(
                            objectMapper,
                            request.runId(),
                            "architect.failed",
                            "ARCHITECT_STREAM_UPSTREAM_ERROR",
                            "架构服务连接中断，请重试或稍后继续。"
                    ));
                });
    }

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
        AtomicBoolean streamStarted = new AtomicBoolean(false);
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
                                        new ArchitectStreamException(
                                                response.statusCode(), body
                                        )
                                ));
                    }
                    return response.bodyToFlux(SSE_TYPE);
                })
                .doOnNext(event -> {
                    streamStarted.set(true);
                    observeEvent(event);
                })
                .onErrorMap(
                        WebClientRequestException.class,
                        ex -> new ArchitectStreamException(
                                HttpStatus.BAD_GATEWAY,
                                errorJson(
                                        "ARCHITECT_STREAM_UPSTREAM_ERROR",
                                        "连接 AI Core Architect 事件流失败: "
                                                + ex.getMessage()
                                )
                        )
                )
                .onErrorResume(ex -> {
                    if (!streamStarted.get() && ex instanceof ArchitectStreamException) {
                        return Flux.error(ex);
                    }
                    log.warn(
                            "bff architect replay interrupted runId={} type={} message={}",
                            runId,
                            ex.getClass().getSimpleName(),
                            ex.getMessage()
                    );
                    return Flux.just(SseErrorFrames.terminal(
                            objectMapper,
                            runId,
                            "architect.failed",
                            "ARCHITECT_STREAM_UPSTREAM_ERROR",
                            "架构服务连接中断，请重试或稍后继续。"
                    ));
                });
    }

    /**
     * 把 Architect 事件写入管理中台指标。
     *
     * <p>若同一 Run 已由 Supervisor dispatch 登记过，指标服务会保留
     * dispatch 写入的 Agent 归属；这里只做兜底。</p>
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
            adminMetricsService.observeEvent(
                    runIdNode.asText(),
                    text(node, "type"),
                    parseTimestamp(text(node, "ts")),
                    node.path("payload"),
                    AdminMetricsService.ARCHITECT_AGENT_ID
            );
        } catch (Exception ex) {
            // 指标是旁路能力，解析失败绝不能中断事件转发。
            log.warn("failed to parse architect event for metrics: {}",
                    ex.getMessage());
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
        return SseErrorFrames.httpEnvelope(objectMapper, code, message);
    }

    private static long elapsedMillis(long startedAt) {
        return TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt);
    }
}
