package com.vibeplatform.bff.identity;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;

class TenantFilterTest {

    private final TenantFilter filter = new TenantFilter();

    @AfterEach
    void clear() {
        SecurityContextHolder.clearContext();
        TenantContext.clear();
    }

    @Test
    void fallsBackToHeaderWhenNoJwt() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader(TenantContext.HEADER, "tenant_header");
        MockHttpServletResponse response = new MockHttpServletResponse();
        AtomicReference<String> seen = new AtomicReference<>();

        filter.doFilter(request, response, (req, res) -> seen.set(TenantContext.current()));

        assertEquals("tenant_header", seen.get());
        assertEquals("tenant_header", response.getHeader(TenantContext.HEADER));
    }

    @Test
    void jwtClaimWinsAndMismatchIsRejected() throws Exception {
        Jwt jwt = Jwt.withTokenValue("token")
                .header("alg", "none")
                .claim(TenantFilter.TENANT_CLAIM, "tenant_jwt")
                .build();
        SecurityContextHolder.getContext().setAuthentication(
                new JwtAuthenticationToken(jwt)
        );

        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader(TenantContext.HEADER, "tenant_jwt");
        MockHttpServletResponse response = new MockHttpServletResponse();
        AtomicReference<String> seen = new AtomicReference<>();
        filter.doFilter(request, response, (req, res) -> seen.set(TenantContext.current()));
        assertEquals("tenant_jwt", seen.get());

        MockHttpServletRequest mismatch = new MockHttpServletRequest();
        mismatch.addHeader(TenantContext.HEADER, "tenant_other");
        MockHttpServletResponse rejected = new MockHttpServletResponse();
        filter.doFilter(mismatch, rejected, (req, res) -> {
            throw new AssertionError("不应进入业务链");
        });
        assertEquals(403, rejected.getStatus());
    }

    @Test
    void invalidHeaderIsRejected() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest();
        request.addHeader(TenantContext.HEADER, "bad tenant!");
        MockHttpServletResponse response = new MockHttpServletResponse();
        filter.doFilter(request, response, (req, res) -> {
            throw new AssertionError("不应进入业务链");
        });
        assertEquals(400, response.getStatus());
    }
}
