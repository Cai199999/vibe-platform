package com.vibeplatform.bff.resilience;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.LongSupplier;

/**
 * 单个下游通道的舱壁 + 熔断器。
 *
 * <p>舱壁用信号量限制同时阻塞在 AI Core 上的线程数；熔断器在连续失败
 * 达到阈值后快速失败，冷却期后放少量探测请求（half-open）。</p>
 *
 * <p>这是有意的轻量实现：不引入额外框架，状态只存在进程内。多副本部署
 * 时每个副本独立判断，后续可替换为 Resilience4j + 共享指标。</p>
 */
public final class AiCoreGuard {

    public enum State {
        CLOSED,
        OPEN,
        HALF_OPEN
    }

    private final String name;
    private final Semaphore permits;
    private final int maxConcurrent;
    private final int acquireTimeoutMs;
    private final int failureThreshold;
    private final long openDurationMs;
    private final int halfOpenMaxCalls;
    private final LongSupplier clock;

    private final AtomicReference<State> state = new AtomicReference<>(State.CLOSED);
    private final AtomicInteger consecutiveFailures = new AtomicInteger();
    private final AtomicLong openUntilMillis = new AtomicLong();
    private final AtomicInteger halfOpenInFlight = new AtomicInteger();

    public AiCoreGuard(
            String name,
            int maxConcurrent,
            int acquireTimeoutMs,
            int failureThreshold,
            long openDurationMs,
            int halfOpenMaxCalls
    ) {
        this(
                name,
                maxConcurrent,
                acquireTimeoutMs,
                failureThreshold,
                openDurationMs,
                halfOpenMaxCalls,
                System::currentTimeMillis
        );
    }

    AiCoreGuard(
            String name,
            int maxConcurrent,
            int acquireTimeoutMs,
            int failureThreshold,
            long openDurationMs,
            int halfOpenMaxCalls,
            LongSupplier clock
    ) {
        this.name = name;
        this.maxConcurrent = Math.max(1, maxConcurrent);
        this.acquireTimeoutMs = Math.max(0, acquireTimeoutMs);
        this.failureThreshold = Math.max(1, failureThreshold);
        this.openDurationMs = Math.max(1, openDurationMs);
        this.halfOpenMaxCalls = Math.max(1, halfOpenMaxCalls);
        this.clock = clock;
        this.permits = new Semaphore(this.maxConcurrent, true);
    }

    /**
     * 尝试获取一个执行许可；超过等待预算返回 false，由调用方快速失败。
     */
    public boolean tryAcquirePermit() throws InterruptedException {
        if (acquireTimeoutMs <= 0) {
            return permits.tryAcquire();
        }
        return permits.tryAcquire(acquireTimeoutMs, TimeUnit.MILLISECONDS);
    }

    public void releasePermit() {
        permits.release();
    }

    /**
     * 熔断器是否允许本次调用。open 状态在冷却期结束后自动进入 half-open，
     * 仅放行少量探测请求。
     */
    public boolean allowRequest() {
        State current = state.get();
        if (current == State.CLOSED) {
            return true;
        }
        if (current == State.OPEN) {
            if (clock.getAsLong() < openUntilMillis.get()) {
                return false;
            }
            if (state.compareAndSet(State.OPEN, State.HALF_OPEN)) {
                halfOpenInFlight.set(0);
            }
            current = state.get();
        }
        if (current == State.HALF_OPEN) {
            return halfOpenInFlight.incrementAndGet() <= halfOpenMaxCalls;
        }
        return true;
    }

    public void recordSuccess() {
        consecutiveFailures.set(0);
        halfOpenInFlight.set(0);
        state.set(State.CLOSED);
    }

    public void recordFailure() {
        int failures = consecutiveFailures.incrementAndGet();
        State current = state.get();
        if (current == State.HALF_OPEN || failures >= failureThreshold) {
            if (state.getAndSet(State.OPEN) != State.OPEN) {
                openUntilMillis.set(clock.getAsLong() + openDurationMs);
            }
        }
    }

    /** 仅供只读健康检查 / 管理端点使用。 */
    public Map<String, Object> snapshot() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("name", name);
        State current = state.get();
        data.put("state", current.name());
        data.put("maxConcurrent", maxConcurrent);
        data.put("inFlight", maxConcurrent - permits.availablePermits());
        data.put("consecutiveFailures", consecutiveFailures.get());
        long remaining = current == State.OPEN
                ? Math.max(0, openUntilMillis.get() - clock.getAsLong())
                : 0;
        data.put("openRemainingMs", remaining);
        return data;
    }
}
