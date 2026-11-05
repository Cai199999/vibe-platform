package com.vibeplatform.bff.project;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.multipart.MultipartFile;

/**
 * 项目导入代理：浏览器 -> Java BFF -> Python AI Core。
 *
 * <p>BFF 负责第一道扩展名 / 体积校验，并把 multipart 原样转发给 Python。
 * Python 负责解压、路径穿越 / zip 炸弹防护、npm install 与沙盒启动。</p>
 */
@Service
public class ProjectImportService {

    private static final Logger log = LoggerFactory.getLogger(ProjectImportService.class);
    private static final String IMPORT_PATH = "/api/v1/internal/sandbox/import";
    private static final long MAX_FILE_BYTES = 33_554_432L;
    private static final Set<String> ALLOWED_SUFFIXES = Set.of(".zip", ".png", ".jpg", ".jpeg");

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;

    public ProjectImportService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
    }

    /**
     * 校验并转发上传文件。
     *
     * @param projectId 前端预生成的项目 ID；为空时由 BFF 生成
     * @param file      上传的 zip 或图片
     * @return Python 返回的沙盒信息，或原样透传的错误体
     */
    public ResponseEntity<JsonNode> importProject(String projectId, MultipartFile file) {
        if (file == null || file.isEmpty()) {
            return error(HttpStatus.BAD_REQUEST, "IMPORT_EMPTY_FILE", "上传文件为空");
        }
        String filename = sanitizeFilename(file.getOriginalFilename());
        String suffix = suffixOf(filename);
        if (!ALLOWED_SUFFIXES.contains(suffix)) {
            return error(
                    HttpStatus.UNSUPPORTED_MEDIA_TYPE,
                    "IMPORT_UNSUPPORTED_FILE",
                    "只支持 .zip / .png / .jpg / .jpeg"
            );
        }
        if (file.getSize() > MAX_FILE_BYTES) {
            return error(
                    HttpStatus.PAYLOAD_TOO_LARGE,
                    "IMPORT_FILE_TOO_LARGE",
                    "上传文件超过 32 MB 限制"
            );
        }

        String resolvedProjectId = normalizeProjectId(projectId);
        try {
            ByteArrayResource resource = new ByteArrayResource(file.getBytes()) {
                @Override
                public String getFilename() {
                    return filename;
                }
            };
            MultiValueMap<String, Object> parts = new LinkedMultiValueMap<>();
            parts.add("project_id", resolvedProjectId);
            parts.add("file", resource);

            ResponseEntity<JsonNode> response = aiCoreRunRestClient.post()
                    .uri(IMPORT_PATH)
                    .contentType(MediaType.MULTIPART_FORM_DATA)
                    .body(parts)
                    .exchange((request, clientResponse) -> {
                        HttpStatusCode status = clientResponse.getStatusCode();
                        JsonNode body = clientResponse.bodyTo(JsonNode.class);
                        if (body == null) {
                            body = errorBody(
                                    "EMPTY_AI_CORE_RESPONSE",
                                    "AI Core 返回了空响应"
                            );
                        }
                        return ResponseEntity.status(status).body(body);
                    });
            log.info("project import proxy projectId={} file={} status={}",
                    resolvedProjectId, filename, response.getStatusCode());
            return response;
        } catch (IOException ex) {
            log.error("read upload failed: {}", ex.getMessage());
            return error(
                    HttpStatus.BAD_REQUEST,
                    "IMPORT_READ_FAILED",
                    "读取上传文件失败: " + ex.getMessage()
            );
        } catch (RestClientException ex) {
            log.error("project import proxy failed: {}", ex.getMessage());
            return error(
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "AI_CORE_UNAVAILABLE",
                    "导入服务暂不可用: " + ex.getMessage()
            );
        }
    }

    /**
     * 项目 ID 只允许安全字符；前端未传时生成一个。
     */
    private String normalizeProjectId(String projectId) {
        if (projectId != null && projectId.matches("[A-Za-z0-9_-]{1,64}")) {
            return projectId;
        }
        return "proj_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);
    }

    /**
     * 去掉客户端可能携带的目录部分，避免文件名进入日志 / 响应时污染。
     */
    private String sanitizeFilename(String originalFilename) {
        if (originalFilename == null || originalFilename.isBlank()) {
            return "upload.bin";
        }
        String normalized = originalFilename.replace('\\', '/');
        int index = normalized.lastIndexOf('/');
        String name = index >= 0 ? normalized.substring(index + 1) : normalized;
        return name.isBlank() ? "upload.bin" : name;
    }

    private String suffixOf(String filename) {
        int index = filename.lastIndexOf('.');
        return index < 0 ? "" : filename.substring(index).toLowerCase(Locale.ROOT);
    }

    private ResponseEntity<JsonNode> error(HttpStatus status, String code, String message) {
        return ResponseEntity.status(status).body(errorBody(code, message));
    }

    private JsonNode errorBody(String code, String message) {
        ObjectNode node = objectMapper.createObjectNode();
        node.put("code", code);
        node.put("message", message);
        return node;
    }
}
