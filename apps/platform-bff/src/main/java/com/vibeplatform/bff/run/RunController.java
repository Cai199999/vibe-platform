package com.vibeplatform.bff.run;

import com.fasterxml.jackson.databind.JsonNode;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 画布 -> BFF -> AI Core 的同步执行入口。
 */
@RestController
@RequestMapping("/api/v1/runs")
public class RunController {

    private final RunService runService;

    public RunController(RunService runService) {
        this.runService = runService;
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
}
