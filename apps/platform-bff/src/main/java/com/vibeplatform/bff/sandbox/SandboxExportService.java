package com.vibeplatform.bff.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vibeplatform.bff.run.RunSandboxRegistry;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * 项目导出代理：浏览器 -> Java BFF -> Python AI Core -> 沙盒 tar.gz。
 *
 * <p>AI Core 的 internal 接口返回二进制 gzip 流，BFF 不落盘、不解析压缩包，
 * 直接把 InputStream 复制到 Servlet 输出流，避免在内存中复制整包。响应头
 * 由 BFF 统一设置，前端拿到的就是浏览器原生下载。</p>
 */
@Service
public class SandboxExportService {

    private static final Logger log = LoggerFactory.getLogger(SandboxExportService.class);

    // runId / sandboxId 由平台生成，只允许安全字符，避免路径与响应头注入。
    private static final Pattern ID_PATTERN = Pattern.compile("[A-Za-z0-9_-]{1,80}");
    private static final String EXPORT_PATH = "/api/v1/internal/sandbox/{sandboxId}/export";
    private static final String ARCHIVE_FILENAME = "project-export.tar.gz";
    private static final MediaType GZIP = MediaType.parseMediaType("application/gzip");

    private final RestClient aiCoreRunRestClient;
    private final ObjectMapper objectMapper;
    private final RunSandboxRegistry runSandboxRegistry;

    public SandboxExportService(
            @Qualifier("aiCoreRunRestClient") RestClient aiCoreRunRestClient,
            ObjectMapper objectMapper,
            RunSandboxRegistry runSandboxRegistry
    ) {
        this.aiCoreRunRestClient = aiCoreRunRestClient;
        this.objectMapper = objectMapper;
        this.runSandboxRegistry = runSandboxRegistry;
    }

    /**
     * 按 runId 导出。sandboxId 可选：
     *  - 前端通常同时持有两者，直接传 sandboxId，BFF 重启后也能用；
     *  - 只传 runId 时使用本次进程内记录的映射。
     */
    public void exportByRun(String runId, String sandboxId, HttpServletResponse response) {
        if (runId == null || !ID_PATTERN.matcher(runId).matches()) {
            writeError(response, HttpStatus.BAD_REQUEST, "INVALID_RUN_ID", "非法的 runId");
            return;
        }
        String resolved = sandboxId;
        if (resolved == null || resolved.isBlank()) {
            resolved = runSandboxRegistry.find(runId);
        }
        if (resolved == null || resolved.isBlank()) {
            writeError(
                    response,
                    HttpStatus.NOT_FOUND,
                    "RUN_NOT_FOUND",
                    "未找到该 Run 对应的沙盒，请携带 sandboxId 参数重试"
            );
            return;
        }
        exportSandbox(resolved, response);
    }

    /**
     * 直接把沙盒打包结果流式透传给浏览器。
     */
    public void exportSandbox(String sandboxId, HttpServletResponse response) {
        if (sandboxId == null || !ID_PATTERN.matcher(sandboxId).matches()) {
            writeError(response, HttpStatus.BAD_REQUEST, "INVALID_SANDBOX_ID", "非法的 sandboxId");
            return;
        }

        try {
            aiCoreRunRestClient.get()
                    .uri(EXPORT_PATH, sandboxId)
                    .accept(GZIP)
                    .exchange((clientRequest, clientResponse) -> {
                        HttpStatusCode status = clientResponse.getStatusCode();
                        if (!status.is2xxSuccessful()) {
                            writeError(
                                    response,
                                    status.value(),
                                    "EXPORT_FAILED",
                                    extractMessage(clientResponse.bodyTo(JsonNode.class), status)
                            );
                            return null;
                        }

                        response.setStatus(HttpStatus.OK.value());
                        response.setContentType(GZIP.toString());
                        response.setHeader(
                                HttpHeaders.CONTENT_DISPOSITION,
                                ContentDisposition.attachment()
                                        .filename(ARCHIVE_FILENAME)
                                        .build()
                                        .toString()
                        );
                        response.setHeader(HttpHeaders.CACHE_CONTROL, "no-store");

                        // 边读边写：项目源码包通常只有几十 KB，但这里不假设大小，
                        // 不把整个压缩包缓冲进 BFF 堆内存。
                        try (InputStream body = clientResponse.getBody();
                             OutputStream output = response.getOutputStream()) {
                            if (body == null) {
                                throw new IOException("AI Core 返回了空响应");
                            }
                            body.transferTo(output);
                            output.flush();
                        }
                        return null;
                    });

            log.info("sandbox export proxied sandboxId={}", sandboxId);
        } catch (RestClientException ex) {
            if (!response.isCommitted()) {
                writeError(
                        response,
                        HttpStatus.SERVICE_UNAVAILABLE,
                        "AI_CORE_UNAVAILABLE",
                        "导出项目失败: " + ex.getMessage()
                );
            } else {
                // 已经向浏览器写入了部分字节，无法再改状态码，只能记录日志。
                log.warn("sandbox export stream interrupted sandboxId={}: {}",
                        sandboxId, ex.getMessage());
            }
        }
    }

    /**
     * 统一错误体：{@code {"code": "...", "message": "..."}}，与其余 BFF 接口一致。
     */
    private void writeError(
            HttpServletResponse response,
            HttpStatus status,
            String code,
            String message
    ) {
        writeError(response, status.value(), code, message);
    }

    private void writeError(
            HttpServletResponse response,
            int status,
            String code,
            String message
    ) {
        if (response.isCommitted()) {
            return;
        }
        response.setStatus(status);
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding("UTF-8");
        ObjectNode body = objectMapper.createObjectNode();
        body.put("code", code);
        body.put("message", message);
        try {
            response.getOutputStream().write(objectMapper.writeValueAsBytes(body));
            response.getOutputStream().flush();
        } catch (IOException ex) {
            log.warn("failed to write export error response: {}", ex.getMessage());
        }
    }

    /**
     * 从 AI Core 的错误体里提取可读信息。FastAPI 使用
     * {@code {"detail": {"code": "...", "message": "..."}}} 结构。
     */
    private String extractMessage(JsonNode body, HttpStatusCode status) {
        if (body != null) {
            JsonNode detail = body.path("detail");
            if (detail.isTextual()) {
                return detail.asText();
            }
            if (detail.isObject() && detail.hasNonNull("message")) {
                String code = detail.path("code").asText("");
                return code.isBlank()
                        ? detail.get("message").asText()
                        : code + " · " + detail.get("message").asText();
            }
            if (body.hasNonNull("message")) {
                return body.get("message").asText();
            }
        }
        return "AI Core 导出失败（HTTP " + status.value() + "）";
    }
}
