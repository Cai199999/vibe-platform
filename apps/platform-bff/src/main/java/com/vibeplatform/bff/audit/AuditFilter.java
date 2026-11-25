package com.vibeplatform.bff.audit;

import com.vibeplatform.bff.common.TraceContext;
import com.vibeplatform.bff.identity.TenantContext;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Set;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.autoconfigure.security.SecurityProperties;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 写操作审计：POST / PUT / PATCH / DELETE 记录到 MySQL。
 *
 * <p>只记录元数据，不落请求体，避免把客户数据写进审计表。</p>
 */
@Component
@Order(SecurityProperties.DEFAULT_FILTER_ORDER + 20)
public class AuditFilter extends OncePerRequestFilter {

    private static final Set<String> MUTATING_METHODS =
            Set.of("POST", "PUT", "PATCH", "DELETE");

    private final ObjectProvider<AuditEventRepository> repositoryProvider;

    public AuditFilter(ObjectProvider<AuditEventRepository> repositoryProvider) {
        this.repositoryProvider = repositoryProvider;
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain
    ) throws ServletException, IOException {
        try {
            filterChain.doFilter(request, response);
        } finally {
            if (shouldRecord(request)) {
                AuditEventRepository repository = repositoryProvider.getIfAvailable();
                if (repository != null) {
                    repository.record(
                            TenantContext.currentOrDefault(),
                            TraceContext.current(),
                            actor(request),
                            request.getMethod(),
                            request.getRequestURI(),
                            response.getStatus(),
                            detail(request)
                    );
                }
            }
        }
    }

    private boolean shouldRecord(HttpServletRequest request) {
        return MUTATING_METHODS.contains(request.getMethod())
                && request.getRequestURI().startsWith("/api/");
    }

    private String actor(HttpServletRequest request) {
        String userId = request.getHeader("X-User-Id");
        if (userId != null && !userId.isBlank()) {
            return userId.trim();
        }
        String role = request.getHeader("X-User-Role");
        return role == null ? "" : role.trim();
    }

    private String detail(HttpServletRequest request) {
        String userAgent = request.getHeader("User-Agent");
        if (userAgent == null || userAgent.isBlank()) {
            return null;
        }
        return userAgent.length() > 240 ? userAgent.substring(0, 240) : userAgent;
    }
}
