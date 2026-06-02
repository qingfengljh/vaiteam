# Programming Skill Pack

这是当前默认启用的编程领域模板包。

## 目录结构

- `pack.yaml`: 模板包元数据
- `roles/*.md`: 角色 Skill Profile（YAML frontmatter + Markdown 指令）

## 切换方式

通过环境变量切换：

```bash
export OPENCLAW_SKILL_PACK=programming
```

未设置时默认使用 `programming`。系统不再兼容旧 `dispatcher/app/roles` 路径，必须使用 `skill_packs/<pack>/roles`。
