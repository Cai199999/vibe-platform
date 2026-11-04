package com.vibeplatform.bff.run;

import com.fasterxml.jackson.databind.JsonNode;
import com.vibeplatform.bff.sandbox.SandboxExportService;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 画布 -> BFF -> AI Core 的同步执行入口。
 */
@RestController
@RequestMapping("/api/v1/runs")
public class RunController {

    private final RunService runService;
    private final SandboxExportService sandboxExportService;

    public RunController(RunService runService, SandboxExportService sandboxExportService) {
        this.runService = runService;
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
