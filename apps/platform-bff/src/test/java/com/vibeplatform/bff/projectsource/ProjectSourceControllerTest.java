package com.vibeplatform.bff.projectsource;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceResponse;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.SaveProjectSourceRequest;
import java.util.LinkedHashMap;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

/**
 * 源码持久化接口契约测试：保存、回读与稳定错误码。
 */
@WebMvcTest(controllers = ProjectSourceController.class)
@Import(ProjectSourceControllerTest.StubBeans.class)
class ProjectSourceControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private CapturingProjectSourceService service;

    @BeforeEach
    void reset() {
        service.lastSave = null;
        service.lastGetTenant = null;
        service.failure = null;
    }

    @Test
    void savesSourceSnapshotAndReturnsMetadata() throws Exception {
        mockMvc.perform(put("/api/v1/project-sources/proj_1")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "tenantId":"tenant_local",
                                  "projectName":"存量项目",
                                  "entryFile":"src/App.jsx",
                                  "sourceName":"App.jsx",
                                  "files":{"src/App.jsx":"export default function App() {}"}
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.projectId").value("proj_1"))
                .andExpect(jsonPath("$.revision").value(1))
                .andExpect(jsonPath("$.fileCount").value(1));

        assertThat(service.lastSave).isNotNull();
        assertThat(service.lastSave.entryFile()).isEqualTo("src/App.jsx");
        assertThat(service.lastSave.files())
                .containsEntry("src/App.jsx", "export default function App() {}");
    }

    @Test
    void readsSourceSnapshotWithTenantFilter() throws Exception {
        service.response = responseWithFiles();

        mockMvc.perform(get("/api/v1/project-sources/proj_1")
                        .param("tenantId", "tenant_local"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.files['src/App.jsx']")
                        .value("export default function App() {}"));

        assertThat(service.lastGetTenant).isEqualTo("tenant_local");
    }

    @Test
    void returnsStableNotFoundCode() throws Exception {
        service.failure = ProjectSourceException.notFound();

        mockMvc.perform(get("/api/v1/project-sources/proj_missing"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("PROJECT_SOURCE_NOT_FOUND"))
                .andExpect(jsonPath("$.retryable").value(false));
    }

    @Test
    void rejectsEmptyFilesAtBoundary() throws Exception {
        mockMvc.perform(put("/api/v1/project-sources/proj_1")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"files\":{}}"))
                .andExpect(status().isUnprocessableEntity())
                .andExpect(jsonPath("$.code").value("REQUEST_VALIDATION_FAILED"));

        assertThat(service.lastSave).isNull();
    }

    private static ProjectSourceResponse responseWithFiles() {
        Map<String, String> files = new LinkedHashMap<>();
        files.put("src/App.jsx", "export default function App() {}");
        return new ProjectSourceResponse(
                "proj_1",
                "tenant_local",
                "存量项目",
                "src/App.jsx",
                "App.jsx",
                1,
                1,
                30,
                "a".repeat(64),
                "2026-11-18T00:00:00Z",
                files
        );
    }

    static class CapturingProjectSourceService extends ProjectSourceService {

        SaveProjectSourceRequest lastSave;
        String lastGetTenant;
        ProjectSourceResponse response;
        RuntimeException failure;

        CapturingProjectSourceService() {
            super(null, null);
            this.response = responseWithFiles();
        }

        @Override
        public synchronized ProjectSourceResponse save(
                String projectId,
                SaveProjectSourceRequest request
        ) {
            if (failure != null) {
                throw failure;
            }
            lastSave = request;
            return new ProjectSourceResponse(
                    projectId,
                    request.tenantId(),
                    request.projectName(),
                    request.entryFile(),
                    request.sourceName(),
                    1,
                    request.files().size(),
                    30,
                    "a".repeat(64),
                    "2026-11-18T00:00:00Z",
                    null
            );
        }

        @Override
        public ProjectSourceResponse get(String projectId, String tenantId) {
            if (failure != null) {
                throw failure;
            }
            lastGetTenant = tenantId;
            return response;
        }
    }

    @TestConfiguration
    static class StubBeans {

        @Bean
        CapturingProjectSourceService projectSourceService() {
            return new CapturingProjectSourceService();
        }
    }
}
