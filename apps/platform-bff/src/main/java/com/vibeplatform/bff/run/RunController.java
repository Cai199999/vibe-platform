package com.vibeplatform.bff.run;

import com.fasterxml.jackson.databind.JsonNode;
import com.vibeplatform.bff.sandbox.SandboxExportService;
import jakarta.servlet.http.HttpServletResponse;
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
 * 画布 -> BFF -> AI Core 的同步 / 流式执行入口。
 */
@RestController
@RequestMapping("/api/v1/runs")
public class RunController {

    private final RunService runService;
    private final RunStreamService runStreamService;
    private final SandboxExportService sandboxExportService;

    public RunController(
            RunService runService,
            RunStreamService runStreamService,
            SandboxExportService sandboxExportService
    ) {
        this.runService = runService;
        this.runStreamService = runStreamService;
        this.sandboxExportService = sandboxExportService;
    }

    /**
     * 接收 AnnotationEnvelope，代理执行单 Agent 闭环。
     *
     * <p>成功与业务失败均可能返回 200：前端必须读取响应体中的
     * {@code status}（AWAITING_APPROVAL / FAILED）。</p>
     */
    @PostMapping
    public ResponseEntity<JsonNode> createRun(@Valid @RequestBody CreateRunRequest request) {
        return runService.execute(request);
    }

    /**
     * 启动 Run 并返回 SSE。
     *
     * <p>返回 Flux 由 Spring MVC 的 ReactiveTypeHandler 异步写回，
     * 不占用 Servlet 线程等待整个 Run。</p>
     */
    @PostMapping(
            value = "/stream",
            produces = MediaType.TEXT_EVENT_STREAM_VALUE
    )
    public Flux<ServerSentEvent<String>> streamRun(
            @Valid @RequestBody CreateRunRequest request
    ) {
        return runStreamService.start(request);
    }

    /**
     * 断线重放：从 afterSeq 之后继续推送同一 Run 的事件。
     */
    @GetMapping(
            value = "/{runId}/events/stream",
            produces = MediaType.TEXT_EVENT_STREAM_VALUE
    )
    public Flux<ServerSentEvent<String>> streamRunEvents(
            @PathVariable String runId,
            @RequestParam(name = "afterSeq", required = false) Integer afterSeq,
            @RequestHeader(name = "Last-Event-ID", required = false) String lastEventId
    ) {
        return runStreamService.replay(runId, afterSeq, lastEventId);
    }

    /**
     * 提交澄清答案；AI Core 后台恢复状态机并返回 202。
     */
    @PostMapping("/{runId}/clarifications/{clarificationId}/answers")
    public ResponseEntity<JsonNode> answerClarification(
            @PathVariable String runId,
            @PathVariable String clarificationId,
            @RequestBody(required = false) JsonNode body
    ) {
        return runService.answerClarification(runId, clarificationId, body);
    }

    /**
     * Run 快照：刷新页面 / 重连后恢复当前状态与待回答澄清。
     */
    @GetMapping("/{runId}")
    public ResponseEntity<JsonNode> getRun(@PathVariable String runId) {
        return runService.getRun(runId);
    }

    @GetMapping("/{runId}/clarifications")
    public ResponseEntity<JsonNode> getClarifications(@PathVariable String runId) {
        return runService.getClarifications(runId);
    }

    /**
     * 把某个标注 thread 回滚到指定成功轮次。
     *
     * <p>AI Core 会执行 {@code git reset --hard} 并恢复该 thread 的
     * checkpoint；返回的 previewUrl 带 cache-busting 参数，前端替换后
     * iframe 强制刷新。</p>
     */
    @PostMapping("/rollback")
    public ResponseEntity<JsonNode> rollback(@Valid @RequestBody RollbackRunRequest request) {
        return runService.rollback(request);
    }

    /**
     * 下载该 Run 生成的完整项目（tar.gz）。
     *
     * <p>sandboxId 可选：BFF 会记录最近执行的 runId -> sandboxId；前端同时
     * 传 sandboxId 可以覆盖 BFF 重启后映射丢失的情况。</p>
     */
    @GetMapping("/{runId}/export")
    public void exportRun(
            @PathVariable String runId,
            @RequestParam(name = "sandboxId", required = false) String sandboxId,
            HttpServletResponse response
    ) {
        sandboxExportService.exportByRun(runId, sandboxId, response);
    }
}
