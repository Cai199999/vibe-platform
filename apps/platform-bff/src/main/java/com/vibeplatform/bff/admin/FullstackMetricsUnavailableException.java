package com.vibeplatform.bff.admin;

/**
 * Python AI Core 指标不可用。
 *
 * <p>监控探针失败不应伪装成空数据（那会把“指标服务挂了”误报成
 * “拦截率为 0”），因此显式抛出并由 {@link AdminExceptionHandler}
 * 映射为 502。</p>
 */
public class FullstackMetricsUnavailableException extends RuntimeException {

    public FullstackMetricsUnavailableException(String message, Throwable cause) {
        super(message, cause);
    }
}
