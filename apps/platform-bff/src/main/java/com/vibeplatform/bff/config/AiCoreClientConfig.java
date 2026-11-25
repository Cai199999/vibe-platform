package com.vibeplatform.bff.config;

import com.vibeplatform.bff.resilience.AiCoreResilienceRegistry;
import io.netty.channel.ChannelOption;
import java.net.http.HttpClient;
import java.time.Duration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.web.client.RestClient;
import org.springframework.web.reactive.function.client.WebClient;

/**
 * AI Core HTTP 客户端。
 *
 * <p>使用 JDK HttpClient，显式设置连接/读取超时：</p>
 * <ul>
 *   <li>{@code aiCoreRestClient}：短超时，供健康检查使用，下游变慢时快速失败；</li>
 *   <li>{@code aiCoreInteractiveRestClient}：15 秒，供 Supervisor /
 *       Architect 状态操作等交互式代理使用；</li>
 *   <li>{@code aiCoreRunRestClient}：长超时，供 Run 执行代理使用，覆盖
 *       模型生成、沙盒构建与最多 3 次修复。</li>
 * </ul>
 *
 * <p>三条通道各自挂载舱壁 + 熔断拦截器；{@code aiCoreRestClient} 标记为
 * {@link Primary}，其他按类型注入的旧代码行为不变。</p>
 */
@Configuration
public class AiCoreClientConfig {

    @Bean
    @Primary
    public RestClient aiCoreRestClient(
            AiCoreProperties properties,
            AiCoreResilienceRegistry resilience
    ) {
        return buildRestClient(
                properties.baseUrl(),
                properties.connectTimeoutMs(),
                properties.readTimeoutMs(),
                resilience.interceptor(AiCoreResilienceRegistry.SHORT)
        );
    }

    /**
     * 交互式代理：Supervisor 路由/派发/注册中心、Architect 澄清与决策。
     * 独立 15 秒预算，避免下游卡死时占用 300 秒的长任务线程。
     */
    @Bean
    public RestClient aiCoreInteractiveRestClient(
            AiCoreProperties properties,
            AiCoreResilienceRegistry resilience
    ) {
        return buildRestClient(
                properties.baseUrl(),
                properties.interactiveConnectTimeoutMs(),
                properties.interactiveReadTimeoutMs(),
                resilience.interceptor(AiCoreResilienceRegistry.INTERACTIVE)
        );
    }

    @Bean
    public RestClient aiCoreRunRestClient(
            AiCoreProperties properties,
            AiCoreResilienceRegistry resilience
    ) {
        return buildRestClient(
                properties.baseUrl(),
                properties.runConnectTimeoutMs(),
                properties.runReadTimeoutMs(),
                resilience.interceptor(AiCoreResilienceRegistry.RUN)
        );
    }

    /**
     * SSE 专用 WebClient。
     *
     * <p>只设置连接超时，不设置整体 responseTimeout：一次全栈 Run 可能持续
     * 数分钟，流式响应的生命周期由上游事件与客户端断开决定。Reactor Netty
     * 的事件循环在等待上游数据时不占用 Servlet 工作线程。</p>
     */
    @Bean
    public WebClient aiCoreStreamWebClient(AiCoreProperties properties) {
        reactor.netty.http.client.HttpClient httpClient =
                reactor.netty.http.client.HttpClient.create()
                        .option(
                                ChannelOption.CONNECT_TIMEOUT_MILLIS,
                                properties.runConnectTimeoutMs()
                        )
                        .compress(false);

        return WebClient.builder()
                .baseUrl(properties.baseUrl())
                .clientConnector(new ReactorClientHttpConnector(httpClient))
                .codecs(configurer ->
                        configurer.defaultCodecs().maxInMemorySize(1024 * 1024))
                .build();
    }

    private RestClient buildRestClient(
            String baseUrl,
            int connectTimeoutMs,
            int readTimeoutMs,
            org.springframework.http.client.ClientHttpRequestInterceptor resilienceInterceptor
    ) {
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
                .requestInterceptor(resilienceInterceptor)
                .defaultHeader("Accept", "application/json")
                .build();
    }
}
