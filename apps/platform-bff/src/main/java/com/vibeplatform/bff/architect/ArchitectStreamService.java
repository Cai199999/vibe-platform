package com.vibeplatform.bff.architect;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
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

    public ArchitectStreamService(
            @Qualifier("aiCoreStreamWebClient") WebClient aiCoreStreamWebClient,
            ObjectMapper objectMapper
    ) {
        this.aiCoreStreamWebClient = aiCoreStreamWebClient;
        this.objectMapper = objectMapper;
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
                );
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
                );
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
