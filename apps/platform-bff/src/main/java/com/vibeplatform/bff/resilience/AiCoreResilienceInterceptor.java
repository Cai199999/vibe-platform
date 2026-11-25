package com.vibeplatform.bff.resilience;

import com.vibeplatform.bff.common.TraceContext;
import java.io.IOException;
import org.springframework.http.HttpRequest;
import org.springframework.http.client.ClientHttpRequestExecution;
import org.springframework.http.client.ClientHttpRequestInterceptor;
import org.springframework.http.client.ClientHttpResponse;

/**
 * 包裹 AI Core 调用的舱壁 + 熔断 + trace 透传。
 *
 * <p>顺序很重要：先拿许可，再判断熔断。这样 half-open 探测不会在没有
 * 许可的情况下无限增加计数；熔断打开时请求立即失败，不占用线程。</p>
 */
public final class AiCoreResilienceInterceptor implements ClientHttpRequestInterceptor {

    private final AiCoreGuard guard;
    private final boolean enabled;

    public AiCoreResilienceInterceptor(AiCoreGuard guard, boolean enabled) {
        this.guard = guard;
        this.enabled = enabled;
    }

    @Override
    public ClientHttpResponse intercept(
            HttpRequest request,
            byte[] body,
            ClientHttpRequestExecution execution
    ) throws IOException {
        String traceId = TraceContext.current();
        if (traceId != null && !traceId.isBlank()) {
            request.getHeaders().set(TraceContext.HEADER, traceId);
        }
        if (!enabled) {
            return execution.execute(request, body);
        }
        boolean acquired;
        try {
            acquired = guard.tryAcquirePermit();
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
            throw new AiCoreGuardException(
                    "AI_CORE_BUSY",
                    "等待 AI Core 执行许可被中断"
            );
        }
        if (!acquired) {
            // 舱壁已满：立即失败，把 BFF 线程留给健康检查等非 AI 接口。
            throw new AiCoreGuardException(
                    "AI_CORE_BUSY",
                    "AI Core 并发已达上限，请稍后重试"
            );
        }
        if (!guard.allowRequest()) {
            guard.releasePermit();
            throw new AiCoreGuardException(
                    "AI_CORE_CIRCUIT_OPEN",
                    "AI Core 熔断中，请稍后重试"
            );
        }
        try {
            ClientHttpResponse response = execution.execute(request, body);
            int status = response.getStatusCode().value();
            if (status >= 500) {
                guard.recordFailure();
            } else {
                guard.recordSuccess();
            }
            return response;
        } catch (IOException | RuntimeException ex) {
            guard.recordFailure();
            throw ex;
        } finally {
            guard.releasePermit();
        }
    }
}
