# 2026-11-04 · 工作区 UI 极简清洗

## 目标

移除所有“开发者调试视角”的文案，让工作区只保留导航、功能按钮、画布与
悬浮输入框，达到 Vercel / Figma 式克制观感。

## 移除的文案

- 页面副标题：“沿画布圈选组件，浮层内下达修改意图，Agent 在隔离沙盒中执行。”
- 页脚长文：`platform-web → /api/v1/runs → Java BFF → python-ai → Docker 沙盒`
  及 20–60 秒、关闭 Tab 销毁沙盒等实现细节。
- 画布顶栏：`vibe://canvas/preview`、`src/App.jsx`。
- 预览顶栏：沙盒端口 URL、失效时的原始地址。
- 模型选择：`Mock（离线演示）`、`自动（有 Key 用 DeepSeek）` 等内部
  provider 说明。现在只显示 `DeepSeek V3` / `本地预览`，默认 `DeepSeek V3`。
- 浮层标题：`标注 1 / 1`、组件类型标签；现在只剩颜色圆点与关闭按钮。
- 浮层占位符：`例如：改成蓝色 / 放大一倍` → `描述修改意图...`。
- 浮层底部：`发送后立即执行，后续可继续对话调整` 等教学文案；仅在执行 /
  回滚时显示 `正在修改…` / `正在恢复…`。
- 预览空态与加载态：`Planner / Coder / Applier / Verifier`、坐标归一化、
  Diff、HMR、错误日志回灌等内部链路说明；改为“理解修改意图 / 生成代码 /
  验证构建”三步。
- 顶栏 `M1`、`React / Vite Sandbox`、`Agent Running / Ready` 文本，
  只保留品牌、状态圆点与账户。
- Tab 栏 `拖拽 Tab 可排序`、项目下拉 `Phase 3 导入项目`、登录页
  `Mock 鉴权 · 本地会话`、账户弹窗 `Mock Session`。

## 保留 / 弱化

- 功能按钮文案保留：清除标注、提交、对比模式、展示模式、画布、预览、
  导出代码。
- 画布提示保留但弱化：`圈选组件 · 空格/中键平移 · 滚轮缩放`，
  字号 10px、透明度 0.45、无边框无背景。
- 标注数量改为 `1 个标注` 小胶囊，仅在已圈选时出现。
- 模型选择改为齿轮 + 极淡文字，hover / focus 时恢复不透明度。

## 测试

`scripts/ui_copy_clean_e2e_test.py`：

```text
同步链路 / 技术术语 / 文件路径 / 页脚均已移除
模型: DeepSeek V3 / 选项: ['DeepSeek V3', '本地预览']
功能按钮: ['清除标注', '提交', '对比模式', '展示模式', '画布', '预览', '导出代码']
标题仅保留颜色圆点；占位符：描述修改意图...；无底部说明
画布提示: fontSize=10px opacity=0.45
画布顶栏仅保留窗口圆点
UI COPY CLEAN E2E: PASS
```

回归：

- `ui_e2e_test.py`：PASS（标题断言改为活动 Tab 名称；状态断言改为“已完成”；
  离线文案改为“预览已失效”）
- `multi_turn_chat_e2e_test.py`：PASS
- `multi_annotation_e2e_test.py`：PASS
- `rollback_e2e_test.py` / `annotation_delete_rollback_e2e_test.py`：PASS
- `popover_equal_scale_e2e_test.py`：PASS（浮层高度因移除说明从 135→108，
  归一化比例三档仍完全一致）
- `docker exec vibe-platform-web npm run build`：PASS

截图：`docs/screenshots/m1-ui-polished-workspace.png`。

## 顺带修复：Vite 临时文件导致 Git checkpoint 偶发失败

回归中复现：

```text
SANDBOX_GIT_FAILED
初始化 git add 失败: fatal: unable to stat
'vite.config.js.timestamp-...-....mjs': No such file or directory
```

Vite 会瞬时创建并删除 `vite.config.js.timestamp-*.mjs`，`git add -A`
扫描到删除瞬间就会失败。修复：

1. `.gitignore` 与 `.git/info/exclude` 增加
   `vite.config.*.timestamp-*`、`*.timestamp-*.mjs`；
2. `.git/info/exclude` 兼容升级前创建的旧沙盒，且不会进入源码导出；
3. `_git_add_all()` 对 `unable to stat` / `No such file` 做 3 次短重试，
   仍失败才抛 `SandboxGitError`。
