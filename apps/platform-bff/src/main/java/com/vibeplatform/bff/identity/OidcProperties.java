package com.vibeplatform.bff.identity;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * OIDC Resource Server 配置。
 *
 * <p>默认关闭，沿用 Mock 鉴权；生产通过环境变量启用，启用后必须提供
 * issuer-uri 或 jwk-set-uri，缺失时启动失败（fail-closed）。</p>
 */
@ConfigurationProperties(prefix = "vibe.oidc")
public class OidcProperties {

    private boolean enabled = false;
    private String issuerUri = "";
    private String jwkSetUri = "";

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public String getIssuerUri() {
        return issuerUri;
    }

    public void setIssuerUri(String issuerUri) {
        this.issuerUri = issuerUri;
    }

    public String getJwkSetUri() {
        return jwkSetUri;
    }

    public void setJwkSetUri(String jwkSetUri) {
        this.jwkSetUri = jwkSetUri;
    }
}
