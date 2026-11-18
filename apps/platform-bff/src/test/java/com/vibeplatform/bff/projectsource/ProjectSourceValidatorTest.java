package com.vibeplatform.bff.projectsource;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.LinkedHashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * 源码路径安全与哈希的确定性测试。
 */
class ProjectSourceValidatorTest {

    @Test
    void normalizesSeparatorsAndLeadingDotSlash() {
        assertThat(ProjectSourceValidator.normalizePath("./src\\pages//Home.jsx"))
                .isEqualTo("src/pages/Home.jsx");
    }

    @Test
    void rejectsTraversalAndAbsolutePaths() {
        assertThatThrownBy(() ->
                ProjectSourceValidator.normalizePath("../etc/passwd"))
                .isInstanceOf(ProjectSourceException.class)
                .hasMessageContaining("..");
        assertThatThrownBy(() ->
                ProjectSourceValidator.normalizePath("/etc/passwd"))
                .isInstanceOf(ProjectSourceException.class)
                .hasMessageContaining("绝对路径");
        assertThatThrownBy(() ->
                ProjectSourceValidator.normalizePath("C:\\Windows\\win.ini"))
                .isInstanceOf(ProjectSourceException.class);
    }

    @Test
    void rejectsDuplicatePathsAfterNormalization() {
        LinkedHashMap<String, String> files = new LinkedHashMap<>();
        files.put("src/App.jsx", "export default 1");
        files.put("./src/App.jsx", "export default 2");

        assertThatThrownBy(() -> ProjectSourceValidator.normalizeFiles(files))
                .isInstanceOf(ProjectSourceException.class)
                .hasMessageContaining("重复");
    }

    @Test
    void entryFileFallsBackToSrcAppAndMustExist() {
        LinkedHashMap<String, String> files = new LinkedHashMap<>();
        files.put("src/App.jsx", "export default function App() {}");

        assertThat(ProjectSourceValidator.resolveEntryFile(null, files))
                .isEqualTo("src/App.jsx");
        assertThatThrownBy(() ->
                ProjectSourceValidator.resolveEntryFile("src/Missing.jsx", files))
                .isInstanceOf(ProjectSourceException.class)
                .hasMessageContaining("entryFile");
    }

    @Test
    void contentHashIsStableAcrossMapOrder() {
        Map<String, String> first = new LinkedHashMap<>();
        first.put("src/b.jsx", "b");
        first.put("src/a.jsx", "a");
        Map<String, String> second = new LinkedHashMap<>();
        second.put("src/a.jsx", "a");
        second.put("src/b.jsx", "b");

        assertThat(ProjectSourceValidator.contentHash(first))
                .isEqualTo(ProjectSourceValidator.contentHash(second));
        assertThat(ProjectSourceValidator.contentHash(first)).hasSize(64);
    }
}
