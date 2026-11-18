package com.vibeplatform.bff.projectsource;

/**
 * 项目源码持久化的稳定错误类型。
 *
 * <p>错误码由 BFF 统一返回，前端不解析底层 MySQL / MinIO 异常文本。</p>
 */
public class ProjectSourceException extends RuntimeException {

    private final String code;
    private final int status;
    private final boolean retryable;

    private ProjectSourceException(
            String code,
            String message,
            int status,
            boolean retryable,
            Throwable cause
    ) {
        super(message, cause);
        this.code = code;
        this.status = status;
        this.retryable = retryable;
    }

    public static ProjectSourceException invalid(String message) {
        return new ProjectSourceException(
                "PROJECT_SOURCE_INVALID",
                message,
                400,
                false,
                null
        );
    }

    public static ProjectSourceException notFound() {
        return new ProjectSourceException(
                "PROJECT_SOURCE_NOT_FOUND",
                "没有找到该项目的持久化源码",
                404,
                false,
                null
        );
    }

    public static ProjectSourceException unavailable(String message, Throwable cause) {
        return new ProjectSourceException(
                "PROJECT_SOURCE_STORAGE_UNAVAILABLE",
                message,
                503,
                true,
                cause
        );
    }

    public String code() {
        return code;
    }

    public int status() {
        return status;
    }

    public boolean retryable() {
        return retryable;
    }
}
