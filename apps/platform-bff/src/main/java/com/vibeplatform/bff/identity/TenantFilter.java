package com.vibeplatform.bff.identity;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.boot.autoconfigure.security.SecurityProperties;
import org.springframework.core.annotation.Order;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 解析租户并写入请求级上下文。
 *
 * <p>启用 OIDC 时优先读取 JWT claim {@code tenant_id}；未启用时回退
 * {@code X-Tenant-Id}。两者同时存在且不一致时拒绝请求，防止靠头部
 * 覆盖 token 内租户。</p>
 */
@Component
@Order(SecurityProperties.DEFAULT_FILTER_ORDER + 10)
public class TenantFilter extends OncePerRequestFilter {

    static final String TENANT_CLAIM = "tenant_id";

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain
    ) throws ServletException, IOException {
        String claimedTenant = tenantFromJwt();
        String headerTenant;
        try {
            headerTenant = TenantContext.normalize(
                    request.getHeader(TenantContext.HEADER)
            );
        } catch (IllegalArgumentException ex) {
            reject(response, 400, "INVALID_TENANT", "非法租户标识");
            return;
        }

        String tenantId;
        if (claimedTenant != null) {
            if (!claimedTenant.equals(headerTenant)
                    && request.getHeader(TenantContext.HEADER) != null) {
                reject(response, 403, "TENANT_MISMATCH", "租户与令牌不一致");
                return;
            }
            tenantId = claimedTenant;
        } else {
            tenantId = headerTenant;
        }

        TenantContext.set(tenantId);
        response.setHeader(TenantContext.HEADER, tenantId);
        try {
            filterChain.doFilter(request, response);
        } finally {
            TenantContext.clear();
        }
    }

    private String tenantFromJwt() {
        Authentication authentication = SecurityContextHolder
                .getContext()
                .getAuthentication();
        if (!(authentication instanceof JwtAuthenticationToken token)) {
            return null;
        }
        Object claim = token.getTokenAttributes().get(TENANT_CLAIM);
        if (claim == null || String.valueOf(claim).isBlank()) {
            return null;
        }
        try {
            return TenantContext.normalize(String.valueOf(claim));
        } catch (IllegalArgumentException ex) {
            return null;
        }
    }

    private void reject(
            HttpServletResponse response,
            int status,
            String code,
            String message
    ) throws IOException {
        response.setStatus(status);
        response.setContentType("application/json");
        response.getWriter().write(
                "{\"code\":\"" + code + "\",\"message\":\"" + message + "\"}"
        );
    }
}
