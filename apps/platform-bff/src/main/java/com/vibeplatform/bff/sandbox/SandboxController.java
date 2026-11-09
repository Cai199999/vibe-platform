package com.vibeplatform.bff.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import java.util.Locale;
import java.util.regex.Pattern;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 前端可调用的沙盒管理入口。
 */
@RestController
@RequestMapping("/api/v1/sandboxes")
public class SandboxController {

    // 沙盒 ID 由 Python 生成（sbx_xxx），只允许安全字符，避免路径注入。
    private static final Pattern SANDBOX_ID_PATTERN = Pattern.compile("[A-Za-z0-9_-]{1,80}");
    // commit hash 只接受完整 40 位十六进制；Python 侧还会再做一次白名单校验。
    private static final Pattern COMMIT_HASH_PATTERN = Pattern.compile("[0-9a-f]{40}");

    private final SandboxService sandboxService;
    private final SandboxExportService sandboxExportService;
    private final SandboxSnapshotService snapshotService;
    private final ObjectMapper objectMapper;

    public SandboxController(
            SandboxService sandboxService,
            SandboxExportService sandboxExportService,
            SandboxSnapshotService snapshotService,
            ObjectMapper objectMapper
    ) {
        this.sandboxService = sandboxService;
        this.sandboxExportService = sandboxExportService;
        this.snapshotService = snapshotService;
        this.objectMapper = objectMapper;
    }

    /** 保存快照：把沙盒当前工作区提交为 Git checkpoint。 */
    @PostMapping("/{sandboxId}/snapshots")
    public ResponseEntity<JsonNode> createSnapshot(
            @PathVariable String sandboxId,
            @Valid @RequestBody SnapshotCreateRequest request
    ) {
        ResponseEntity<JsonNode> invalid = validateSandboxId(sandboxId);
        if (invalid != null) return invalid;
        return snapshotService.create(sandboxId, request.message().trim());
    }

    /** 版本历史：按时间倒序返回快照列表。 */
    @GetMapping("/{sandboxId}/snapshots")
    public ResponseEntity<JsonNode> listSnapshots(
            @PathVariable String sandboxId,
            @RequestParam(name = "limit", defaultValue = "50")
            @Min(1) @Max(200) int limit
    ) {
        ResponseEntity<JsonNode> invalid = validateSandboxId(sandboxId);
        if (invalid != null) return invalid;
        return snapshotService.list(sandboxId, limit);
    }

    /** 恢复快照：硬回滚到指定 commit。 */
    @PostMapping("/{sandboxId}/snapshots/{commitHash}/restore")
    public ResponseEntity<JsonNode> restoreSnapshot(
            @PathVariable String sandboxId,
            @PathVariable String commitHash
    ) {
        ResponseEntity<JsonNode> invalid = validateSandboxId(sandboxId);
        if (invalid != null) return invalid;
        ResponseEntity<JsonNode> invalidHash = validateCommitHash(commitHash);
        if (invalidHash != null) return invalidHash;
        return snapshotService.restore(sandboxId, normalizeCommitHash(commitHash));
    }

    /** 快照只读视图的代码 Diff。 */
    @GetMapping("/{sandboxId}/snapshots/{commitHash}/diff")
    public ResponseEntity<JsonNode> snapshotDiff(
            @PathVariable String sandboxId,
            @PathVariable String commitHash,
            @RequestParam(name = "mode", defaultValue = "baseline") String mode
    ) {
        ResponseEntity<JsonNode> invalid = validateSandboxId(sandboxId);
        if (invalid != null) return invalid;
        ResponseEntity<JsonNode> invalidHash = validateCommitHash(commitHash);
        if (invalidHash != null) return invalidHash;
        if (!"commit".equals(mode)
                && !"current".equals(mode)
                && !"baseline".equals(mode)) {
            return badRequest(
                    "INVALID_SNAPSHOT_MODE",
                    "mode 只能是 commit / current / baseline"
            );
        }
        return snapshotService.diff(sandboxId, normalizeCommitHash(commitHash), mode);
    }

    private ResponseEntity<JsonNode> validateSandboxId(String sandboxId) {
        if (sandboxId != null && SANDBOX_ID_PATTERN.matcher(sandboxId).matches()) {
            return null;
        }
        return badRequest("INVALID_SANDBOX_ID", "非法的 sandboxId");
    }

    private ResponseEntity<JsonNode> validateCommitHash(String commitHash) {
        if (commitHash != null
                && COMMIT_HASH_PATTERN.matcher(normalizeCommitHash(commitHash)).matches()) {
            return null;
        }
        return badRequest("INVALID_COMMIT_HASH", "commitHash 必须是 40 位十六进制");
    }

    private String normalizeCommitHash(String commitHash) {
        return commitHash == null ? "" : commitHash.trim().toLowerCase(Locale.ROOT);
    }

    private ResponseEntity<JsonNode> badRequest(String code, String message) {
        ObjectNode body = objectMapper.createObjectNode();
        body.put("code", code);
        body.put("message", message);
        return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(body);
    }

    /**
     * 下载沙盒当前项目的 tar.gz 快照。
     * runId 不可知或 BFF 重启后，前端可直接用 sandboxId 调用该入口。
     */
    @GetMapping("/{sandboxId}/export")
    public void export(@PathVariable String sandboxId, HttpServletResponse response) {
        sandboxExportService.exportSandbox(sandboxId, response);
    }

    /**
     * 关闭项目时销毁对应沙盒容器，释放内存、CPU 与随机端口。
     * 由 BFF 调用 Python 的 DELETE /api/v1/internal/sandbox/{sandboxId}。
     */
    @DeleteMapping("/{sandboxId}")
    public ResponseEntity<JsonNode> destroy(@PathVariable String sandboxId) {
        if (!SANDBOX_ID_PATTERN.matcher(sandboxId).matches()) {
            ObjectNode body = objectMapper.createObjectNode();
            body.put("code", "INVALID_SANDBOX_ID");
            body.put("message", "非法的 sandboxId");
            return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(body);
        }
        return sandboxService.destroy(sandboxId);
    }
}
