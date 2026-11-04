package com.vibeplatform.bff.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Python AI Core 的内部调用配置。
 *
 * <p>容器内默认通过 Docker 自定义网络的服务名访问：
 * {@code http://python-ai:8100}。不要在业务代码里硬编码地址。</p>
 *
 * <p>健康检查与 Run 执行使用不同的读取超时：健康检查必须快速失败，
 * 而一次 Run 可能包含多轮模型调用与 npm 构建，需要分钟级预算。</p>
 */
@ConfigurationProperties(prefix = "ai-core")
public record AiCoreProperties(
        String baseUrl,
        int connectTimeoutMs,
        int readTimeoutMs,
        int runConnectTimeoutMs,
        int runReadTimeoutMs
) {

    public AiCoreProperties {
        if (baseUrl == null || baseUrl.isBlank()) {
            baseUrl = "http://localhost:8100";
        }
        // 去掉尾部斜杠，避免 RestClient 拼接出双斜杠路径。
        baseUrl = baseUrl.replaceAll("/+$", "");
        if (connectTimeoutMs <= 0) {
            connectTimeoutMs = 2_000;
        }
        if (readTimeoutMs <= 0) {
            readTimeoutMs = 3_000;
        }
        if (runConnectTimeoutMs <= 0) {
            runConnectTimeoutMs = connectTimeoutMs;
        }
        if (runReadTimeoutMs <= 0) {
            // 单次 Run 含最多 4 次构建 + 模型调用，M1 给 5 分钟预算。
            runReadTimeoutMs = 300_000;
        }
    }
}
