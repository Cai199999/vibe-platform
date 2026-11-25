package com.vibeplatform.bff.run;

import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * runId -> sandboxId 映射。
 *
 * <p>内存 LRU 保证单副本高频读取；配置了 Redis 时同步写一份带 TTL 的
 * 副本，使 BFF 重启或多副本部署后仍能凭 runId 找到沙盒。Redis 不可用
 * 只记录日志，不影响内存路径。</p>
 */
@Component
public class RunSandboxRegistry {

    private static final Logger log = LoggerFactory.getLogger(RunSandboxRegistry.class);
    private static final int MAX_ENTRIES = 1_000;
    private static final Duration TTL = Duration.ofDays(7);
    private static final String KEY_PREFIX = "vibe:bff:run-sandbox:";

    private final Map<String, String> sandboxes = new LinkedHashMap<>(16, 0.75f, true);
    private final StringRedisTemplate redis;

    public RunSandboxRegistry() {
        this.redis = null;
    }

    @Autowired
    public RunSandboxRegistry(ObjectProvider<StringRedisTemplate> redisProvider) {
        this.redis = redisProvider.getIfAvailable();
    }

    /**
     * 记录映射；按访问顺序淘汰最旧条目，并写 Redis 副本。
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
        if (redis != null) {
            try {
                redis.opsForValue().set(key(runId), sandboxId, TTL);
            } catch (RuntimeException ex) {
                log.warn("persist run-sandbox mapping failed runId={}: {}",
                        runId, ex.getMessage());
            }
        }
    }

    /**
     * 查找 runId 对应的沙盒；内存未命中时回源 Redis。
     */
    public synchronized String find(String runId) {
        String cached = sandboxes.get(runId);
        if (cached != null || redis == null || runId == null || runId.isBlank()) {
            return cached;
        }
        try {
            String persisted = redis.opsForValue().get(key(runId));
            if (persisted != null && !persisted.isBlank()) {
                sandboxes.put(runId, persisted);
                return persisted;
            }
        } catch (RuntimeException ex) {
            log.warn("load run-sandbox mapping failed runId={}: {}",
                    runId, ex.getMessage());
        }
        return null;
    }

    /**
     * 当前仍存活的沙盒数。
     *
     * <p>多个 Run 可能复用同一个沙盒，因此按 sandboxId 去重。</p>
     */
    public synchronized int activeCount() {
        return new HashSet<>(sandboxes.values()).size();
    }

    /**
     * 沙盒被显式销毁后移除所有指向它的映射，避免看板继续计入。
     */
    public synchronized void remove(String sandboxId) {
        if (sandboxId == null || sandboxId.isBlank()) {
            return;
        }
        List<String> removedRuns = new ArrayList<>();
        sandboxes.entrySet().removeIf(entry -> {
            if (sandboxId.equals(entry.getValue())) {
                removedRuns.add(entry.getKey());
                return true;
            }
            return false;
        });
        if (redis != null) {
            for (String runId : removedRuns) {
                try {
                    redis.delete(key(runId));
                } catch (RuntimeException ex) {
                    log.warn("delete run-sandbox mapping failed runId={}: {}",
                            runId, ex.getMessage());
                }
            }
        }
    }

    private static String key(String runId) {
        return KEY_PREFIX + runId;
    }
}
