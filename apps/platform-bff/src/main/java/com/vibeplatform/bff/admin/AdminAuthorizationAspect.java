package com.vibeplatform.bff.admin;

import jakarta.servlet.http.HttpServletRequest;
import java.util.Locale;
import java.util.Set;
import org.aspectj.lang.JoinPoint;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;
import org.springframework.stereotype.Component;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

/**
 * {@link IsAdmin} 的权限切面。
 *
 * <p>M3.1 阶段通过 Mock Header 校验：{@code X-User-Role: admin}。
 * 同时兼容架构文档中的角色命名（{@code platform_admin} /
 * {@code tenant_admin} / {@code tenant_operator}），便于后续接入真实 JWT
 * 时前端可以直接传真实角色而不用改协议。</p>
 *
 * <p>注意：前端隐藏入口只是体验优化，真正的权限边界在这里。</p>
 */
@Aspect
@Component
public class AdminAuthorizationAspect {

    /**
     * 允许访问管理端的角色。
     *
     * <ul>
     *   <li>{@code admin}：M3.1 Mock 别名，等价于平台管理员；</li>
     *   <li>{@code platform_admin}：跨租户聚合与平台资源管理；</li>
     *   <li>{@code tenant_admin} / {@code tenant_operator}：租户内管理 /
     *       只读指标。</li>
     * </ul>
     */
    private static final Set<String> ADMIN_ROLES = Set.of(
            "admin",
            "platform_admin",
            "tenant_admin",
            "tenant_operator"
    );

    @Before("@within(com.vibeplatform.bff.admin.IsAdmin) "
            + "|| @annotation(com.vibeplatform.bff.admin.IsAdmin)")
    public void authorize(JoinPoint joinPoint) {
        ServletRequestAttributes attributes =
                (ServletRequestAttributes) RequestContextHolder.getRequestAttributes();
        if (attributes == null) {
            // 非 Web 线程（例如内部任务）没有请求上下文，一律拒绝。
            throw new AdminAccessDeniedException("ANONYMOUS");
        }
        HttpServletRequest request = attributes.getRequest();
        String role = request.getHeader("X-User-Role");
        String normalized = role == null ? "" : role.trim().toLowerCase(Locale.ROOT);
        if (!ADMIN_ROLES.contains(normalized)) {
            throw new AdminAccessDeniedException(role);
        }
    }
}
