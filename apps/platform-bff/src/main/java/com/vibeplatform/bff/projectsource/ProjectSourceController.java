package com.vibeplatform.bff.projectsource;

import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceResponse;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.SaveProjectSourceRequest;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 项目源码持久化入口。
 *
 * <p>导入源码时前端先 PUT 落库，再在本地建立项目；重新打开项目时 GET
 * 回灌文件树，保证 ProjectModeResolver 始终能拿到源码事实。</p>
 */
@RestController
@RequestMapping("/api/v1/project-sources")
public class ProjectSourceController {

    private final ProjectSourceService service;

    public ProjectSourceController(ProjectSourceService service) {
        this.service = service;
    }

    @PutMapping("/{projectId}")
    public ProjectSourceResponse save(
            @PathVariable String projectId,
            @Valid @RequestBody SaveProjectSourceRequest request
    ) {
        return service.save(projectId, request);
    }

    @GetMapping("/{projectId}")
    public ProjectSourceResponse get(
            @PathVariable String projectId,
            @RequestParam(name = "tenantId", required = false) String tenantId
    ) {
        return service.get(projectId, tenantId);
    }
}
