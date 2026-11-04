package com.vibeplatform.bff.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Python AI Core 的内部调用配置。
 *
 * <p>容器内默认通过 Docker 自定义网络的服务名访问：
 * {@code http://python-ai:8100}。不要在业务代码里硬编码地址。</p>
 */
@ConfigurationProperties(prefix = "ai-core")
public record AiCoreProperties(
        String baseUrl,
        int connectTimeoutMs,
        int readTimeoutMs
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
    }
}
