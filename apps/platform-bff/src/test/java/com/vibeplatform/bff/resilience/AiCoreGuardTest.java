package com.vibeplatform.bff.resilience;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;

class AiCoreGuardTest {

    @Test
    void bulkheadRejectsWhenPermitExhausted() throws Exception {
        AiCoreGuard guard = new AiCoreGuard("run", 1, 0, 4, 1_000, 1);

        assertThat(guard.tryAcquirePermit()).isTrue();
        assertThat(guard.tryAcquirePermit()).isFalse();
        guard.releasePermit();

        assertThat(guard.tryAcquirePermit()).isTrue();
        guard.releasePermit();
    }

    @Test
    void circuitOpensAfterThresholdAndRecoversThroughHalfOpen() throws Exception {
        AtomicLong now = new AtomicLong(1_000);
        AiCoreGuard guard = new AiCoreGuard(
                "interactive",
                4,
                0,
                2,
                500,
                1,
                now::get
        );

        assertThat(guard.allowRequest()).isTrue();
        guard.recordFailure();
        assertThat(guard.allowRequest()).isTrue();
        guard.recordFailure();

        assertThat(guard.snapshot())
                .containsEntry("state", "OPEN");
        assertThat(guard.allowRequest()).isFalse();

        now.addAndGet(500);
        // half-open：只放行一个探测请求。
        assertThat(guard.allowRequest()).isTrue();
        assertThat(guard.allowRequest()).isFalse();

        guard.recordSuccess();
        assertThat(guard.snapshot()).containsEntry("state", "CLOSED");
        assertThat(guard.allowRequest()).isTrue();
    }

    @Test
    void halfOpenFailureReopensImmediately() {
        AtomicLong now = new AtomicLong(0);
        AiCoreGuard guard = new AiCoreGuard(
                "short",
                2,
                0,
                1,
                1_000,
                1,
                now::get
        );

        guard.recordFailure();
        assertThat(guard.allowRequest()).isFalse();

        now.addAndGet(1_000);
        assertThat(guard.allowRequest()).isTrue();
        guard.recordFailure();

        assertThat(guard.snapshot()).containsEntry("state", "OPEN");
        assertThat(guard.allowRequest()).isFalse();
    }

    @Test
    void snapshotReportsInFlight() throws Exception {
        AiCoreGuard guard = new AiCoreGuard("short", 2, 0, 4, 1_000, 1);
        assertThat(guard.tryAcquirePermit()).isTrue();

        assertThat(guard.snapshot())
                .containsEntry("inFlight", 1)
                .containsEntry("maxConcurrent", 2);
        guard.releasePermit();
    }
}
