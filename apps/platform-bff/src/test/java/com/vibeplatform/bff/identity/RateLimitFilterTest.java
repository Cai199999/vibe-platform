package com.vibeplatform.bff.identity;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;

import jakarta.servlet.FilterChain;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

/** 租户限流：超限 429、健康检查放行、租户互相隔离。 */
class RateLimitFilterTest {

    @AfterEach
    void clearTenant() {
        TenantContext.clear();
    }

    @Test
    void disabledFilterPassesThrough() throws Exception {
        RateLimitFilter filter = new RateLimitFilter(false, 1);
        FilterChain chain = mock(FilterChain.class);
        MockHttpServletRequest request = request("/api/v1/architect/runs");
        MockHttpServletResponse response = new MockHttpServletResponse();

        for (int index = 0; index < 5; index++) {
            filter.doFilter(request, response, chain);
        }
        verify(chain, org.mockito.Mockito.times(5)).doFilter(request, response);
    }

    @Test
    void rejectsAfterBucketExhaustedWith429() throws Exception {
        RateLimitFilter filter = new RateLimitFilter(true, 2);
        FilterChain chain = mock(FilterChain.class);
        TenantContext.set("tenant_rate");

        MockHttpServletRequest request = request("/api/v1/architect/runs");
        MockHttpServletResponse first = new MockHttpServletResponse();
        MockHttpServletResponse second = new MockHttpServletResponse();
        MockHttpServletResponse third = new MockHttpServletResponse();

        filter.doFilter(request, first, chain);
        filter.doFilter(request, second, chain);
        filter.doFilter(request, third, chain);

        assertThat(first.getStatus()).isEqualTo(200);
        assertThat(second.getStatus()).isEqualTo(200);
        assertThat(third.getStatus()).isEqualTo(429);
        assertThat(third.getHeader("Retry-After")).isNotBlank();
        assertThat(third.getContentAsString()).contains("RATE_LIMITED");
    }

    @Test
    void tenantsDoNotShareBuckets() throws Exception {
        RateLimitFilter filter = new RateLimitFilter(true, 1);
        FilterChain chain = mock(FilterChain.class);
        MockHttpServletRequest request = request("/api/v1/architect/runs");

        TenantContext.set("tenant_a");
        MockHttpServletResponse first = new MockHttpServletResponse();
        filter.doFilter(request, first, chain);
        MockHttpServletResponse second = new MockHttpServletResponse();
        filter.doFilter(request, second, chain);

        TenantContext.set("tenant_b");
        MockHttpServletResponse otherTenant = new MockHttpServletResponse();
        filter.doFilter(request, otherTenant, chain);

        assertThat(first.getStatus()).isEqualTo(200);
        assertThat(second.getStatus()).isEqualTo(429);
        assertThat(otherTenant.getStatus()).isEqualTo(200);
    }

    @Test
    void healthCheckIsNeverLimited() throws Exception {
        RateLimitFilter filter = new RateLimitFilter(true, 1);
        FilterChain chain = mock(FilterChain.class);
        MockHttpServletRequest request = request("/api/v1/health");

        for (int index = 0; index < 3; index++) {
            MockHttpServletResponse response = new MockHttpServletResponse();
            filter.doFilter(request, response, chain);
            assertThat(response.getStatus()).isEqualTo(200);
        }
    }

    private static MockHttpServletRequest request(String path) {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", path);
        request.setRequestURI(path);
        return request;
    }
}
