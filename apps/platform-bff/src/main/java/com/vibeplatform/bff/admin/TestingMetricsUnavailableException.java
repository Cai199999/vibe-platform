package com.vibeplatform.bff.admin;

/** AI Core 测试指标/报告不可用；由 AdminExceptionHandler 映射为 502。 */
public class TestingMetricsUnavailableException extends RuntimeException {

    public TestingMetricsUnavailableException(String message, Throwable cause) {
        super(message, cause);
    }
}
