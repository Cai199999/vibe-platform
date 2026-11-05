package com.vibeplatform.bff.admin;

/**
 * 非管理员访问管理端 API。
 *
 * <p>单独定义异常而不是直接返回 403，便于统一映射错误码、写审计日志，
 * 也避免 Controller 里混入权限判断。</p>
 */
public class AdminAccessDeniedException extends RuntimeException {

    private final String role;

    public AdminAccessDeniedException(String role) {
        super("需要管理员权限");
        this.role = role == null || role.isBlank() ? "ANONYMOUS" : role;
    }

    public String role() {
        return role;
    }
}
