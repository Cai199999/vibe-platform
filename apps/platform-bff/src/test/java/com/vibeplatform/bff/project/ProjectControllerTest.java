package com.vibeplatform.bff.project;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.vibeplatform.bff.identity.TenantFilter;
import com.vibeplatform.bff.project.ProjectDtos.ContractView;
import com.vibeplatform.bff.project.ProjectDtos.ProjectView;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(controllers = ProjectController.class)
@Import({
        TenantFilter.class,
        ProjectPersistenceExceptionHandler.class,
        ProjectControllerTest.TestBeans.class
})
class ProjectControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void listsProjectsForCurrentTenant() throws Exception {
        mockMvc.perform(get("/api/v1/projects")
                        .header("X-Tenant-Id", "tenant_projects"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.tenantId").value("tenant_projects"))
                .andExpect(jsonPath("$.projects.length()").value(1))
                .andExpect(jsonPath("$.projects[0].name").value("工单系统"));
    }

    @Test
    void createsProjectWithClientId() throws Exception {
        mockMvc.perform(post("/api/v1/projects")
                        .header("X-Tenant-Id", "tenant_projects")
                        .header("X-User-Id", "user_test")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"id":"proj_test0001","name":"工单系统",
                                 "appType":"FULLSTACK","sourceMode":"visual"}
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.project.id").value("proj_test0001"))
                .andExpect(jsonPath("$.project.appType").value("FULLSTACK"));
    }

    @Test
    void rejectsBlankName() throws Exception {
        mockMvc.perform(post("/api/v1/projects")
                        .header("X-Tenant-Id", "tenant_projects")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\"\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("PROJECT_NAME_INVALID"));
    }

    @TestConfiguration
    static class TestBeans {

        @Bean
        ProjectRepository projectRepository() {
            return new StubRepository();
        }

        @Bean
        ProjectService projectService(ProjectRepository repository) {
            return new ProjectService(repository);
        }
    }

    static class StubRepository extends ProjectRepository {

        private ProjectView stored = new ProjectView(
                "proj_test0001",
                "工单系统",
                "FULLSTACK",
                "visual",
                "ACTIVE",
                "2026-11-26T00:00:00Z",
                "2026-11-26T00:00:00Z"
        );

        StubRepository() {
            super(null);
        }

        @Override
        public List<ProjectView> list(String tenantId, int limit) {
            return List.of(stored);
        }

        @Override
        public Optional<ProjectView> get(String tenantId, String projectId) {
            return stored.id().equals(projectId) ? Optional.of(stored) : Optional.empty();
        }

        @Override
        public ProjectView create(
                String tenantId,
                String userId,
                String id,
                String name,
                String appType,
                String sourceMode
        ) {
            stored = new ProjectView(
                    id,
                    name,
                    appType,
                    sourceMode,
                    "ACTIVE",
                    "2026-11-26T00:00:00Z",
                    "2026-11-26T00:00:00Z"
            );
            return stored;
        }

        @Override
        public ContractView saveContract(
                String tenantId,
                String projectId,
                String contractId,
                String status,
                String payloadJson
        ) {
            return new ContractView(
                    "ctr_test",
                    contractId,
                    projectId,
                    status,
                    new com.fasterxml.jackson.databind.ObjectMapper().createObjectNode(),
                    "2026-11-26T00:00:00Z",
                    "2026-11-26T00:00:00Z"
            );
        }

        @Override
        public List<ContractView> listContracts(
                String tenantId,
                String projectId,
                int limit
        ) {
            return List.of();
        }
    }
}
