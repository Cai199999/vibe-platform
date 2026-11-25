package com.vibeplatform.bff.resilience;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.http.client.ClientHttpRequestInterceptor;
import org.springframework.stereotype.Component;

/**
 * 三条 AI Core 通道的舱壁/熔断状态。
 *
 * <ul>
 *   <li>{@code short}：健康检查、管理指标等必须快速失败的调用；</li>
 *   <li>{@code interactive}：Supervisor / Architect 状态操作等交互式代理；</li>
 *   <li>{@code run}：同步长任务（Run / Direct Ops / 导出）。</li>
 * </ul>
 *
 * <p>三条通道各自独立熔断：健康检查不会因为长任务熔断而失去探测能力，
 * 长任务也不会因为健康检查短暂失败而全部被拒绝。</p>
 */
@Component
public class AiCoreResilienceRegistry {

    public static final String SHORT = "short";
    public static final String INTERACTIVE = "interactive";
    public static final String RUN = "run";

    private final boolean enabled;
    private final Map<String, AiCoreGuard> guards;

    public AiCoreResilienceRegistry(AiCoreResilienceProperties properties) {
        this.enabled = Boolean.TRUE.equals(properties.enabled());
        LinkedHashMap<String, AiCoreGuard> created = new LinkedHashMap<>();
        created.put(SHORT, guard(
                SHORT,
                properties.shortMaxConcurrent(),
                properties.shortAcquireTimeoutMs(),
                properties
        ));
        created.put(INTERACTIVE, guard(
                INTERACTIVE,
                properties.interactiveMaxConcurrent(),
                properties.interactiveAcquireTimeoutMs(),
                properties
        ));
        created.put(RUN, guard(
                RUN,
                properties.runMaxConcurrent(),
                properties.runAcquireTimeoutMs(),
                properties
        ));
        this.guards = Collections.unmodifiableMap(created);
    }

    public ClientHttpRequestInterceptor interceptor(String channel) {
        return new AiCoreResilienceInterceptor(guard(channel), enabled);
    }

    public AiCoreGuard guard(String channel) {
        AiCoreGuard guard = guards.get(channel);
        if (guard == null) {
            throw new IllegalArgumentException("unknown AI Core channel: " + channel);
        }
        return guard;
    }

    public Map<String, Object> snapshot() {
        LinkedHashMap<String, Object> data = new LinkedHashMap<>();
        data.put("enabled", enabled);
        LinkedHashMap<String, Object> channels = new LinkedHashMap<>();
        guards.forEach((name, guard) -> channels.put(name, guard.snapshot()));
        data.put("channels", channels);
        return data;
    }

    private static AiCoreGuard guard(
            String name,
            int maxConcurrent,
            int acquireTimeoutMs,
            AiCoreResilienceProperties properties
    ) {
        return new AiCoreGuard(
                name,
                maxConcurrent,
                acquireTimeoutMs,
                properties.failureThreshold(),
                properties.openDurationMs(),
                properties.halfOpenMaxCalls()
        );
    }
}
