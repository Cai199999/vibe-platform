package com.vibeplatform.bff.run;

import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.stereotype.Component;

/**
 * runId -> sandboxId 的进程内映射。
 *
 * <p>BFF 目前是无状态的同步代理，不把 Run 落库。为了让
 * {@code GET /api/v1/runs/{runId}/export} 可以只凭 runId 找到沙盒，
 * 这里记录最近执行成功的 Run。容量固定，超出后淘汰最旧条目，避免
 * 长跑实例内存无限增长；前端仍可显式传 sandboxId 兜底（BFF 重启后
 * 内存映射丢失，但浏览器里的运行结果还在）。</p>
 */
@Component
public class RunSandboxRegistry {

    private static final int MAX_ENTRIES = 1_000;

    private final Map<String, String> sandboxes = new LinkedHashMap<>(16, 0.75f, true);

    /**
     * 记录映射；按访问顺序淘汰最旧条目。
     */
    public synchronized void record(String runId, String sandboxId) {
        if (runId == null || runId.isBlank() || sandboxId == null || sandboxId.isBlank()) {
            return;
        }
        sandboxes.put(runId, sandboxId);
        while (sandboxes.size() > MAX_ENTRIES) {
            String oldest = sandboxes.keySet().iterator().next();
            sandboxes.remove(oldest);
        }
    }

    /**
     * 查找 runId 对应的沙盒；不存在返回 null。
     */
    public synchronized String find(String runId) {
        return sandboxes.get(runId);
    }
}
