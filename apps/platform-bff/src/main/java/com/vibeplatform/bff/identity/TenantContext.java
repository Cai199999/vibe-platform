package com.vibeplatform.bff.identity;

/**
 * 请求级租户上下文。
 *
 * <p>当前阶段由 {@code X-Tenant-Id} 解析；接入 OIDC 后可由 JWT claim 回填，
 * 下游仓储与审计统一读取该上下文，避免各接口各自解析租户。</p>
 */
public final class TenantContext {

    public static final String HEADER = "X-Tenant-Id";
    public static final String DEFAULT_TENANT = "tenant_local";

    private static final ThreadLocal<String> CURRENT = new ThreadLocal<>();

    private TenantContext() {
    }

    public static void set(String tenantId) {
        CURRENT.set(normalize(tenantId));
    }

    public static String current() {
        return CURRENT.get();
    }

    public static String currentOrDefault() {
        String value = CURRENT.get();
        return value == null ? DEFAULT_TENANT : value;
    }

    public static void clear() {
        CURRENT.remove();
    }

    public static String normalize(String tenantId) {
        if (tenantId == null || tenantId.isBlank()) {
            return DEFAULT_TENANT;
        }
        String value = tenantId.trim();
        if (!value.matches("[A-Za-z0-9_-]{1,64}")) {
            throw new IllegalArgumentException("非法 tenantId");
        }
        return value;
    }
}
