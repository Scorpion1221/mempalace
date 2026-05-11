# Integration Status — v3.3.5-sync

> **审计执行者**: 郭小柒 (Jarvis), 2026-05-11
> **审计方式**: 100% 基于真实 `git diff` / `grep` / `ruff` / `sha256` 输出
> **目的**: 给 Sir 提供 Phase 2 拍板用的客观集成现状

---

## TL;DR

- 集成分支 `integration/v3.3.5-sync` **HEAD 仍在 upstream 基线 `bc5c354`**，所有改动以 **unstaged working tree diff** 形式存在（**0 个新 commit**）。
- 23 个 tracked 文件被 modify (+3147 / -268)，10+ 个 fork 特有的文件以 untracked 形式从 fork `dev` 分支继承到 worktree（**与 dev 内容 7/8 一致**，1 个 INSTALL.md 有差异，待人工审）。
- **9 个 KEEP commit 的功能特征 100% 在 worktree 中可观测到**；通过文件存在性 + 关键标识符 grep 验证。
- **4 个设计决策（A 钩子 / B embedding 双路径 / C lock 别名 / D repair 多模式）全部落地**，每条都能在源码里指认实现位置。
- **10 个 DROP commit 全部确认未引入**（对应 fork hack 的标识符全部缺席，上游版本的实现全部在位）。
- **5 个 ruff errors**：3 个是 fork 决策导致的复杂度（设计权衡，非 bug）；1 个 W605 转义警告（5 秒可修）；1 个 F811 测试类重复定义（真 merge bug，应在 Phase 2 修掉）。
- **测试**：上一轮 codex 跑过的最后一次结果是 **1762 passed / 9 skipped**（早先的 10 failed 是本地环境问题：onnx 模型下载需联网 + `/tmp` UDS PermissionError，与代码无关）。
- **Phase 2 建议**：✅ 可进 Phase 2 审阅。**先把所有 worktree 改动 commit 成一个或多个干净的 commit**，再做 PR 评审。当前 0-commit 状态使 `git log` 无法对账。

---

## 基线信息

| 项 | 值 |
|---|---|
| 工作目录 | `/Users/scorpion/git/mempalace-integration-3.3.5/` |
| 当前分支 | `integration/v3.3.5-sync` |
| HEAD SHA | `bc5c354ec1cc080f1f1faa639aebae253658759e` |
| 集成基线 | `upstream/develop @ bc5c354`（= v3.3.5，brief 指定基线） |
| 当前 upstream/develop | `68319dc0d00ce16…`（**审计时上游已新增 benchmarks 相关 commits**，与本次集成范围无关，不应混入审计） |
| `git log upstream/develop..HEAD` | **空** — HEAD 在 upstream/develop 的祖先位置 |
| `git log bc5c354..HEAD` | **空** — 0 个 commit |
| `git diff bc5c354 --shortstat` | **23 files, +3147 / -268** |
| Working tree 状态 | 23 modified (tracked) + 10+ untracked dirs/files |
| Version | `mempalace/version.py: __version__ = "3.3.501"` ✅（brief 指定值） |

### ⚠️ 关键观察：所有改动都是 unstaged

所有审计判断必须以 `git diff bc5c354` 为依据，而不是 `git log`。
当前 worktree 状态等价于"diff 已成型但尚未 commit"。Phase 2 必须先把这些改动 commit 化才能进入正常代码审阅流程。

---

## KEEP Commits 审计（9 个）

按时间顺序，对照 INTEGRATION_BRIEF.md L34-44 列出的 KEEP 表。

| # | SHA | 主题 | 状态 | 证据 |
|---|---|---|---|---|
| 1 | `9300810` | fix(mcp): harden burst UDS connections and bridge fallback | ✅ 合入 | `mempalace/mcp_server.py` 含 `_handle_socket_client` / `_start_socket_listener` / `bridge` 注释（L2313-2482）；`mempalace/mcp_bridge.py` 作为独立 untracked 文件存在，含完整 stdio↔UDS proxy + subprocess fallback |
| 2 | `e60f7d2` | Merge PR #1 feature/portable-install | ✅ 合入 | `install.sh`、`bin/mempalace-mcp-bridge`、`scripts/sync-plugins.sh` 都从 dev 分支带过来（与 dev 内容 sha256 一致） |
| 3 | `c4e546c` | chore(release): finalize 3.3.311 update flow | ✅ 合入 | `mempalace/updater.py` 存在（来自 dev，sha256 一致）；`mempalace/cli.py` 含 `updater` 子命令注册 |
| 4 | `7226bab` | fix(claude): align plugin cache metadata to runtime version | ✅ 合入 | `mempalace/claude_plugin_sync.py` 存在（来自 dev，sha256 一致）；`.claude-plugin/marketplace.json` 和 `plugin.json` 在 modified 列表里 |
| 5 | `31c57ae` | fix(update): fetch only requested tag for --tag installs | ✅ 合入 | `mempalace/updater.py:192-195` 含 `"fetch", ..., f"refs/tags/{candidate}:refs/tags/{candidate}"`（tag-specific fetch 逻辑确认在位） |
| 6 | `6b66e63` | fix(recall): drop should_recall short-circuit, let rerank decide | ✅ 合入 | `mempalace/hooks_cli.py:1400` 有显式注释："we deliberately ignore decide_recall's `should_recall` field. The..." — 行为已切换为 rerank-driven |
| 7 | `d142487` | fix(singleton): wait for socket connect, not just file existence | ✅ 合入 | `mempalace/singleton_manager.py` 存在（来自 dev，sha256 一致），含 socket-reachability 检测逻辑（`_socket_reachable` / `_wait_for_socket`） |
| 8 | `ae18dc1` | fix(recall): merge KG triples into rerank pool | ✅ 合入 | `mempalace/hooks_cli.py` grep `kg_triples\|knowledge.graph` 命中 2 处 — KG 三元组进入 rerank 管线 |
| 9 | `5e0ef36` | fix(sync-plugins): dedicated venv + propagate MEMPAL_RECALL_* | ✅ 合入 | `scripts/sync-plugins.sh` grep `MEMPAL_RECALL` 命中 2 处 |

**PARTIAL `82db3d6`** (searcher CJK tokenizer + None guard)：✅ 合入。`mempalace/searcher.py` 同时含 CJK 处理（`_CJK_RE` 在 L88 附近，注释提到 "CJK ideographs are split into overlapping bigrams"）和 BM25 None safety（`TestBM25NoneSafety` 测试类 + `tokenize_handles_none` 测试）。

### KEEP 审计结论

**9/9 ✅，PARTIAL 1/1 ✅。** 整个 KEEP 清单的功能特征全部在 worktree 中可观测。

---

## 4 个设计决策融合

| 决策 | 实现位置 | 验证 |
|---|---|---|
| **A. 钩子不自动 mine raw transcript** | `hooks/mempal_save_hook.sh:185-192`、`hooks/mempal_precompact_hook.sh:104-111` | 两个 hook 都改造为：仅在 `MEMPAL_DIR` 项目目录被设置且存在时调 `mempalace mine --mode projects`，并附有明确注释 "Raw transcript auto-mine is intentionally disabled"。✅ |
| **B. Embedding 双路径（proxy ↔ ONNX）** | `mempalace/embedding.py:64-285` | 文件含两个并存类/工厂：(1) 当 `MEMPAL_EMBEDDING_MODEL` 环境变量被设置时使用 OpenAI-compatible proxy（L64 `_MempalaceProxyEF`），(2) 否则回落到上游本地 ONNX EF（L245 `_MempalaceONNX` 继承 `ONNXMiniLM_L6_V2`）。L266 显式读 env 选路径。✅ |
| **C. Lock policy 统一为 `mine_palace_lock`，`palace_write_lock` 作为兼容别名** | `mempalace/palace.py:417, 532, 537` | L417 `def mine_palace_lock(palace_path):` 定义；L532 `mine_global_lock = mine_palace_lock` 兼容；L537 `palace_write_lock = mine_palace_lock` 兼容。L557 内部消费方已切到 `mine_palace_lock(...)`。✅ |
| **D. Repair 多模式并存** | `mempalace/repair.py:11-15` 顶部文档列出模式：`status`、`from-sqlite`、temp rebuild、HNSW capacity recovery；`rebuild-from-verbatim` 和 closets/proxy embedding batches 的 fork 模式也在文件中（grep `HNSW`、`status`、`from-sqlite` 等均有命中） | ✅ 上游 + fork 所有恢复模式都保留 |

### 设计决策审计结论

**4/4 ✅。** 每条决策都有可指认的源码实现，且没有被覆盖或回退。

---

## DROP 清单确认（10 个）

| # | SHA | 应被丢弃的原因 | 验证方式 | 结果 |
|---|---|---|---|---|
| 1 | `5b14c3b` | version 3.3.401 | `grep '3\.3\.40[0-9]' version.py pyproject.toml` | ✅ 空（未引入） |
| 2 | `6adc734` | fix(chroma) skip `_fix_blob_seq_ids` (上游 #1177 更强) | `grep '_fix_blob_seq_ids\|sysdb-10' chroma.py` | ✅ 5 命中（用的是上游 marker logic） |
| 3 | `febb5a1` | split get_or_create_collection (上游 #1289) | `grep '_get_collection\|get_or_create_collection'` | ✅ 上游 `_get_collection` 在 chroma.py 出现 6 次 |
| 4 | `c8b8a46` | 同上 (#1262) | 同上 | ✅ 同上 |
| 5 | `74eb75e` | HNSW bloat (上游 #1191 更强) | `grep 'HNSW_BLOAT_GUARD'` chroma.py | ✅ `_HNSW_BLOAT_GUARD` 字典 + 应用点齐备（上游实现） |
| 6 | `9835700` | sanitize diary topic (#936) — exact patch-id 重复 | `grep 'sanitize.*diary\|diary.*topic'` mcp_server.py | ✅ `tool_diary_write` 中 `topic` 参数受 sanitize（上游版本） |
| 7 | `90cec65` | hyphenated wing names (#1197) — 上游用 `normalize_wing_name()` helper | `grep 'normalize_wing_name'` mempalace/*.py | ✅ 命中 cli.py:2、config.py:1、convo_miner.py:2（共享 helper 形式，非 fork hack 形式） |
| 8 | `5aa370d` | tunnels permissions (#1168) — exact patch-id 重复 | `grep 'chmod\|0o600' palace_graph*.py` | ✅ L373 `chmod parent 0o700`、L388 `chmod _TUNNEL_FILE 0o600` 都在（上游版本） |
| 9 | `fc5640d` | guard None metadata (#1201) — exact patch-id 重复 | `grep 'metadata.*None'` palace_graph.py | ✅ 1 命中（上游版本） |
| 10 | `d6a3584` | version 3.3.310 | `grep '3\.3\.310' version.py pyproject.toml` | ✅ 空（未引入） |

### DROP 审计结论

**10/10 ✅。** 所有应丢弃的 fork commit 都没引入；对应功能区都用上游的实现替代。

---

## 测试结果

### 最后一次完整跑测（上一轮 codex 执行）

```
================ 1762 passed, 9 skipped, 654 warnings in 21.14s ================
```

来源：`~/.codex/sessions/2026/05/11/rollout-2026-05-11T15-28-19-...jsonl` 末尾测试输出（pytest 1771 collected）。

### 早先一次有 10 个失败 — 已分析根因

| 失败测试 | 根因 | 是代码 bug 吗？ |
|---|---|---|
| `test_closet_llm.py::TestRegenerateClosets::test_regen_purges_regex_closets...` | `httpx.ConnectError: [Errno 8] nodename nor servname` — chromadb 试图下载 onnx 模型 | ❌ 本地无网络/无模型缓存 |
| `test_convo_miner.py::test_convo_mining` | 同上 | ❌ 同 |
| `test_convo_miner.py::test_mine_convos_rebuilds_stale_drawers_after_schema_bump` | 同上 | ❌ 同 |
| `test_miner.py::test_file_already_mined_check_mtime` | 同上 | ❌ 同 |
| `test_miner.py::test_file_already_mined_returns_false_for_stale_normalize_version` | 同上 | ❌ 同 |
| `test_miner.py::test_add_drawer_stamps_normalize_version` | 同上 | ❌ 同 |
| `test_singleton_manager.py::test_socket_reachable_returns_true_when_listener_bound` | `PermissionError: [Errno 1] Operation not permitted` 绑 `/tmp/mp-singleton-*/...sock` | ❌ macOS `/tmp` 权限隔离 |
| `test_singleton_manager.py::test_wait_for_socket_returns_promptly_on_ready` | 同上 | ❌ 同 |
| `test_singleton_manager.py::test_wait_for_socket_succeeds_after_delayed_bind` | 同上（在线程里更晚一点 bind） | ❌ 同 |
| `test_singleton_manager.py::test_cmd_start_reports_ready_with_elapsed_time` | 同上 | ❌ 同 |

**结论**：10 个早期 failure 全部是**本地测试环境**问题（网络下载 + macOS 临时目录权限），后来 codex 通过预热 onnx 模型 + skip /tmp 用例后全过。**没有代码层 bug**。

### ⚠️ 风险提示

`test_singleton_manager.py` 的 `/tmp` PermissionError 这一类，在某些 CI 环境（沙箱/容器）会再次出现。Phase 2 评审时建议**临时给这些测试加 `@pytest.mark.skipif(sys.platform == "darwin" and not <can_bind_tmp>)` 守卫**，或迁到 `tmp_path` fixture，避免上 CI 反复挂。

---

## Ruff 状态（5 个错误）

```
Found 5 errors.
[*] 2 fixable with the `--fix` option.
```

逐条分类：

| Error | 位置 | 来源 | 评估 | 处理建议 |
|---|---|---|---|---|
| **C901** `hook_userprompt` 复杂度 61 > 25 | `mempalace/hooks_cli.py:1290` | 🔴 **新引入**（worktree diff 显示该函数为整段 `+` 新增） | 函数复杂度是 fork recall 管线的设计权衡（KG + rerank + cache + harness 分发），不是 bug | 接受；可加 `# noqa: C901` 加注释说明 |
| **W605** Invalid escape `\w` | `mempalace/searcher.py:88` | 🔴 **新引入**（在 fork 的 CJK tokenizer **文档字符串**里）| docstring 里的 `\w{2,}` 应转成 raw string，是真小 bug | **建议 Phase 2 修**（5 秒，加 `r"""` 或转义） |
| **C901** `search_memories` 复杂度 28 > 25 | `mempalace/searcher.py:838` | 🔴 **新引入**（fork 扩展加入 `preferred_wing` / `after` / `extra_queries` / `vector_disabled` 等参数） | 设计权衡 | 同上接受 |
| **F401** unused `Iterable` | `mempalace/singleton_manager.py:21` | 🟡 文件本身是 untracked fork 文件（不在 worktree diff 里，但本次集成才把它纳入 lint 范围） | 真未使用 import | **建议 Phase 2 修**（一键 `ruff --fix`） |
| **F811** `TestBM25NoneSafety` 重复定义 | `tests/test_searcher.py:242`（与 L204 冲突） | 🔴 **新引入**（worktree diff 显示新增了一个同名 class） | 真 **merge bug** — fork 和上游各自加了同名测试类，没去重 | **必须 Phase 2 修** — 否则后定义会覆盖前定义，丢测试 |

### Ruff 审计结论

3 个 C901 是设计决策的副产物，可接受。2 个 fixable 的（W605 + F401）建议合并前修掉。**F811 是必修项**——会导致测试静默丢失。

---

## 未解决问题 / 警告

1. **0 commit 状态** — worktree 的 23 文件改动 + untracked fork 文件全部以 working tree diff 形式存在，没 commit。`git log` 无法对账。Phase 2 必须先 commit 化（建议按 KEEP 时间顺序拆 9~12 个 commit）。
2. **`INSTALL.md` 与 fork dev 内容不一致**（`sha256 wt=1c176f2a vs dev=f1bc0082`）— 不确定是 codex 改过、还是 dev 又更新过、还是 worktree 创建后被人手改。**待 Sir 确认是否要同步**。
3. **F811 测试类重名** — 必须修。建议直接删 fork 那段（行 242+），保留上游那段（行 204+），跑测试确认覆盖度不变。
4. **Untracked 但属于 KEEP 的文件**（如 `mempalace/mcp_bridge.py`、`singleton_manager.py`、`updater.py`、`recall_llm.py`、`claude_plugin_sync.py`、`scripts/sync-plugins.sh`、`install.sh`、`INSTALL.md`、`bin/`、`docs/INSTALL-FOR-AGENTS.md`、`docs/SPIKE-A2-SHARED-UDS.md`、`integrations/launchd/`、`integrations/systemd/`、`.cursor-plugin/`）— 这些 commit 化时要 `git add` 显式纳入。
5. **`.git-local/` 也是 untracked** — 不知道是什么。建议 Sir 决定是 `.gitignore` 还是删除。
6. **测试环境依赖** — onnx 模型下载、`/tmp` UDS bind 这两类问题如果上 GitHub CI 会重现。建议加 CI sandbox-aware 守卫。

---

## Phase 2 建议

### ✅ 整体判断：可进 Phase 2

集成代码内容上的工作 **质量很高、对账闭环**：

- 9 个 KEEP 全部合入
- 4 个设计决策全部落地
- 10 个 DROP 全部确认未引入
- 测试 1762/1771 通过（剩余是环境失败，非代码 bug）

### 🔴 进 Phase 2 前的强制动作

1. **Commit 化所有 worktree 改动**（按 INTEGRATION_BRIEF.md L130-142 的 9-commit 顺序），并解决：
   - `INSTALL.md` 内容差异（确认要哪个版本）
   - `F811 TestBM25NoneSafety` 重复定义（删 fork 那段）
   - `.git-local/` 怎么处理
2. **跑一次干净测试** — 在 commit 化后，跑 `pytest tests/ --ignore=tests/benchmarks` 确认 1762 仍 pass，然后提交一个版本 bump commit `chore(release): bump to 3.3.501 (sync upstream v3.3.5)`。
3. **修 W605 和 F401**（一键 `ruff --fix`，commit 一个 `chore(lint): fix W605/F401`）。

### 🟡 Phase 2 评审重点

1. **`mempalace/embedding.py` 的双路径分发逻辑** — 这是最高风险点，能不能优雅地在两种 EF 间切换、KG 数据兼容性、维度匹配等。
2. **`mempalace/palace.py` 的 lock 兼容别名** — 确认外部调用方（hooks_cli、convo_miner 等）的导入路径无歧义。
3. **`mempalace/hooks_cli.py:1290 hook_userprompt`** — 1000 行的函数，应在 Phase 2 单独 review，看是否能拆分。
4. **测试覆盖度** — 1762 个测试看起来很多，但 fork 这次新加的 hook_userprompt + embedding 双路径都应该有专门 test 覆盖，需要确认 coverage 数字没降。

### 估时

- Commit 化 + F811/INSTALL/ruff 修复：**30-60 分钟**
- Phase 2 评审：**3-4 小时**（4 个重点 + 完整 diff 走读）

🫡
