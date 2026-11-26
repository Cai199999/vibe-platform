package com.vibeplatform.bff.admin;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.EnableAspectJAutoProxy;
import org.springframework.context.annotation.Import;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.client.RestClient;

/** 管理中台门禁错误统计：权限与透传契约。 */
@WebMvcTest(controllers = AdminGateErrorController.class)
@Import({AdminAuthorizationAspect.class, AdminGateErrorControllerTest.TestBeans.class})
class AdminGateErrorControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void rejectsMemberRole() throws Exception {
        mockMvc.perform(get("/api/v1/admin/gate-errors/stats")
                        .header("X-User-Role", "member"))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value("ADMIN_ROLE_REQUIRED"));
    }

    @Test
    void adminCanReadGateErrorStats() throws Exception {
        mockMvc.perform(get("/api/v1/admin/gate-errors/stats")
                        .header("X-User-Role", "admin")
                        .header("X-Request-Id", "req-gate-test"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.requestId").value("req-gate-test"))
                .andExpect(jsonPath("$.data.total").value(4))
                .andExpect(jsonPath("$.data.maxAttempts").value(3));
    }

    @TestConfiguration
    @EnableAspectJAutoProxy
    static class TestBeans {

        @Bean
        AdminGateErrorService adminGateErrorService() {
            return new AdminGateErrorService(RestClient.create()) {
                @Override
                public JsonNode stats(int top) {
                    ObjectMapper mapper = new ObjectMapper();
                    ObjectNode node = mapper.createObjectNode();
                    node.put("total", 4);
                    node.put("maxAttempts", 3);
                    node.set("topErrors", mapper.createArrayNode());
                    return node;
                }
            };
        }
    }
}
