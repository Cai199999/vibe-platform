package com.vibeplatform.bff.health;

import java.time.Instant;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * 平台健康检查。
 *
 * <p>{@code GET /api/v1/health} 会主动调用 Python AI Core 的
 * {@code GET /api/v1/internal/ping}，因此它同时验证：</p>
 * <ol>
 *   <li>Java BFF 进程与 Web 容器可用；</li>
 *   <li>Docker 自定义网络内 {@code java-bff -> python-ai} 的 DNS 与 HTTP 连通；</li>
 *   <li>Python AI Core 的 FastAPI 应用已启动。</li>
 * </ol>
 *
 * <p>下游不可用时返回 503 + {@code status=DEGRADED}，不抛出 500，
 * 便于容器 healthcheck 与前端区分“BFF 挂了”和“AI Core 挂了”。</p>
 */
@RestController
@RequestMapping("/api/v1")
public class HealthController {

    private static final Logger log = LoggerFactory.getLogger(HealthController.class);
    private static final String SERVICE_NAME = "java-bff";
    private static final String PYTHON_PING_PATH = "/api/v1/internal/ping";

    private final RestClient aiCoreRestClient;
    private final String applicationVersion;

    public HealthController(
            RestClient aiCoreRestClient,
            @Value("${info.app.version:0.1.0}") String applicationVersion
    ) {
        this.aiCoreRestClient = aiCoreRestClient;
        this.applicationVersion = applicationVersion;
    }

    @GetMapping("/health")
    public ResponseEntity<HealthResponse> health() {
        Instant timestamp = Instant.now();

        try {
            Map<String, Object> pythonPing = aiCoreRestClient.get()
                    .uri(PYTHON_PING_PATH)
                    .retrieve()
                    .body(new ParameterizedTypeReference<Map<String, Object>>() {
                    });

            boolean pythonUp = pythonPing != null
                    && "UP".equalsIgnoreCase(String.valueOf(pythonPing.get("status")));

            HealthResponse.DependencyStatus pythonStatus = new HealthResponse.DependencyStatus(
                    pythonUp ? "UP" : "DOWN",
                    "python-ai",
                    pythonPing,
                    pythonUp ? null : "Python AI Core 返回了非 UP 状态"
            );

            HealthResponse response = new HealthResponse(
                    pythonUp ? "UP" : "DEGRADED",
                    SERVICE_NAME,
                    applicationVersion,
                    timestamp,
                    Map.of("pythonAi", pythonStatus)
            );

            return pythonUp
                    ? ResponseEntity.ok(response)
                    : ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(response);
        } catch (RestClientException ex) {
            log.warn("Python AI Core ping failed: {}", ex.getMessage());
            HealthResponse response = new HealthResponse(
                    "DEGRADED",
                    SERVICE_NAME,
                    applicationVersion,
                    timestamp,
                    Map.of("pythonAi", new HealthResponse.DependencyStatus(
                            "DOWN",
                            "python-ai",
                            null,
                            ex.getMessage()
                    ))
            );
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(response);
        }
    }
}
