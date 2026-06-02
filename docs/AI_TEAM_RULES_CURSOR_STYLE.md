# AI 团队成员规范：Cursor Rules 与 Claude Code 双栈

本文说明：**如何把「对各 AI 协作者的约束」做成可版本化、可发现的多入口规则**，而不是只散落在口头或单次 prompt 里。

## 0. 全体成员总约束（权威正文）

- **`docs/AI_TEAM_MEMBER_CHARTER.md`**：**所有 AI 成员**（CC、Cursor、其他）共享的总体宪章。
- **Claude Code**：通过仓库根 **`CLAUDE.md`** 的 `@` 引用在**每次会话启动**载入（与 [官方 memory 说明](https://docs.anthropic.com/en/docs/claude-code/memory) 一致）。
- **Cursor**：通过 **`.cursor/rules/*.mdc`** 注入；可与宪章**互补**（宪章偏全文契约，`.mdc` 偏硬条与 globs）。

## 1. Cursor 做法（本项目已对齐）

| 机制 | 作用 |
|------|------|
| **`.cursor/rules/*.mdc`** | 带 YAML frontmatter 的 Markdown；`alwaysApply: true` 时本会话默认注入；`globs` 时仅编辑匹配文件时注入。 |
| **`description`** | 在规则列表里可读，便于人类挑选与审计。 |
| **`AGENTS.md`** | 仓库级「总说明」：边界、角色、会话习惯；与 `.mdc` **互补**（AGENTS 偏叙事，mdc 偏硬约束条）。 |

**本仓库落点**：`openclaw-team/.cursor/rules/`（随 git 版本化，整团队一致）。

## 2. 推荐工作区打开方式（让规则自动生效）

- **日常改 openclaw-team 代码**：在 Cursor 里将**工作区根目录**设为 **`openclaw-team/`**，则该目录下 `.cursor/rules` 会按 Cursor 机制自动应用。
- 若工作区根是上一层的 **`ai-orchestration/`**：父目录已有全局规则（布局、git-safe）；子仓规则**可能**不会全部自动合并——请在新会话 **@** 引用：`openclaw-team/AGENTS.md` 与 `openclaw-team/.cursor/rules/` 下相关 `.mdc`，或切换到以 `openclaw-team` 为根的窗口。

## 3. 给「非 Cursor」AI（网页、其他 IDE）的同一套约束

把对应 `.mdc` **正文**（去掉 frontmatter 或保留 YAML 均可）贴进系统提示 / 任务书附录，或只贴：

1. `AGENTS.md` 中「编排与治理」「Cursor 多会话编排」两节；
2. `01-virtual-team-execution.mdc` 全文（虚拟团队硬约束）。

字段与验收仍以 **`docs/46-*`、`50-*`、`74-*`** 为准。

## 4. 新增一条团队规则时

1. 在 `openclaw-team/.cursor/rules/` 新建 `NN-主题.mdc`（编号便于排序）。
2. 写清：`description`、`alwaysApply` 或 **`globs`**（只影响某树时用 globs，避免全仓噪音）。
3. 若与产品文档强相关，在 **`docs/00-README.md`** 或对应专题文档记一笔「规则已收束到 `.cursor/rules/xx.mdc`」。

## 5. 与 HANDOFF 的关系

- **`docs/session-handoff/`**：当次任务输入输出、人类审核收口。
- **`.cursor/rules/`** 与 **`CLAUDE.md` / `.claude/rules/`**：跨会话、跨任务的**不变约束**。
- 分会话开场白建议：**HANDOFF（当次）+ `AI_TEAM_MEMBER_CHARTER` + `AGENTS` + 与本任务相关的 `.mdc` / `.claude/rules`**。

## 6. Claude Code 专用落点小结

| 位置 | 用途 |
|------|------|
| **`CLAUDE.md`**（仓库根） | CC 每次会话加载；建议 `@AGENTS.md` + `@docs/AI_TEAM_MEMBER_CHARTER.md`。 |
| **`.claude/rules/*.md`** | 模块化规则；可用 YAML **`paths:`** 仅在对匹配文件工作时注入（省 token）。 |
| **`CLAUDE.local.md`** | 个人偏好，**勿提交**（已列入 `.gitignore`）。 |
