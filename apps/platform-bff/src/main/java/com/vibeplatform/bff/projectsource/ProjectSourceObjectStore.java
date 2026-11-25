package com.vibeplatform.bff.projectsource;

import com.vibeplatform.bff.config.MinioProperties;
import io.minio.BucketExistsArgs;
import io.minio.GetObjectArgs;
import io.minio.MakeBucketArgs;
import io.minio.MinioClient;
import io.minio.PutObjectArgs;
import io.minio.RemoveObjectArgs;
import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collection;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * 源码正文对象存储。
 *
 * <p>MinIO 只负责保存文件正文；路径、大小、哈希与对象键由 MySQL 元数据
 * 表持有。读取失败时返回稳定错误码，不把 MinIO 技术异常透给前端。</p>
 */
@Component
public class ProjectSourceObjectStore {

    private static final Logger log =
            LoggerFactory.getLogger(ProjectSourceObjectStore.class);

    private final MinioClient client;
    private final String bucket;
    private volatile boolean bucketReady = false;

    public ProjectSourceObjectStore(MinioClient client, MinioProperties properties) {
        this.client = client;
        this.bucket = properties.bucket();
    }

    /**
     * 写入快照全部文件，返回 path -> objectKey。
     */
    public Map<String, String> putFiles(
            String prefix,
            Map<String, String> files
    ) {
        ensureBucket();
        try {
            java.util.LinkedHashMap<String, String> keys = new java.util.LinkedHashMap<>();
            for (Map.Entry<String, String> entry : files.entrySet()) {
                String objectKey = prefix + entry.getKey();
                byte[] content = entry.getValue().getBytes(StandardCharsets.UTF_8);
                client.putObject(
                        PutObjectArgs.builder()
                                .bucket(bucket)
                                .object(objectKey)
                                .stream(new ByteArrayInputStream(content), content.length, -1)
                                .contentType("text/plain; charset=utf-8")
                                .build()
                );
                keys.put(entry.getKey(), objectKey);
            }
            return keys;
        } catch (Exception ex) {
            throw ProjectSourceException.unavailable(
                    "写入源码对象存储失败，请稍后重试",
                    ex
            );
        }
    }

    /**
     * 尽力删除一批对象。用于 MySQL 元数据写入失败后的补偿：
     * 删除失败只记录日志，不能覆盖原始异常。
     */
    public void deleteObjects(Collection<String> objectKeys) {
        if (objectKeys == null || objectKeys.isEmpty()) {
            return;
        }
        for (String objectKey : objectKeys) {
            try {
                client.removeObject(
                        RemoveObjectArgs.builder()
                                .bucket(bucket)
                                .object(objectKey)
                                .build()
                );
            } catch (Exception ex) {
                log.warn("cleanup orphan source object failed key={}", objectKey, ex);
            }
        }
    }

    public String readText(String objectKey) {
        ensureBucket();
        try (InputStream input = client.getObject(
                GetObjectArgs.builder()
                        .bucket(bucket)
                        .object(objectKey)
                        .build()
        )) {
            return new String(input.readAllBytes(), StandardCharsets.UTF_8);
        } catch (Exception ex) {
            throw ProjectSourceException.unavailable(
                    "读取源码对象存储失败，请稍后重试",
                    ex
            );
        }
    }

    private void ensureBucket() {
        if (bucketReady) {
            return;
        }
        synchronized (this) {
            if (bucketReady) {
                return;
            }
            try {
                if (!client.bucketExists(
                        BucketExistsArgs.builder().bucket(bucket).build()
                )) {
                    client.makeBucket(
                            MakeBucketArgs.builder().bucket(bucket).build()
                    );
                    log.info("created minio bucket {}", bucket);
                }
                bucketReady = true;
            } catch (Exception ex) {
                throw ProjectSourceException.unavailable(
                        "源码对象存储不可用，请稍后重试",
                        ex
                );
            }
        }
    }
}
