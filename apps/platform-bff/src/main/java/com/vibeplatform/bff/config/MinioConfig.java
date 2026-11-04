package com.vibeplatform.bff.config;

import io.minio.MinioClient;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * MinIO 客户端配置。
 *
 * <p>客户端为懒连接：启动时不会访问 MinIO，只有真正上传/下载时才建立连接。
 * 这样即使对象存储暂时不可用，BFF 仍可提供健康检查与只读接口。</p>
 */
@Configuration
public class MinioConfig {

    @Bean
    public MinioClient minioClient(MinioProperties properties) {
        return MinioClient.builder()
                .endpoint(properties.endpoint())
                .credentials(properties.accessKey(), properties.secretKey())
                .build();
    }
}
