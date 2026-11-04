package com.vibeplatform.bff.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * MinIO 连接配置，全部来自环境变量。
 *
 * <p>M1 复用宿主机已有 MinIO；平台 Compose 不重复部署对象存储。</p>
 */
@ConfigurationProperties(prefix = "minio")
public record MinioProperties(
        String endpoint,
        String accessKey,
        String secretKey,
        String bucket,
        boolean secure
) {

    public MinioProperties {
        if (endpoint == null || endpoint.isBlank()) {
            endpoint = "http://localhost:9000";
        }
        if (bucket == null || bucket.isBlank()) {
            bucket = "vibe-platform";
        }
    }
}
