package com.vibeplatform.bff.run;

import com.vibeplatform.bff.common.TraceContext;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.vibeplatform.bff.admin.AdminMetricsService;
import com.vibeplatform.bff.common.SseErrorFrames;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
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
    private static final String FULLSTACK_STREAM_START_PATH =
            "/api/v1/internal/fullstack/runs/stream";
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
        return startStream(
                STREAM_START_PATH,
                request,
                "RUN_REQUEST_SERIALIZATION_FAILED",
                AdminMetricsService.UI_AGENT_ID,
                "run.failed",
                request.runId()
        );
    }

    /**
     * 启动契约确认后的全栈代码生成，并把 DBACoder / BackendCoder 等
     * 节点事件原样透传给浏览器。
     */
    public Flux<ServerSentEvent<String>> startFullstack(JsonNode request) {
        return startStream(
                FULLSTACK_STREAM_START_PATH,
                request,
                "FULLSTACK_REQUEST_SERIALIZATION_FAILED",
                AdminMetricsService.FULLSTACK_AGENT_ID,
                "fullstack.failed",
                request.path("runId").asText("")
        );
    }

    private Flux<ServerSentEvent<String>> startStream(
            String upstreamPath,
            Object request,
            String serializationErrorCode,
            String agentId,
            String terminalEventType,
            String runId
    ) {
        AtomicBoolean streamStarted = new AtomicBoolean(false);
        final String jsonBody;
        try {
            jsonBody = objectMapper.writeValueAsString(request);
        } catch (Exception ex) {
            return Flux.error(new AiCoreStreamException(
                    HttpStatus.BAD_REQUEST,
                    errorJson(
                            serializationErrorCode,
                            "请求序列化失败: " + ex.getMessage()
                    )
            ));
        }

        long startedAt = System.nanoTime();
        return aiCoreStreamWebClient.post()
                .uri(upstreamPath)
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM)
                .headers(headers -> {
                    String traceId = TraceContext.current();
                    if (traceId != null && !traceId.isBlank()) {
                        headers.set(TraceContext.HEADER, traceId);
                    }
                })
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
                .doOnNext(event -> {
                    streamStarted.set(true);
                    observeEvent(event, agentId);
                })
                .doOnComplete(() -> log.info(
                        "{} completed elapsedMs={}",
                        upstreamPath,
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
                )
                .onErrorResume(ex -> {
                    // 建流前错误交给 RestControllerAdvice 设置 HTTP 状态。
                    if (!streamStarted.get() && ex instanceof AiCoreStreamException) {
                        return Flux.error(ex);
                    }
                    log.warn(
                            "bff stream interrupted path={} runId={} type={} message={}",
                            upstreamPath,
                            runId,
                            ex.getClass().getSimpleName(),
                            ex.getMessage()
                    );
                    return Flux.just(SseErrorFrames.terminal(
                            objectMapper,
                            runId,
                            terminalEventType,
                            "STREAM_UPSTREAM_ERROR",
                            "生成服务连接中断，请重试或稍后继续。"
                    ));
                });
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
        AtomicBoolean streamStarted = new AtomicBoolean(false);
        AtomicReference<String> terminalEventType =
                new AtomicReference<>("run.failed");
        return aiCoreStreamWebClient.get()
                .uri(uriBuilder -> uriBuilder
                        .path(STREAM_EVENTS_PATH)
                        .queryParam("afterSeq", resumeFrom)
                        .build(runId))
                .accept(MediaType.TEXT_EVENT_STREAM)
                .headers(headers -> {
                    String traceId = TraceContext.current();
                    if (traceId != null && !traceId.isBlank()) {
                        headers.set(TraceContext.HEADER, traceId);
                    }
                })
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
                .doOnNext(event -> {
                    streamStarted.set(true);
                    // 同一重放接口同时服务 UI Run 与全栈 Run；按事件命名
                    // 空间选择终态类型，避免全栈前端收到 run.failed 后重连。
                    String data = event.data();
                    if (data != null && data.contains("\"type\":\"fullstack.")) {
                        terminalEventType.set("fullstack.failed");
                    }
                    observeEvent(event, AdminMetricsService.UI_AGENT_ID);
                })
                .onErrorMap(
                        WebClientRequestException.class,
                        ex -> new AiCoreStreamException(
                                HttpStatus.BAD_GATEWAY,
                                errorJson(
                                        "STREAM_UPSTREAM_ERROR",
                                        "连接 AI Core 事件流失败: " + ex.getMessage()
                                )
                        )
                )
                .onErrorResume(ex -> {
                    if (!streamStarted.get() && ex instanceof AiCoreStreamException) {
                        return Flux.error(ex);
                    }
                    log.warn(
                            "bff replay interrupted runId={} type={} message={}",
                            runId,
                            ex.getClass().getSimpleName(),
                            ex.getMessage()
                    );
                    return Flux.just(SseErrorFrames.terminal(
                            objectMapper,
                            runId,
                            terminalEventType.get(),
                            "STREAM_UPSTREAM_ERROR",
                            "生成服务连接中断，请重试或稍后继续。"
                    ));
                });
    }

    /**
     * 从事件中提取 Run 生命周期指标与 sandboxId。
     *
     * <p>本服务只处理 UI Graph 的 Run，因此事件流兜底归属为
     * {@code ui_graph}；Supervisor dispatch 会在此之前写入权威 Agent ID。
     * 只在 payload.sandboxId 是文本时覆盖，避免 run.started 的 null 把之前
     * 已记录的 ID 抹掉。</p>
     */
    private void observeEvent(ServerSentEvent<String> event, String agentId) {
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
                    agentId
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
        return SseErrorFrames.httpEnvelope(objectMapper, code, message);
    }

    private static long elapsedMillis(long startedAt) {
        return TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt);
    }
}
