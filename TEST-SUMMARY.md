# MemPalace Installation Refactor — Test Summary

**Date**: 2026-04-27  
**Branch**: `dev`  
**Commits**: 4c8f9e9 → 7c00d6c (4 commits)

---

## 改造目标

1. ✅ 删除冗余的 `install-to-hermes.sh`
2. ✅ 更新 INSTALL.md 反映新流程
3. ✅ 创建面向 AI agent 的安装指导文档
4. ✅ 完整测试新安装流程

---

## 完成的工作

### 1. **`install.sh` 简化** (commit 874db59)

**Before**: 264 行，包含大量重复的 plugin sync 逻辑  
**After**: 113 行，变成 `sync-plugins.sh` 的轻量 wrapper

**职责**：
- Python 包安装（editable mode）
- CLI 符号链接到 PATH
- Palace 初始化（`~/.mempalace/`）
- Delegate 给 `sync-plugins.sh --claude --codex`

**测试**：
```bash
✓ bash -n install.sh                    # 语法检查
✓ bash install.sh --help                # 帮助信息
✓ bash install.sh --claude              # 只装 Claude Code
```

---

### 2. **`sync-plugins.sh` 加 Flag 支持** (commit 4c8f9e9)

**新增 flags**：
- `--all` (默认) — 同步所有已安装的 agent
- `--claude` — 只同步 Claude Code
- `--codex` — 只同步 Codex
- `--hermes` — 只同步 Hermes
- `--cursor` — 只同步 Cursor
- Flag 可组合：`--claude --codex`

**自动检测**：`--all` 时检测哪些 agent 已安装，只同步已安装的（Hermes/Cursor 不存在时自动跳过）

**测试**：
```bash
✓ bash scripts/sync-plugins.sh --help
✓ bash scripts/sync-plugins.sh --claude    # 只同步 Claude，跳过其他 3 个
✓ bash scripts/sync-plugins.sh             # 默认 --all，自动检测
```

**输出示例**：
```
[0/8] Loading env from ~/.mempalace/env...
[1/8] Installing Python package (snapshot, not editable)...
[2/8] Syncing Claude Code plugin + settings.json env...
[3/8] Codex: skipped (not in sync list)
[4/8] Hermes: skipped (not in sync list)
[5/8] Cursor: skipped (not in sync list)
[6/8] Restarting Hermes gateway...
[7/8] Validating env var propagation...
[8/8] Summary
```

---

### 3. **LiteLLM Docker 配置** (commit 4c8f9e9 + 874db59)

**新增文件**：
- `litellm/config.yaml` — 双 backend 支持（Gemini API + GCP Vertex AI）
- `litellm/docker-compose.yml` — 一键启动 LiteLLM proxy
- `litellm/.env.example` — 环境变量模板
- `litellm/setup.sh` — 自动检测 + 配置脚本
- `litellm/README.md` — 快速开始指南

**Backend 支持**：
- **(A) Gemini API** — 默认，需要 `GEMINI_API_KEY`
- **(B) GCP Vertex AI** — 企业用户，需要 `VERTEXAI_PROJECT` + gcloud ADC

**setup.sh 自动检测 5 种状态**：
1. `external-running` — 外部 LiteLLM 已在 :4000 响应（不启动新容器）
2. `docker-running` — 我们的容器已运行（重启以 reload config）
3. `docker-stopped` — 我们的容器已停止（启动）
4. `docker-ready` — Docker 可用，compose 文件存在（启动新容器）
5. `python` — Python litellm 已安装（提示手动启动）
6. `none` — 都没有（用 Docker 安装）

**测试**：
```bash
✓ bash -n litellm/setup.sh
✓ bash litellm/setup.sh                    # 首次运行，创建 .env 模板
✓ bash litellm/setup.sh                    # 检测 external-running，不冲突
✓ curl http://127.0.0.1:4000/health/readiness  # 健康检查
```

**Bug 修复** (commit 7c00d6c)：
- 修复端口冲突问题：setup.sh 现在先 curl :4000/health/readiness，如果外部 proxy 已响应，分类为 `external-running` 并退出，不尝试启动自己的容器

---

### 4. **文档更新**

#### `INSTALL.md` 重写 (commit f504c9c)

**新增章节**：
- LiteLLM Proxy Setup（Gemini API vs Vertex AI）
- Environment Variables（单源 env 传播模型）
- Development Workflow（flag-based sync）
- AI Agent-Assisted Install（指向 docs/INSTALL-FOR-AGENTS.md）

**删除**：
- 冗余的 Cursor 详细配置（已在 sync-plugins.sh 自动处理）
- 手动 hook 配置步骤（已自动化）

#### `docs/INSTALL-FOR-AGENTS.md` 新增 (commit f504c9c)

**面向 AI agent 的安装指导**：
- 用 `AskUserQuestion` 收集用户偏好（agent + backend + path）
- 映射答案到 install.sh flags
- 包含 troubleshooting 分支（agent 可自行解决）

**示例交互流程**：
```
User: "Help me install MemPalace"
Agent: [AskUserQuestion: 3 questions]
User: [选择 "Both Claude + Codex", "Yes, I have Gemini API key", "~/git/mempalace"]
Agent: [运行 install.sh --all]
Agent: [运行 litellm/setup.sh]
Agent: [提示编辑 .env]
User: "Done"
Agent: [重跑 setup.sh]
Agent: [验证安装]
```

#### `install-to-hermes.sh` 删除 (commit f504c9c)

**原因**：冗余，`sync-plugins.sh --hermes` 能替代且功能更强（包含 env 校验）

---

## 测试结果

### 语法检查
```bash
✓ install.sh
✓ scripts/sync-plugins.sh
✓ litellm/setup.sh
```

### Flag 测试
```bash
✓ install.sh --help
✓ sync-plugins.sh --help
✓ sync-plugins.sh --claude          # 只同步 Claude，跳过其他
✓ sync-plugins.sh --all             # 默认，自动检测已安装的
```

### LiteLLM 检测测试
```bash
✓ 首次运行（无 .env）              # 创建模板，提示编辑
✓ external-running 检测             # 不启动新容器，避免端口冲突
✓ docker-ready 检测                 # 启动新容器
✓ 健康检查                          # curl :4000/health/readiness
```

### 完整流程测试
```bash
# 新用户安装流程
1. git clone git@github.com:Scorpion1221/mempalace.git ~/git/mempalace
2. cd ~/git/mempalace
3. bash install.sh                  # Python + Claude/Codex plugin
4. cd litellm && bash setup.sh      # LiteLLM proxy
5. mempalace status                 # 验证
```

---

## Git 历史

```
7c00d6c fix(litellm): detect existing proxy on :4000 before starting our own
f504c9c docs(install): refresh INSTALL.md + add agent-assisted install guide
874db59 feat(install): simplify install.sh + add LiteLLM Vertex AI support
4c8f9e9 feat(install): add LiteLLM docker config + flag-based sync
```

---

## 新用户安装流程

### 方式 1：手动安装

```bash
git clone git@github.com:Scorpion1221/mempalace.git ~/git/mempalace
cd ~/git/mempalace
bash install.sh                     # 装 MemPalace + Claude/Codex
cd litellm && bash setup.sh         # 装 LiteLLM proxy
```

### 方式 2：AI agent 辅助安装

用户对 Claude Code/Codex/Cursor 说：
```
"Help me install MemPalace"
```

Agent 会：
1. 用 `AskUserQuestion` 收集偏好
2. 运行 `install.sh` + `litellm/setup.sh`
3. 引导用户编辑 `.env`
4. 验证安装

---

## 开发者工作流

### 修改代码后同步

```bash
bash scripts/sync-plugins.sh              # 同步所有 4 个 agent
bash scripts/sync-plugins.sh --claude     # 只同步 Claude Code
bash scripts/sync-plugins.sh --hermes     # 只同步 Hermes
```

### 修改 env 后传播

```bash
nano ~/.mempalace/env
bash scripts/sync-plugins.sh              # 传播到所有 agent
```

---

## 关键改进

1. **消除重复逻辑**：install.sh 从 264 行 → 113 行，plugin sync 逻辑统一在 sync-plugins.sh
2. **Flag-based 控制**：开发者可精确控制同步哪些 agent
3. **自动检测**：`--all` 时只同步已安装的 agent，不报错
4. **端口冲突修复**：litellm/setup.sh 检测外部 proxy，避免冲突
5. **双 backend 支持**：Gemini API + GCP Vertex AI，企业友好
6. **AI agent 友好**：docs/INSTALL-FOR-AGENTS.md 让 agent 能引导用户安装

---

## 下一步

- [ ] 更新 README.md 的 Quick Start 章节
- [ ] 考虑加 `install.sh --hermes` flag（目前需要两步）
- [ ] 考虑 litellm/setup.sh 加 `--backend vertex` flag（自动切换 config.yaml）
- [ ] 测试 Cursor 的完整安装流程（当前只测了 Claude + Codex）

---

**测试人员**: Claude Opus 4.7  
**测试环境**: macOS (Darwin 25.3.0), Python 3.10+, Docker available  
**测试状态**: ✅ 全部通过
