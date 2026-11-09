package com.vibeplatform.bff.sandbox;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.test.web.servlet.MockMvc;

/**
 * 快照接口契约测试：上下文记忆作用域透传与非法值拦截。
 *
 * <p>Java 25 下 Mockito 的 Byte Buddy 版本不兼容，因此用轻量桩 Bean
 * 替代 {@code @MockBean}，保持测试可执行且无外部依赖。</p>
 */
@WebMvcTest(controllers = SandboxController.class)
@Import(SandboxControllerSnapshotTest.StubBeans.class)
class SandboxControllerSnapshotTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private CapturingSnapshotService snapshotService;

    @BeforeEach
    void resetCalls() {
        // WebMvcTest 会复用同一个 Spring 上下文，桩 Bean 需要按用例清理。
        snapshotService.calls.clear();
    }

    @Test
    void passesTrimmedMemoryScopeToSnapshotService() throws Exception {
        mockMvc.perform(post("/api/v1/sandboxes/sbx_1/snapshots")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"message":" 完成登录 ","projectId":" proj_1 ","tenantId":"tenant_local","latestChanges":" 新增登录按钮 "}
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.commitHash").value("a".repeat(40)));

        assertThat(snapshotService.calls)
                .containsExactly(new Call(
                        "sbx_1",
                        "完成登录",
                        "proj_1",
                        "tenant_local",
                        "新增登录按钮"
                ));
    }

    @Test
    void rejectsScopeWithPathTraversal() throws Exception {
        mockMvc.perform(post("/api/v1/sandboxes/sbx_1/snapshots")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"message":"快照","projectId":"../../etc","tenantId":"tenant_local"}
                                """))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_MEMORY_SCOPE"));

        assertThat(snapshotService.calls).isEmpty();
    }

    record Call(
            String sandboxId,
            String message,
            String projectId,
            String tenantId,
            String latestChanges
    ) {
    }

    static class CapturingSnapshotService extends SandboxSnapshotService {

        private final ObjectMapper mapper;
        private final List<Call> calls = new ArrayList<>();

        CapturingSnapshotService(ObjectMapper mapper) {
            super(null, mapper);
            this.mapper = mapper;
        }

        @Override
        public ResponseEntity<JsonNode> create(
                String sandboxId,
                String message,
                String projectId,
                String tenantId,
                String latestChanges
        ) {
            calls.add(new Call(sandboxId, message, projectId, tenantId, latestChanges));
            ObjectNode body = mapper.createObjectNode();
            body.put("commitHash", "a".repeat(40));
            return ResponseEntity.ok(body);
        }
    }

    @TestConfiguration
    static class StubBeans {

        @Bean
        SandboxService sandboxService(ObjectMapper mapper) {
            return new SandboxService(null, mapper, null);
        }

        @Bean
        SandboxExportService sandboxExportService(ObjectMapper mapper) {
            return new SandboxExportService(null, mapper, null);
        }

        @Bean
        CapturingSnapshotService snapshotService(ObjectMapper mapper) {
            return new CapturingSnapshotService(mapper);
        }
    }
}
