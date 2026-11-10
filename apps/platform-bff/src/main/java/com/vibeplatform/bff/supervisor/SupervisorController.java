package com.vibeplatform.bff.supervisor;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Supervisor 多智能体总调度公开 API。
 *
 * <p>BFF 只做透传：意图分类、AgentRegistry 与子图派发全部由 Python AI Core
 * 负责；UI Graph / Architect Graph 的既有公开 API 保持独立。</p>
 */
@RestController
@RequestMapping("/api/v1/supervisor")
public class SupervisorController {

    private final SupervisorProxyService supervisorService;

    public SupervisorController(SupervisorProxyService supervisorService) {
        this.supervisorService = supervisorService;
    }

    /** 只返回路由决策与 Handoff 计划，不启动子图。 */
    @PostMapping("/route")
    public ResponseEntity<JsonNode> route(
            @RequestBody(required = false) JsonNode body
    ) {
        return supervisorService.route(body);
    }

    /** 按路由计划启动子图，返回子 Run ID 与 SSE 路径。 */
    @PostMapping("/dispatch")
    public ResponseEntity<JsonNode> dispatch(
            @RequestBody(required = false) JsonNode body
    ) {
        return supervisorService.dispatch(body);
    }

    /** 查询 both 门控状态。 */
    @GetMapping("/gates/{gateId}")
    public ResponseEntity<JsonNode> gate(@PathVariable String gateId) {
        return supervisorService.gate(gateId);
    }

    /** 契约确认后放行 UI Graph；重复调用返回同一个 UI Run。 */
    @PostMapping("/gates/{gateId}/release")
    public ResponseEntity<JsonNode> releaseGate(
            @PathVariable String gateId,
            @RequestBody(required = false) JsonNode body
    ) {
        return supervisorService.releaseGate(gateId);
    }

    /** 列出内置、预留与企业 Manifest Agent。 */
    @GetMapping("/agents")
    public ResponseEntity<JsonNode> agents() {
        return supervisorService.agents();
    }

    /** 重新扫描 CustomAgent Manifest。 */
    @PostMapping("/agents/reload")
    public ResponseEntity<JsonNode> reloadAgents() {
        return supervisorService.reloadAgents();
    }

    /** 根据自然语言需求创建企业自定义 Agent。 */
    @PostMapping("/agents")
    public ResponseEntity<JsonNode> createAgent(
            @RequestBody JsonNode body
    ) {
        return supervisorService.createAgent(body);
    }

    /** 删除自定义 Agent；内置 Agent 由 AI Core 侧拒绝。 */
    @DeleteMapping("/agents/{agentId}")
    public ResponseEntity<JsonNode> deleteAgent(@PathVariable String agentId) {
        return supervisorService.deleteAgent(agentId);
    }

    /** 列出某个自定义 Agent 的知识文件。 */
    @GetMapping("/agents/{agentId}/knowledge")
    public ResponseEntity<JsonNode> agentKnowledge(
            @PathVariable String agentId
    ) {
        return supervisorService.agentKnowledge(agentId);
    }

    /** 上传 .md/.txt 知识文件（JSON 文本，避免 BFF 维护 multipart 链路）。 */
    @PostMapping("/agents/{agentId}/knowledge")
    public ResponseEntity<JsonNode> uploadAgentKnowledge(
            @PathVariable String agentId,
            @RequestBody JsonNode body
    ) {
        return supervisorService.uploadAgentKnowledge(agentId, body);
    }

    /** 删除单个知识文件。 */
    @DeleteMapping("/agents/{agentId}/knowledge/{fileName}")
    public ResponseEntity<JsonNode> deleteAgentKnowledge(
            @PathVariable String agentId,
            @PathVariable String fileName
    ) {
        return supervisorService.deleteAgentKnowledge(agentId, fileName);
    }
}
