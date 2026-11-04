package com.vibeplatform.bff;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

/**
 * Java BFF 启动入口。
 *
 * <p>M1 阶段只承担认证、项目、Run facade、审批持久化与长连接；
 * 不在此进程中执行任何生成代码，也不内嵌 LangGraph。</p>
 */
@SpringBootApplication
@ConfigurationPropertiesScan
public class VibePlatformBffApplication {

    public static void main(String[] args) {
        SpringApplication.run(VibePlatformBffApplication.class, args);
    }
}
