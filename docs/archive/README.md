# docs 归档区

本目录由 `scripts/archive_docs.py` 自动维护，存放已归档的历史文档。

- 每次归档位于一个时间戳目录，例如 `2026-11-09_083000/`。
- 每次归档的 `manifest.json` 记录了原路径、归档路径、归档原因与大小。
- 原路径会保留一个跳转占位文件，历史链接不会断。

恢复某次归档：

```bash
python3 scripts/archive_docs.py --restore 2026-11-09_083000
```
