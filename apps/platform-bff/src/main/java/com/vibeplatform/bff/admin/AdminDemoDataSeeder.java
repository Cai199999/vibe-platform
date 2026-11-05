package com.vibeplatform.bff.admin;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.stereotype.Component;

/**
 * 本地演示数据开关。
 *
 * <p>默认关闭。开启方式：</p>
 * <pre>
 * ADMIN_METRICS_DEMO_SEED=true
 * </pre>
 *
 * <p>仅在内存中还没有任何真实 Run 时才写入样例，避免污染真实指标；
 * API 响应会带 {@code demo=true}，前端也会展示“演示数据”徽标。</p>
 */
@Component
public class AdminDemoDataSeeder implements ApplicationRunner {

    private static final Logger log =
            LoggerFactory.getLogger(AdminDemoDataSeeder.class);

    private final AdminMetricsService metricsService;
    private final boolean enabled;

    public AdminDemoDataSeeder(
            AdminMetricsService metricsService,
            @Value("${platform.admin.demo-seed:false}") boolean enabled
    ) {
        this.metricsService = metricsService;
        this.enabled = enabled;
    }

    @Override
    public void run(ApplicationArguments args) {
        if (!enabled) {
            return;
        }
        boolean seeded = metricsService.seedDemoData();
        log.info("admin demo seed enabled={} seeded={}", enabled, seeded);
    }
}
