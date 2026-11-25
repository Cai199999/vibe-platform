package com.vibeplatform.bff.identity;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtDecoders;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.util.StringUtils;

/**
 * 平台鉴权。
 *
 * <p>{@code VIBE_OIDC_ENABLED=false} 时保持 Mock 行为（全部放行，角色由
 * {@code X-User-Role} 等头部表达）；启用后所有 API 需要 Bearer JWT，
 * 健康检查保持公开。租户上下文仍由 {@link TenantFilter} 统一解析。</p>
 */
@Configuration
@EnableWebSecurity
@EnableConfigurationProperties(OidcProperties.class)
public class SecurityConfig {

    @Bean
    public SecurityFilterChain securityFilterChain(
            HttpSecurity http,
            OidcProperties properties
    ) throws Exception {
        http.csrf(AbstractHttpConfigurer::disable)
                .httpBasic(AbstractHttpConfigurer::disable)
                .formLogin(AbstractHttpConfigurer::disable)
                .sessionManagement(session -> session
                        .sessionCreationPolicy(SessionCreationPolicy.STATELESS));
        if (properties.isEnabled()) {
            http.authorizeHttpRequests(auth -> auth
                            .requestMatchers("/api/v1/health", "/api/v1/health/**")
                            .permitAll()
                            .anyRequest()
                            .authenticated())
                    .oauth2ResourceServer(oauth2 -> oauth2.jwt(Customizer.withDefaults()));
        } else {
            http.authorizeHttpRequests(auth -> auth.anyRequest().permitAll());
        }
        return http.build();
    }

    @Configuration
    @ConditionalOnProperty(prefix = "vibe.oidc", name = "enabled", havingValue = "true")
    static class JwtDecoderConfig {

        @Bean
        public JwtDecoder jwtDecoder(OidcProperties properties) {
            if (StringUtils.hasText(properties.getJwkSetUri())) {
                return NimbusJwtDecoder.withJwkSetUri(properties.getJwkSetUri()).build();
            }
            if (StringUtils.hasText(properties.getIssuerUri())) {
                return JwtDecoders.fromIssuerLocation(properties.getIssuerUri());
            }
            throw new IllegalStateException(
                    "OIDC 已启用，但未配置 issuer-uri 或 jwk-set-uri"
            );
        }
    }
}
