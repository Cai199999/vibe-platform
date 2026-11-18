package com.vibeplatform.bff.projectsource;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.vibeplatform.bff.config.MinioProperties;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceFileRecord;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceRecord;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceResponse;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.SaveProjectSourceRequest;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;

/**
 * 持久化服务编排测试：revision 递增、对象键生成与回读。
 */
class ProjectSourceServiceTest {

    @Test
    void saveIncrementsRevisionAndWritesEveryFile() {
        StubRepository repository = new StubRepository();
        repository.existing = new ProjectSourceRecord(
                "tenant_local",
                "proj_1",
                "旧项目",
                "src/App.jsx",
                "App.jsx",
                1,
                10,
                "old",
                2,
                "project-sources/tenant_local/proj_1/r2/",
                "2026-11-17T00:00:00Z"
        );
        StubObjectStore store = new StubObjectStore();
        ProjectSourceService service = new ProjectSourceService(repository, store);
        Map<String, String> files = new LinkedHashMap<>();
        files.put("src/App.jsx", "export default function App() {}");
        files.put("src/theme.css", ":root { color: red; }");

        ProjectSourceResponse response = service.save(
                "proj_1",
                new SaveProjectSourceRequest(
                        "tenant_local",
                        "新项目",
                        "src/App.jsx",
                        "App.jsx",
                        files
                )
        );

        assertThat(response.revision()).isEqualTo(3);
        assertThat(response.fileCount()).isEqualTo(2);
        assertThat(response.files()).isNull();
        assertThat(repository.saved.revision()).isEqualTo(3);
        assertThat(repository.saved.objectPrefix())
                .isEqualTo("project-sources/tenant_local/proj_1/r3/");
        assertThat(store.putPrefix)
                .isEqualTo("project-sources/tenant_local/proj_1/r3/");
        assertThat(store.putFiles).containsOnlyKeys("src/App.jsx", "src/theme.css");
        assertThat(repository.savedFiles)
                .extracting(ProjectSourceFileRecord::path)
                .containsExactlyInAnyOrder("src/App.jsx", "src/theme.css");
    }

    @Test
    void getReadsEveryObjectBack() {
        StubRepository repository = new StubRepository();
        repository.existing = new ProjectSourceRecord(
                "tenant_local",
                "proj_1",
                "存量项目",
                "src/App.jsx",
                "App.jsx",
                1,
                10,
                "hash",
                1,
                "project-sources/tenant_local/proj_1/r1/",
                "2026-11-18T00:00:00Z"
        );
        repository.files = List.of(
                new ProjectSourceFileRecord(
                        "src/App.jsx",
                        10,
                        "hash",
                        "project-sources/tenant_local/proj_1/r1/src/App.jsx"
                )
        );
        StubObjectStore store = new StubObjectStore();
        store.objects.put(
                "project-sources/tenant_local/proj_1/r1/src/App.jsx",
                "export default function App() {}"
        );
        ProjectSourceService service = new ProjectSourceService(repository, store);

        ProjectSourceResponse response = service.get("proj_1", "tenant_local");

        assertThat(response.files())
                .containsEntry("src/App.jsx", "export default function App() {}");
        assertThat(response.revision()).isEqualTo(1);
    }

    @Test
    void getMissingProjectReturnsNotFound() {
        ProjectSourceService service = new ProjectSourceService(
                new StubRepository(),
                new StubObjectStore()
        );

        assertThatThrownBy(() -> service.get("proj_missing", "tenant_local"))
                .isInstanceOf(ProjectSourceException.class)
                .satisfies(error -> assertThat(
                        ((ProjectSourceException) error).code()
                ).isEqualTo("PROJECT_SOURCE_NOT_FOUND"));
    }

    static class StubRepository extends ProjectSourceRepository {

        ProjectSourceRecord existing;
        ProjectSourceRecord saved;
        List<ProjectSourceFileRecord> savedFiles = new ArrayList<>();
        List<ProjectSourceFileRecord> files = List.of();

        StubRepository() {
            super(null);
        }

        @Override
        public Optional<ProjectSourceRecord> find(String tenantId, String projectId) {
            return Optional.ofNullable(existing);
        }

        @Override
        public List<ProjectSourceFileRecord> findFiles(String tenantId, String projectId) {
            return files;
        }

        @Override
        public void save(
                ProjectSourceRecord record,
                List<ProjectSourceFileRecord> fileRecords
        ) {
            saved = record;
            savedFiles = new ArrayList<>(fileRecords);
        }
    }

    static class StubObjectStore extends ProjectSourceObjectStore {

        String putPrefix;
        Map<String, String> putFiles = Map.of();
        final Map<String, String> objects = new LinkedHashMap<>();

        StubObjectStore() {
            super(null, new MinioProperties(null, null, null, "vibe-platform", false));
        }

        @Override
        public Map<String, String> putFiles(
                String prefix,
                Map<String, String> files
        ) {
            putPrefix = prefix;
            putFiles = files;
            LinkedHashMap<String, String> keys = new LinkedHashMap<>();
            files.keySet().forEach(path -> keys.put(path, prefix + path));
            return keys;
        }

        @Override
        public String readText(String objectKey) {
            return objects.get(objectKey);
        }
    }
}
