package com.vibeplatform.bff.architect;

import com.fasterxml.jackson.databind.JsonNode;
import jakarta.validation.Valid;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Flux;

/**
 * 全局架构澄清（M2.5）公开 API。
 *
 * <p>与 {@code /api/v1/runs} 完全分离：画布修改与全栈 Path 生成拥有独立
 * 生命周期、事件命名空间和 HITL 暂停语义。</p>
 */
@RestController
@RequestMapping("/api/v1/architect")
public class ArchitectController {

    private final ArchitectService architectService;
    private final ArchitectStreamService architectStreamService;

    public ArchitectController(
            ArchitectService architectService,
            ArchitectStreamService architectStreamService
    ) {
        this.architectService = architectService;
        this.architectStreamService = architectStreamService;
    }

    /** 启动架构澄清并返回 SSE。 */
    @PostMapping(
            value = "/runs/stream",
            produces = MediaType.TEXT_EVENT_STREAM_VALUE
    )
    public Flux<ServerSentEvent<String>> start(
            @Valid @RequestBody ArchitectStartRequest request
    ) {
        return architectStreamService.start(request);
    }

    /** 断线重放：从 afterSeq 之后继续订阅。 */
    @GetMapping(
            value = "/runs/{runId}/events/stream",
            produces = MediaType.TEXT_EVENT_STREAM_VALUE
    )
    public Flux<ServerSentEvent<String>> replay(
            @PathVariable String runId,
            @RequestParam(name = "afterSeq", required = false) Integer afterSeq,
            @RequestHeader(name = "Last-Event-ID", required = false) String lastEventId
    ) {
        return architectStreamService.replay(runId, afterSeq, lastEventId);
    }

    /** 回答全局澄清；AI Core 后台恢复状态机并返回 202。 */
    @PostMapping(
            "/runs/{runId}/clarifications/{clarificationId}/answers"
    )
    public ResponseEntity<JsonNode> answerClarification(
            @PathVariable String runId,
            @PathVariable String clarificationId,
            @RequestBody(required = false) JsonNode body
    ) {
        return architectService.answerClarification(runId, clarificationId, body);
    }

    /** CONFIRM / REVISE / CANCEL 实施契约。 */
    @PostMapping("/runs/{runId}/contracts/{contractId}/decision")
    public ResponseEntity<JsonNode> decideContract(
            @PathVariable String runId,
            @PathVariable String contractId,
            @RequestBody(required = false) JsonNode body
    ) {
        return architectService.decideContract(runId, contractId, body);
    }

    /** 影子 LLM：只读解释架构决策，不推进主状态机。 */
    @PostMapping("/runs/{runId}/helper")
    public ResponseEntity<JsonNode> askHelper(
            @PathVariable String runId,
            @RequestBody(required = false) JsonNode body
    ) {
        return architectService.askHelper(runId, body);
    }

    /** 取消架构澄清。 */
    @PostMapping("/runs/{runId}/cancel")
    public ResponseEntity<JsonNode> cancel(
            @PathVariable String runId,
            @RequestBody(required = false) JsonNode body
    ) {
        return architectService.cancel(runId, body);
    }

    /** 刷新 / 重连后恢复快照。 */
    @GetMapping("/runs/{runId}")
    public ResponseEntity<JsonNode> snapshot(@PathVariable String runId) {
        return architectService.snapshot(runId);
    }
}
