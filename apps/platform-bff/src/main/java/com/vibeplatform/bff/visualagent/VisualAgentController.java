package com.vibeplatform.bff.visualagent;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 离线画布局部 VisualNode 生成入口。 */
@RestController
@RequestMapping("/api/v1/visual-agent")
public class VisualAgentController {

    private final VisualAgentService visualAgentService;

    public VisualAgentController(VisualAgentService visualAgentService) {
        this.visualAgentService = visualAgentService;
    }

    /** 只生成 VisualNode JSON，不写源码、不启动沙盒。 */
    @PostMapping("/nodes")
    public ResponseEntity<JsonNode> generateNodes(@RequestBody JsonNode body) {
        return visualAgentService.generateNodes(body);
    }
}
