package com.vibeplatform.bff.identity;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.security.SecurityProperties;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 平台级租户限流（令牌桶，按分钟配置）。
 *
 * <p>默认 600 次/分钟/租户，足以覆盖正常交互与 SSE 建连，同时防止单租户
 * 刷接口拖垮 BFF。健康检查、静态资源与 CORS 预检不计数。超限返回 429 +
 * {@code RATE_LIMITED}，并带 {@code Retry-After}。</p>
 *
 * <p>进程内实现：多副本部署时应替换为 Redis 令牌桶；当前与项目写锁一致，
 * 单实例本地限流先兜底。</p>
 */
@Component
@Order(SecurityProperties.DEFAULT_FILTER_ORDER + 20)
public class RateLimitFilter extends OncePerRequestFilter {

    private final boolean enabled;
    private final int limitPerMinute;
    private final double refillPerSecond;
    private final Map<String, Bucket> buckets = new ConcurrentHashMap<>();

    public RateLimitFilter(
            @Value("${vibe.rate-limit.enabled:true}") boolean enabled,
            @Value("${vibe.rate-limit.per-minute:600}") int limitPerMinute
    ) {
        this.enabled = enabled;
        this.limitPerMinute = Math.max(1, limitPerMinute);
        this.refillPerSecond = this.limitPerMinute / 60.0;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        if (!enabled) {
            return true;
        }
        String path = request.getRequestURI();
        return "OPTIONS".equalsIgnoreCase(request.getMethod())
                || path == null
                || path.startsWith("/api/v1/health")
                || path.startsWith("/actuator")
                || path.equals("/")
                || path.startsWith("/assets/");
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain
    ) throws ServletException, IOException {
        String tenant = TenantContext.currentOrDefault();
        Bucket bucket = buckets.computeIfAbsent(tenant, key -> new Bucket(limitPerMinute));
        if (!bucket.tryAcquire(refillPerSecond)) {
            int retryAfter = bucket.retryAfterSeconds(refillPerSecond);
            response.setStatus(429);
            response.setHeader("Retry-After", String.valueOf(retryAfter));
            response.setContentType("application/json");
            response.setCharacterEncoding("UTF-8");
            response.getWriter().write(
                    "{\"code\":\"RATE_LIMITED\",\"message\":\"请求过于频繁，请稍后重试\","
                            + "\"retryAfterSeconds\":" + retryAfter + "}"
            );
            return;
        }
        if (buckets.size() > 5_000) {
            evictIdleBuckets();
        }
        filterChain.doFilter(request, response);
    }

    private void evictIdleBuckets() {
        long cutoff = System.nanoTime() - 10L * 60 * 1_000_000_000L;
        buckets.entrySet().removeIf(entry -> entry.getValue().lastRefillNanos < cutoff);
    }

    private static final class Bucket {

        private double tokens;
        private long lastRefillNanos = System.nanoTime();

        private Bucket(int capacity) {
            this.tokens = capacity;
        }

        private synchronized boolean tryAcquire(double refillPerSecond) {
            refill(refillPerSecond);
            if (tokens >= 1.0) {
                tokens -= 1.0;
                return true;
            }
            return false;
        }

        private synchronized int retryAfterSeconds(double refillPerSecond) {
            refill(refillPerSecond);
            double missing = Math.max(0.0, 1.0 - tokens);
            return Math.max(1, (int) Math.ceil(missing / refillPerSecond));
        }

        private void refill(double refillPerSecond) {
            long now = System.nanoTime();
            double elapsedSeconds = (now - lastRefillNanos) / 1_000_000_000.0;
            if (elapsedSeconds <= 0) {
                return;
            }
            tokens = Math.min(
                    tokens + elapsedSeconds * refillPerSecond,
                    Math.max(1.0, refillPerSecond * 60.0)
            );
            lastRefillNanos = now;
        }
    }
}
