package com.vibeplatform.bff.projectsource;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 源码快照的确定性校验与归一化。
 *
 * <p>路径安全必须在 BFF 再做一次：前端 / AI Core 都可能传入带目录穿越、
 * 绝对路径或重复分隔符的 key，不能让它直接成为 MinIO object key。</p>
 */
final class ProjectSourceValidator {

    static final int MAX_FILES = 200;
    static final int MAX_PATH_LENGTH = 400;
    static final int MAX_FILE_BYTES = 2 * 1024 * 1024;
    static final long MAX_TOTAL_BYTES = 20L * 1024 * 1024;

    private ProjectSourceValidator() {
    }

    static String normalizeTenant(String tenantId) {
        if (tenantId == null || tenantId.isBlank()) {
            return "tenant_local";
        }
        String value = tenantId.trim();
        if (!value.matches("[A-Za-z0-9_-]{1,64}")) {
            throw ProjectSourceException.invalid(
                    "tenantId 只允许 1-64 位字母、数字、下划线或短横线"
            );
        }
        return value;
    }

    static String normalizeProjectId(String projectId) {
        if (projectId == null || !projectId.matches("[A-Za-z0-9_-]{1,64}")) {
            throw ProjectSourceException.invalid(
                    "projectId 只允许 1-64 位字母、数字、下划线或短横线"
            );
        }
        return projectId;
    }

    /**
     * 归一化文件树：拒绝不安全路径、重复归一化后的路径与超限内容。
     */
    static LinkedHashMap<String, String> normalizeFiles(Map<String, String> raw) {
        if (raw == null || raw.isEmpty()) {
            throw ProjectSourceException.invalid("files 不能为空");
        }
        if (raw.size() > MAX_FILES) {
            throw ProjectSourceException.invalid(
                    "文件数量超过上限 " + MAX_FILES
            );
        }
        LinkedHashMap<String, String> normalized = new LinkedHashMap<>();
        long totalBytes = 0;
        for (Map.Entry<String, String> entry : raw.entrySet()) {
            String path = normalizePath(entry.getKey());
            if (normalized.containsKey(path)) {
                throw ProjectSourceException.invalid(
                        "文件路径重复（归一化后）: " + path
                );
            }
            String content = entry.getValue() == null ? "" : entry.getValue();
            long bytes = content.getBytes(StandardCharsets.UTF_8).length;
            if (bytes > MAX_FILE_BYTES) {
                throw ProjectSourceException.invalid(
                        "文件超过 2MB 上限: " + path
                );
            }
            totalBytes += bytes;
            if (totalBytes > MAX_TOTAL_BYTES) {
                throw ProjectSourceException.invalid(
                        "源码总大小超过 20MB 上限"
                );
            }
            normalized.put(path, content);
        }
        return normalized;
    }

    static String normalizePath(String rawPath) {
        if (rawPath == null || rawPath.isBlank()) {
            throw ProjectSourceException.invalid("文件路径不能为空");
        }
        String path = rawPath.replace('\\', '/').trim();
        while (path.startsWith("./")) {
            path = path.substring(2);
        }
        if (path.startsWith("/") || path.matches("^[A-Za-z]:.*")) {
            throw ProjectSourceException.invalid("不允许绝对路径: " + rawPath);
        }
        List<String> segments = new ArrayList<>();
        for (String segment : path.split("/")) {
            if (segment.isEmpty() || ".".equals(segment)) {
                continue;
            }
            if ("..".equals(segment)) {
                throw ProjectSourceException.invalid("路径不允许包含 ..: " + rawPath);
            }
            if (segment.length() > 120 || segment.matches(".*[\\p{Cntrl}].*")) {
                throw ProjectSourceException.invalid("文件路径包含非法字符: " + rawPath);
            }
            segments.add(segment);
        }
        if (segments.isEmpty()) {
            throw ProjectSourceException.invalid("文件路径不能为空");
        }
        String normalized = String.join("/", segments);
        if (normalized.length() > MAX_PATH_LENGTH) {
            throw ProjectSourceException.invalid("文件路径超过 400 字符: " + rawPath);
        }
        return normalized;
    }

    static String resolveEntryFile(String entryFile, Map<String, String> files) {
        if (entryFile != null && !entryFile.isBlank()) {
            String normalized = normalizePath(entryFile);
            if (!files.containsKey(normalized)) {
                throw ProjectSourceException.invalid(
                        "entryFile 必须存在于 files 中: " + normalized
                );
            }
            return normalized;
        }
        if (files.containsKey("src/App.jsx")) {
            return "src/App.jsx";
        }
        return files.keySet().iterator().next();
    }

    static String contentHash(Map<String, String> files) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            files.entrySet().stream()
                    .sorted(Map.Entry.comparingByKey())
                    .forEach(entry -> {
                        digest.update(entry.getKey().getBytes(StandardCharsets.UTF_8));
                        digest.update((byte) 0);
                        digest.update(entry.getValue().getBytes(StandardCharsets.UTF_8));
                        digest.update((byte) 0);
                    });
            return HexFormat.of().formatHex(digest.digest());
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException("SHA-256 不可用", ex);
        }
    }

    static String sha256Text(String text) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(
                    digest.digest(text.getBytes(StandardCharsets.UTF_8))
            );
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException("SHA-256 不可用", ex);
        }
    }

    static String normalizeSourceName(String sourceName) {
        if (sourceName == null || sourceName.isBlank()) {
            return null;
        }
        String value = sourceName.replace('\\', '/').trim();
        int index = value.lastIndexOf('/');
        if (index >= 0) {
            value = value.substring(index + 1);
        }
        if (value.length() > 255) {
            value = value.substring(value.length() - 255);
        }
        return value;
    }

    static String normalizeProjectName(String projectName) {
        if (projectName == null || projectName.isBlank()) {
            return null;
        }
        String value = projectName.trim();
        return value.length() <= 200 ? value : value.substring(0, 200);
    }

    static String objectPrefix(String tenantId, String projectId, int revision) {
        String safeTenant = tenantId.toLowerCase(Locale.ROOT);
        return "project-sources/" + safeTenant + "/" + projectId + "/r" + revision + "/";
    }
}
