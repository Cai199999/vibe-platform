#!/usr/bin/env python3
"""docs/ 文档自动归档脚本。

归档规则（二选一）：

1. 长篇设计文档：位于 ``docs/design/`` 下，非空白字符数 >= ``--min-chars``，
   且最后修改时间距今 >= ``--days`` 天。
2. 增量文档：位于 ``docs/memory/`` 下、以 ``YYYY-MM-DD`` 日期开头的阶段总结，
   或文件名包含 ``delta / 增量 / changelog / changes / update / snapshot`` 的文档，
   非空白字符数 >= ``--min-chars``。

安全设计：

- 默认 dry-run，只有显式传入 ``--apply`` 才会真正移动文件。
- 被代码文件（.py / .js / .java / .yml / .sh 等，非 Markdown）引用的文档默认跳过，
  ``--force`` 可强制归档。
- 归档保留原相对目录结构：``docs/design/x.md`` →
  ``docs/archive/<timestamp>/design/x.md``。
- 原路径写入一个极小的 Markdown 跳转占位文件，历史相对链接仍然可用。
- 被归档文档内部的相对链接，如果目标文件未归档，会自动重写为指向原位置的新相对路径。
- 每次归档生成 ``manifest.json``，可执行 ``--restore <timestamp>`` 完整还原。

用法::

    python3 scripts/archive_docs.py                  # 预览
    python3 scripts/archive_docs.py --apply          # 执行归档
    python3 scripts/archive_docs.py --dry-run --verbose
    python3 scripts/archive_docs.py --restore 2026-11-09_083000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 项目根目录：scripts/ 的上一级
REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
ARCHIVE_DIR = DOCS_DIR / "archive"

# 永不归档的文档
PROTECTED_NAMES = {"00_project_brief.md", "CURRENT_CONTEXT.md", "README.md"}

# 增量文档识别
INCREMENTAL_NAME_RE = re.compile(r"(?i)(delta|增量|changelog|changes|update|snapshot)")
DATED_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

# 需要检查“是否引用文档路径”的代码文件扩展名
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
    ".java",
    ".json",
    ".yml",
    ".yaml",
    ".sh",
    ".bash",
    ".xml",
    ".toml",
    ".properties",
    ".sql",
    ".gradle",
}

# 扫描代码引用时跳过的目录
SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
}

# Markdown 链接：支持图片与可选 title
MD_LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")

# 占位文件标记，用于 restore 时判断能否安全覆盖
STUB_MARKER = "<!-- archived-doc-stub -->"

ARCHIVE_README = """# docs 归档区

本目录由 `scripts/archive_docs.py` 自动维护，存放已归档的历史文档。

- 每次归档位于一个时间戳目录，例如 `2026-11-09_083000/`。
- 每次归档的 `manifest.json` 记录了原路径、归档路径、归档原因与大小。
- 原路径会保留一个跳转占位文件，历史链接不会断。

恢复某次归档：

```bash
python3 scripts/archive_docs.py --restore 2026-11-09_083000
```
"""


@dataclass
class Candidate:
    """一个待归档文档。"""

    path: Path
    archived_path: Path
    reason: str
    chars: int
    age_days: float
    mtime: float
    referenced_by: str | None = None

    @property
    def rel(self) -> str:
        return self.path.relative_to(REPO_ROOT).as_posix()

    @property
    def archived_rel(self) -> str:
        return self.archived_path.relative_to(REPO_ROOT).as_posix()

    @property
    def should_archive(self) -> bool:
        return self.referenced_by is None


def non_whitespace_chars(path: Path) -> int:
    """统计非空白字符数（中文与英文均按字符计）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for ch in text if not ch.isspace())


def iter_code_files() -> list[Path]:
    """枚举需要做引用检查的代码文件，跳过重型目录。"""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        current = Path(dirpath)
        if ARCHIVE_DIR == current or ARCHIVE_DIR in current.parents:
            continue
        for filename in filenames:
            path = current / filename
            if path.suffix.lower() not in CODE_EXTENSIONS:
                continue
            try:
                if path.stat().st_size > 1_048_576:
                    continue
            except OSError:
                continue
            files.append(path)
    return files


def load_code_corpus(code_files: list[Path]) -> list[tuple[str, str]]:
    """一次性读取代码文件，避免对每个候选重复 IO。"""
    corpus: list[tuple[str, str]] = []
    for path in code_files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        corpus.append((path.relative_to(REPO_ROOT).as_posix(), text))
    return corpus


def find_code_reference(doc: Path, corpus: list[tuple[str, str]]) -> str | None:
    """返回第一个引用该文档的代码文件（相对路径），没有则返回 None。"""
    rel = doc.relative_to(REPO_ROOT).as_posix()
    name = doc.name
    for code_rel, text in corpus:
        # 优先匹配 docs/ 相对路径；退而匹配文件名
        if rel in text or name in text:
            return code_rel
    return None


def is_incremental(path: Path) -> bool:
    """判断是否为增量 / 阶段总结文档。"""
    if INCREMENTAL_NAME_RE.search(path.name):
        return True
    return path.parent.name == "memory" and bool(DATED_NAME_RE.match(path.name))


def collect_candidates(
    root: Path,
    days: int,
    min_chars: int,
    corpus: list[tuple[str, str]],
    force: bool,
) -> list[Candidate]:
    """按规则收集候选文档。"""
    candidates: list[Candidate] = []
    now = datetime.now().timestamp()

    for path in sorted(DOCS_DIR.rglob("*.md")):
        if ARCHIVE_DIR == path.parent or ARCHIVE_DIR in path.parents:
            continue
        if path.name in PROTECTED_NAMES:
            continue
        # 占位文件不重复归档
        try:
            if STUB_MARKER in path.read_text(encoding="utf-8", errors="ignore"):
                continue
        except OSError:
            continue

        chars = non_whitespace_chars(path)
        if chars < min_chars:
            continue

        stat = path.stat()
        age_days = max(0.0, (now - stat.st_mtime) / 86400)
        is_long_design = "design" in path.relative_to(DOCS_DIR).parts
        reason: str | None = None

        if is_long_design and age_days >= days:
            reason = f"长篇设计文档：{chars} 字，{age_days:.1f} 天未修改"
        elif is_incremental(path):
            reason = f"增量文档：{chars} 字"

        if reason is None:
            continue

        referenced_by = None if force else find_code_reference(path, corpus)
        candidates.append(
            Candidate(
                path=path,
                archived_path=root / path.relative_to(DOCS_DIR),
                reason=reason,
                chars=chars,
                age_days=age_days,
                mtime=stat.st_mtime,
                referenced_by=referenced_by,
            )
        )

    return candidates


def rewrite_links(
    text: str, old_path: Path, new_path: Path, preserved: set[Path]
) -> str:
    """重写 Markdown 相对链接。

    :param old_path: 改写前文件位置，用于解析链接目标。
    :param new_path: 改写后文件位置，用于生成新相对路径。
    :param preserved: 目录结构保持不变的路径集合（链接无需改写）。
    """

    def replace(match: re.Match[str]) -> str:
        prefix, target, suffix = match.group(1), match.group(2), match.group(3)
        # 跳过绝对地址、锚点、协议链接
        if target.startswith(("#", "/")) or re.match(
            r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target
        ):
            return match.group(0)

        path_part, sep, fragment = target.partition("#")
        try:
            resolved = (old_path.parent / path_part).resolve()
        except OSError:
            return match.group(0)

        # 目标也被归档 / 还原时目录结构保持一致，无需改写
        if resolved in preserved:
            return match.group(0)
        if not resolved.exists():
            return match.group(0)

        new_rel = os.path.relpath(resolved, new_path.parent)
        if sep:
            new_rel = f"{new_rel}#{fragment}"
        return f"{prefix}{new_rel}{suffix}"

    return MD_LINK_RE.sub(replace, text)


def write_stub(stub_path: Path, archived_path: Path) -> None:
    """在原路径写入跳转占位文件。"""
    rel_link = os.path.relpath(archived_path, stub_path.parent)
    content = (
        f"{STUB_MARKER}\n\n"
        f"> 📦 本文档已于 {datetime.now().strftime('%Y-%m-%d %H:%M')} 归档至 "
        f"[{archived_path.name}]({rel_link})。\n"
        f"> 此占位文件用于保持历史链接与相对路径可用；"
        f"新会话请优先阅读 `CURRENT_CONTEXT.md`。\n"
    )
    stub_path.write_text(content, encoding="utf-8")


def archive(
    candidates: list[Candidate],
    root: Path,
    stamp: str,
    args: argparse.Namespace,
    apply: bool,
) -> int:
    """执行归档；返回实际（或预览中）归档数量。"""
    movable = [c for c in candidates if c.should_archive]
    skipped = [c for c in candidates if not c.should_archive]

    for c in skipped:
        print(
            f"SKIP   {c.rel}\n       原因：被代码引用 {c.referenced_by}（可用 --force 强制）"
        )

    if not movable:
        print("\n没有需要归档的文档。")
        return 0

    if apply:
        root.mkdir(parents=True, exist_ok=True)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        (ARCHIVE_DIR / "README.md").write_text(ARCHIVE_README, encoding="utf-8")

    # 归档后目录结构保持一致，内部链接无需改写
    preserved = {c.path.resolve() for c in movable}
    manifest_entries: list[dict[str, object]] = []

    for c in movable:
        print(f"MOVE   {c.rel}\n       → {c.archived_rel}\n       原因：{c.reason}")
        if not apply:
            continue

        c.archived_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(c.path), str(c.archived_path))

        # 归档后修正内部相对链接
        try:
            text = c.archived_path.read_text(encoding="utf-8")
            rewritten = rewrite_links(text, c.path, c.archived_path, preserved)
            if rewritten != text:
                c.archived_path.write_text(rewritten, encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass

        # 原路径保留跳转占位
        write_stub(c.path, c.archived_path)

        manifest_entries.append(
            {
                "original": c.rel,
                "archived": c.archived_rel,
                "reason": c.reason,
                "chars": c.chars,
                "mtime": datetime.fromtimestamp(c.mtime).isoformat(timespec="seconds"),
            }
        )

    if apply:
        manifest = {
            "timestamp": stamp,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "rule": {
                "days": args.days,
                "minChars": args.min_chars,
                "documents": len(manifest_entries),
            },
            "entries": manifest_entries,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"\n已归档 {len(manifest_entries)} 个文档到 {root.relative_to(REPO_ROOT)}/"
        )
        print(f"清单：{(root / 'manifest.json').relative_to(REPO_ROOT)}")
    else:
        print(f"\n预览：将归档 {len(movable)} 个文档（加 --apply 执行）。")

    return len(movable)


def restore(stamp: str, force: bool) -> int:
    """按 manifest 还原一次归档。"""
    root = ARCHIVE_DIR / stamp
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        print(f"找不到归档清单：{manifest_path}", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("entries", [])
    # 还原时所有归档文件都会回到各自原位置，链接结构整体保持一致
    preserved = {(REPO_ROOT / entry["archived"]).resolve() for entry in entries}

    restored = 0
    for entry in entries:
        src = REPO_ROOT / entry["archived"]
        dst = REPO_ROOT / entry["original"]
        if not src.exists():
            print(f"MISS   {entry['archived']}")
            continue

        if dst.exists():
            is_stub = False
            try:
                is_stub = STUB_MARKER in dst.read_text(
                    encoding="utf-8", errors="ignore"
                )
            except OSError:
                pass
            if not is_stub and not force:
                print(f"SKIP   {entry['original']}（目标已存在且不是占位文件）")
                continue
            dst.unlink()

        dst.parent.mkdir(parents=True, exist_ok=True)
        # 先解析并修正链接，再落盘，保证还原后相对路径仍然正确
        try:
            text = src.read_text(encoding="utf-8")
            text = rewrite_links(text, src, dst, preserved)
            dst.write_text(text, encoding="utf-8")
            src.unlink()
        except (OSError, UnicodeDecodeError):
            shutil.move(str(src), str(dst))
        print(f"RESTORE {entry['original']}")
        restored += 1

    print(f"\n已还原 {restored} 个文档。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="docs 文档自动归档")
    parser.add_argument(
        "--apply", action="store_true", help="真正执行移动（默认仅预览）"
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览，等同于默认行为")
    parser.add_argument(
        "--days", type=int, default=7, help="设计文档未修改天数阈值，默认 7"
    )
    parser.add_argument(
        "--min-chars", type=int, default=2000, help="非空白字符阈值，默认 2000"
    )
    parser.add_argument(
        "--force", action="store_true", help="跳过代码引用检查，强制归档"
    )
    parser.add_argument("--restore", metavar="TIMESTAMP", help="还原指定时间戳的归档")
    parser.add_argument("--verbose", action="store_true", help="输出更详细的信息")
    args = parser.parse_args()

    if args.restore:
        return restore(args.restore, args.force)

    if not DOCS_DIR.exists():
        print(f"docs 目录不存在：{DOCS_DIR}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    root = ARCHIVE_DIR / stamp
    suffix = 1
    while root.exists():
        suffix += 1
        root = ARCHIVE_DIR / f"{stamp}-{suffix}"

    code_files = iter_code_files()
    corpus = load_code_corpus(code_files)
    candidates = collect_candidates(root, args.days, args.min_chars, corpus, args.force)

    apply = args.apply and not args.dry_run
    archive(candidates, root, stamp, args, apply)

    if args.verbose:
        print(f"\n扫描代码文件：{len(code_files)} 个")
        print(f"候选文档：{len(candidates)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
