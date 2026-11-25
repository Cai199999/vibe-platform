package com.vibeplatform.bff.resilience;

import java.io.IOException;

/**
 * 舱壁 / 熔断器快速失败信号。
 *
 * <p>继承 {@link IOException} 是为了让 RestClient 将其视为传输失败；
 * BFF 代理层可以据此返回稳定的 {@code AI_CORE_BUSY} /
 * {@code AI_CORE_CIRCUIT_OPEN} 错误码，而不是让请求继续排队。</p>
 */
public class AiCoreGuardException extends IOException {

    private final String code;

    public AiCoreGuardException(String code, String message) {
        super(message);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
