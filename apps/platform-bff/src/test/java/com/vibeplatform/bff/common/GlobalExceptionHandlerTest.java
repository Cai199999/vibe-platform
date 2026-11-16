package com.vibeplatform.bff.common;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** BFF 全局异常边界：只返回统一信封，不泄漏技术细节。 */
class GlobalExceptionHandlerTest {

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        ObjectMapper objectMapper = new ObjectMapper();
        mockMvc = MockMvcBuilders.standaloneSetup(new BoomController())
                .setControllerAdvice(new GlobalExceptionHandler(objectMapper))
                .build();
    }

    @Test
    void unhandledExceptionReturnsEnvelope() throws Exception {
        mockMvc.perform(get("/test/boom")
                        .header("X-Trace-Id", "trc_bff_test"))
                .andExpect(status().isInternalServerError())
                .andExpect(header().string("X-Trace-Id", "trc_bff_test"))
                .andExpect(jsonPath("$.code").value("BFF_INTERNAL_ERROR"))
                .andExpect(jsonPath("$.userMessage").value(
                        "服务暂时不可用，已记录诊断编号，请稍后重试。"))
                .andExpect(jsonPath("$.traceId").value("trc_bff_test"))
                .andExpect(jsonPath("$.errorOptions").isArray())
                .andExpect(jsonPath("$.errorOptions").isEmpty())
                .andExpect(jsonPath("$.diagnostic").doesNotExist());
    }

    @RestController
    public static class BoomController {
        @GetMapping("/test/boom")
        String boom() {
            throw new IllegalStateException("jdbc:mysql://root:root@127.0.0.1");
        }
    }
}
