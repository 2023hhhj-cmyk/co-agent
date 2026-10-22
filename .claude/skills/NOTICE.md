# 前端设计 Skills 来源与许可

本目录下的前端设计 skill 均为第三方开源内容，按固定 commit 从上游 vendor 而来，随仓库分发。
升级时请重新拉取对应 commit 并更新本文件。

| Skill | 上游仓库 | 固定 commit | 许可 |
|-------|---------|-------------|------|
| `frontend-design` | `anthropics/skills` (`skills/frontend-design`) | `dbd4588f9e1033efb41dad4bef2f7947c8993d44` | 见 `frontend-design/LICENSE.txt` |
| `web-artifacts-builder` | `anthropics/skills` (`skills/web-artifacts-builder`) | `dbd4588f9e1033efb41dad4bef2f7947c8993d44` | 见 `web-artifacts-builder/LICENSE.txt` |
| `react-best-practices` | `vercel-labs/agent-skills` (`skills/react-best-practices`) | `063bee94c3f4df8453406c830b0a7df0f2860278` | MIT（见 SKILL frontmatter） |
| `web-design-guidelines` | `vercel-labs/agent-skills` (`skills/web-design-guidelines`) | `063bee94c3f4df8453406c830b0a7df0f2860278` | MIT（见 SKILL frontmatter） |
| `accessibility-engineer` | `mae616/design-skills` (`skills/accessibility-engineer`) | `79e3398bee27664de70c7b97a30dd928f6fe7976` | MIT（见同目录 `LICENSE`） |

## 用途

- `frontend-design`：设计方向与创作，避免通用 AI 审美。
- `web-artifacts-builder`：多文件 React + Tailwind CSS + shadcn/ui 构件构建流程。
- `react-best-practices`：React/Next.js 性能优化规则集。
- `accessibility-engineer`：语义 HTML、最小 ARIA、键盘与焦点可访问性。
- `web-design-guidelines`：UI 审查（运行时从 Vercel 在线地址拉取最新规范，依赖网络）。

## 已移除

- `brand-guidelines`（Anthropic 官方品牌规范）：面向通用客户站点不适用，已删除。

## 安全提示

Skill 是注入到模型上下文的指令文本，等同可执行提示。vendor 前应人工审阅内容，警惕提示注入；
本目录通过固定 commit 控制供应链，避免运行时拉取不可信版本。
