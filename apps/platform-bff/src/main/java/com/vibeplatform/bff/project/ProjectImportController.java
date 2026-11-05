package com.vibeplatform.bff.project;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

/**
 * 项目导入入口。
 *
 * <p>对应前端左上角「导入项目」：上传 zip（React / HTML 项目包）或
 * png / jpg 截图，Python AI Core 解包并启动沙盒后返回预览地址。</p>
 */
@RestController
@RequestMapping("/api/v1/projects")
public class ProjectImportController {

    private final ProjectImportService projectImportService;

    public ProjectImportController(ProjectImportService projectImportService) {
        this.projectImportService = projectImportService;
    }

    /**
     * 上传并导入项目。
     *
     * @param file      项目压缩包或界面截图
     * @param projectId 前端预生成的项目 ID，用于沙盒标签与后续 Run 复用
     * @return 沙盒信息（sandboxId / previewUrl / entryFile / files 等）
     */
    @PostMapping(value = "/import", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<JsonNode> importProject(
            @RequestParam("file") MultipartFile file,
            @RequestParam(value = "projectId", required = false) String projectId
    ) {
        return projectImportService.importProject(projectId, file);
    }
}
