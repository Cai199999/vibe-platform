package com.vibeplatform.bff.sourceanalysis;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 存量 React 源码只读分析入口。
 *
 * <p>只返回组件树分析报告；不写源码、不生成 VisualDocument。</p>
 */
@RestController
@RequestMapping("/api/v1/source-analysis")
public class SourceAnalysisController {

    private final SourceAnalysisService sourceAnalysisService;

    public SourceAnalysisController(SourceAnalysisService sourceAnalysisService) {
        this.sourceAnalysisService = sourceAnalysisService;
    }

    @PostMapping("/analyze")
    public ResponseEntity<JsonNode> analyze(@RequestBody JsonNode body) {
        return sourceAnalysisService.analyze(body);
    }
}
