package com.vibeplatform.bff.admin;

/** AI Core 门禁错误统计不可用；由 AdminExceptionHandler 映射为 502。 */
public class GateErrorStatsUnavailableException extends RuntimeException {

    public GateErrorStatsUnavailableException(String message, Throwable cause) {
        super(message, cause);
    }
}
