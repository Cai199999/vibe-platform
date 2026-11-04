package com.vibeplatform.bff.config;

import java.net.http.HttpClient;
import java.time.Duration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

/**
 * AI Core HTTP 客户端。
 *
 * <p>使用 JDK HttpClient，显式设置连接/读取超时：</p>
 * <ul>
 *   <li>{@code aiCoreRestClient}：短超时，供健康检查使用，下游变慢时快速失败；</li>
 *   <li>{@code aiCoreRunRestClient}：长超时，供 Run 执行代理使用，覆盖
 *       模型生成、沙盒构建与最多 3 次修复。</li>
 * </ul>
 *
 * <p>{@code aiCoreRestClient} 标记为 {@link Primary}，其他按类型注入的旧代码
 * 行为不变；Run 服务通过 {@code @Qualifier} 显式选择长超时客户端。</p>
 */
@Configuration
public class AiCoreClientConfig {

    @Bean
    @Primary
    public RestClient aiCoreRestClient(AiCoreProperties properties) {
        return buildRestClient(
                properties.baseUrl(),
                properties.connectTimeoutMs(),
                properties.readTimeoutMs()
        );
    }

    @Bean
    public RestClient aiCoreRunRestClient(AiCoreProperties properties) {
        return buildRestClient(
                properties.baseUrl(),
                properties.runConnectTimeoutMs(),
                properties.runReadTimeoutMs()
        );
    }

    private RestClient buildRestClient(String baseUrl, int connectTimeoutMs, int readTimeoutMs) {
        // 强制 HTTP/1.1：JDK HttpClient 默认 HTTP/2，会向 Uvicorn 发送 h2c
        // Upgrade 请求。Uvicorn(h11) 不支持升级，日志报 "Unsupported upgrade
        // request"，POST 请求体还会丢失（表现为 FastAPI 422 missing body）。
        HttpClient httpClient = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .build();

        JdkClientHttpRequestFactory requestFactory =
                new JdkClientHttpRequestFactory(httpClient);
        requestFactory.setReadTimeout(Duration.ofMillis(readTimeoutMs));

        return RestClient.builder()
                .baseUrl(baseUrl)
                .requestFactory(requestFactory)
                .defaultHeader("Accept", "application/json")
                .build();
    }
}
