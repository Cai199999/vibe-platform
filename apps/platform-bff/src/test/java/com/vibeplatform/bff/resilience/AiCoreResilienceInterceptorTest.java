package com.vibeplatform.bff.resilience;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.vibeplatform.bff.common.TraceContext;
import java.net.URI;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.mock.http.client.MockClientHttpRequest;
import org.springframework.mock.http.client.MockClientHttpResponse;

class AiCoreResilienceInterceptorTest {

    @AfterEach
    void clearTrace() {
        TraceContext.clear();
    }

    @Test
    void rejectsWithBusyWhenNoPermitAvailable() throws Exception {
        AiCoreGuard guard = new AiCoreGuard("run", 1, 0, 4, 1_000, 1);
        assertThat(guard.tryAcquirePermit()).isTrue();
        AiCoreResilienceInterceptor interceptor =
                new AiCoreResilienceInterceptor(guard, true);

        assertThatThrownBy(() -> interceptor.intercept(
                new MockClientHttpRequest(HttpMethod.GET, URI.create("/x")),
                new byte[0],
                (request, body) -> new MockClientHttpResponse(new byte[0], HttpStatus.OK)
        ))
                .isInstanceOf(AiCoreGuardException.class)
                .extracting(error -> ((AiCoreGuardException) error).code())
                .isEqualTo("AI_CORE_BUSY");
        guard.releasePermit();
    }

    @Test
    void rejectsFastWhenCircuitOpen() {
        AiCoreGuard guard = new AiCoreGuard("short", 2, 0, 1, 1_000, 1);
        guard.recordFailure();
        AiCoreResilienceInterceptor interceptor =
                new AiCoreResilienceInterceptor(guard, true);

        assertThatThrownBy(() -> interceptor.intercept(
                new MockClientHttpRequest(HttpMethod.GET, URI.create("/x")),
                new byte[0],
                (request, body) -> new MockClientHttpResponse(new byte[0], HttpStatus.OK)
        ))
                .isInstanceOf(AiCoreGuardException.class)
                .extracting(error -> ((AiCoreGuardException) error).code())
                .isEqualTo("AI_CORE_CIRCUIT_OPEN");
    }

    @Test
    void propagatesTraceIdAndRecordsServerFailure() throws Exception {
        AiCoreGuard guard = new AiCoreGuard("interactive", 2, 0, 1, 1_000, 1);
        AiCoreResilienceInterceptor interceptor =
                new AiCoreResilienceInterceptor(guard, true);
        MockClientHttpRequest request =
                new MockClientHttpRequest(HttpMethod.GET, URI.create("/x"));
        TraceContext.set("trc_test");

        interceptor.intercept(
                request,
                new byte[0],
                (req, body) -> new MockClientHttpResponse(
                        new byte[0],
                        HttpStatus.SERVICE_UNAVAILABLE
                )
        );

        assertThat(request.getHeaders().getFirst(TraceContext.HEADER))
                .isEqualTo("trc_test");
        assertThat(guard.snapshot()).containsEntry("state", "OPEN");
    }

    @Test
    void disabledInterceptorIsPassThrough() throws Exception {
        AiCoreGuard guard = new AiCoreGuard("run", 1, 0, 1, 1_000, 1);
        AiCoreResilienceInterceptor interceptor =
                new AiCoreResilienceInterceptor(guard, false);

        MockClientHttpResponse response = (MockClientHttpResponse) interceptor.intercept(
                new MockClientHttpRequest(HttpMethod.GET, URI.create("/x")),
                new byte[0],
                (request, body) -> new MockClientHttpResponse(new byte[0], HttpStatus.OK)
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(guard.snapshot()).containsEntry("state", "CLOSED");
    }
}
