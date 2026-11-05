package com.vibeplatform.bff.admin;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * 管理端 RBAC 标记。
 *
 * <p>标注在 Controller 类或方法上，由 {@link AdminAuthorizationAspect}
 * 在方法执行前校验当前请求是否具备管理员角色。M3.1 使用 Mock Header
 * {@code X-User-Role}，接入真实 JWT 后只需替换切面中的角色取值来源，
 * 业务代码不需要改动。</p>
 */
@Target({ElementType.TYPE, ElementType.METHOD})
@Retention(RetentionPolicy.RUNTIME)
@Documented
public @interface IsAdmin {
}
