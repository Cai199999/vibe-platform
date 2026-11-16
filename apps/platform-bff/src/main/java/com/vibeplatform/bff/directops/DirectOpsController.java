package com.vibeplatform.bff.directops;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** Direct Ops：跳过 LLM 的确定性画布操作。 */
@RestController
@RequestMapping("/api/v1/direct-ops")
public class DirectOpsController {

    private final DirectOpsService directOpsService;

    public DirectOpsController(DirectOpsService directOpsService) {
        this.directOpsService = directOpsService;
    }

    /** 读取当前主工作树 revision，供前端填写 baseRevision。 */
    @GetMapping("/revision")
    public ResponseEntity<JsonNode> revision(
            @RequestParam String projectId,
            @RequestParam String sandboxId
    ) {
        return directOpsService.revision(projectId, sandboxId);
    }

    /** 执行 / dry-run 一次 Direct Action。 */
    @PostMapping("/actions")
    public ResponseEntity<JsonNode> execute(@RequestBody JsonNode body) {
        return directOpsService.execute(body);
    }

    /** DELETE_NODE 前置静态分析：只返回安全判定。 */
    @PostMapping("/analyze-delete")
    public ResponseEntity<JsonNode> analyzeDelete(@RequestBody JsonNode body) {
        return directOpsService.analyzeDelete(body);
    }
}
