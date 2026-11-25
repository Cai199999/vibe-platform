package com.vibeplatform.bff.common;

import org.slf4j.MDC;

/**
 * 单请求 traceId 的线程上下文。
 *
 * <p>BFF 入口生成/透传 {@code X-Trace-Id}，调用 AI Core 时继续透传；
 * Python 错误信封与日志会复用同一个 traceId，便于跨语言排障。</p>
 */
public final class TraceContext {

    public static final String HEADER = "X-Trace-Id";
    public static final String MDC_KEY = "traceId";

    private TraceContext() {
    }

    public static void set(String traceId) {
        MDC.put(MDC_KEY, traceId);
    }

    public static String current() {
        return MDC.get(MDC_KEY);
    }

    public static void clear() {
        MDC.remove(MDC_KEY);
    }
}
