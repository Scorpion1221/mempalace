> [!CAUTION]
> **Scam alert.** The only official sources for MemPalace are this
> [GitHub repository](https://github.com/MemPalace/mempalace), the
> [PyPI package](https://pypi.org/project/mempalace/), and the docs site at
> **[mempalaceofficial.com](https://mempalaceofficial.com)**. Any other
> domain — including `mempalace.tech` — is an impostor and may distribute
> malware. Details and timeline: [docs/HISTORY.md](docs/HISTORY.md).

<div align="center">

<img src="assets/mempalace_logo.png" alt="MemPalace" width="240">

# MemPalace

Local-first AI memory. Verbatim storage, pluggable backend, 96.6% R@5 raw on LongMemEval — zero API calls.

[![][version-shield]][release-link]
[![][python-shield]][python-link]
[![][license-shield]][license-link]
[![][discord-shield]][discord-link]

</div>

---

## What it is

MemPalace stores your conversation history as verbatim text and retrieves
it with semantic search. It does not summarize, extract, or paraphrase.
The index is structured — people and projects become *wings*, topics
become *rooms*, and original content lives in *drawers* — so searches
can be scoped rather than run against a flat corpus.

The retrieval layer is pluggable. The current default is ChromaDB; the
interface is defined in [`mempalace/backends/base.py`](mempalace/backends/base.py)
and alternative backends can be dropped in without touching the rest of
the system.

Nothing leaves your machine unless you opt in.

Architecture, concepts, and mining flows:
[mempalaceofficial.com/concepts/the-palace](https://mempalaceofficial.com/concepts/the-palace.html).

---

## Install

### Quick Install (2 steps)

```bash
# Step 1: Install MemPalace
git clone git@github.com:Scorpion1221/mempalace.git ~/git/mempalace
cd ~/git/mempalace
bash install.sh                 # Python package + Claude Code/Codex plugins

# Step 2: Set up LiteLLM proxy (for embedding + recall LLM)
cd litellm
bash setup.sh                   # Auto-detects Docker/Python, guides through config
```

That's it! The scripts handle Python package, CLI, palace init, plugin sync, and
LiteLLM proxy setup. See [INSTALL.md](INSTALL.md) for details.

### AI Agent-Assisted Install

If you're using Claude Code, Codex, or Cursor, paste this one-liner into the chat:

```
请按照 https://github.com/Scorpion1221/mempalace/blob/main/docs/INSTALL-FOR-AGENTS.md 的步骤帮我安装 MemPalace
```

Or in English:

```
Please install MemPalace by following https://github.com/Scorpion1221/mempalace/blob/main/docs/INSTALL-FOR-AGENTS.md
```

Your AI assistant will fetch the guide, ask 3 questions (which agent? Gemini API
or Vertex AI? install path?), run the commands, and verify the install. Full
workflow: [docs/INSTALL-FOR-AGENTS.md](docs/INSTALL-FOR-AGENTS.md).

## Quickstart

```bash
# Mine content into the palace
mempalace mine ~/projects/myapp                    # project files
mempalace mine ~/.claude/projects/ --mode convos   # Claude Code sessions (scope with --wing per project)

# Search
mempalace search "why did we switch to GraphQL"

# Load context for a new session
mempalace wake-up
```

For Claude Code, Gemini CLI, MCP-compatible tools, and local models, see
[mempalaceofficial.com/guide/getting-started](https://mempalaceofficial.com/guide/getting-started.html).

---

## Benchmarks

All numbers below are reproducible from this repository with the commands
in [`benchmarks/BENCHMARKS.md`](benchmarks/BENCHMARKS.md). Full
per-question result files are committed under `benchmarks/results_*`.

### 三套 benchmark 全景对照（baseline vs 我们的生产栈）

| Benchmark | 基线 | 我们的 | Δ |
|---|---|---|---|
| **LongMemEval** (500q) R@5 | 0.966 | **0.980** | +1.4pp |
| **LongMemEval** R@1 | 0.806 | **0.908** | +10.2pp 🔥 |
| **LongMemEval** NDCG@5 | 0.888 | **0.945** | +5.7pp |
| **LoCoMo** (1986q) Avg R@10 | 0.603 | **0.878** | +27.5pp 🔥 |
| **LoCoMo** Perfect rate | 55.3% | **83.7%** | +28.4pp |
| **ConvoMem** (300q) R@5 | 0.794 | **0.828** | +3.4pp |
| **ConvoMem** R@10 | 0.929 | **0.931** | +0.3pp |

**Headline**: LongMemEval R@5 **96.6% → 98.0%**（刷新该 benchmark 的 SOTA），R@1 达到 **90.8%**。LoCoMo 上同一套产品代码对 baseline 的增量达 **+27.5pp**，是跨数据集验证的最强证据。

### 加入 LLM rerank 后的进一步提升

长对话场景下，Gemini Flash Lite rerank 对 top-1/3/5 精度有显著提升（R@10 不变，因为是 set-based metric）：

| Benchmark | 指标 | 我们的 | +Flash Lite rerank | Δ |
|---|---|---|---|---|
| **LoCoMo** | R@1 | 0.499 | **0.731** | +23.2pp 🔥 |
| **LoCoMo** | R@3 | 0.689 | **0.813** | +12.4pp |
| **LoCoMo** | R@5 | 0.768 | **0.838** | +7.0pp |
| **ConvoMem** | R@1 | 0.490 | **0.620** | +13.0pp 🔥 |
| **LongMemEval** | R@1 | 0.902 | **0.908** | +0.6pp |

**关键发现**：
- LLM rerank 在**长对话场景**（LoCoMo、ConvoMem）效果最显著 —— top-1 精度提升 13-23pp
- 短 QA 场景（LongMemEval）的 BM25 hybrid 已经很强，LLM rerank 增量小
- 真实使用（用户只看 top 3-5）hit rate 从 ~69%/77% 提升到 ~81%/84%
- **LoCoMo R@10 对 rerank 不敏感**（0.878 不变）—— set-based metric 只看"前 10 里有没有"，不看顺序；评估 rerank 必须看 R@1/R@3/R@5

### LongMemEval — retrieval recall (R@5, 500 questions)

| Mode | R@5 | LLM required |
|---|---|---|
| Raw (semantic search, no heuristics, no LLM) | **96.6%** | None |
| Hybrid v4, held-out 450q (tuned on 50 dev, not seen during training) | **98.4%** | None |
| Hybrid v4 + LLM rerank (full 500) | ≥99% | Any capable model |

The raw 96.6% requires no API key, no cloud, and no LLM at any stage. The
hybrid pipeline adds keyword boosting, temporal-proximity boosting, and
preference-pattern extraction; the held-out 98.4% is the honest
generalisable figure.

The rerank pipeline promotes the best candidate out of the top-20
retrieved sessions using an LLM reader. It works with any reasonably
capable model — we have reproduced it with Claude Haiku, Claude Sonnet,
Gemini Flash Lite, and minimax-m2.7 via Ollama Cloud (no Anthropic
dependency). The gap between raw and reranked is model-agnostic; we do
not headline a "100%" number because the last 0.6% was reached by
inspecting specific wrong answers, which `benchmarks/BENCHMARKS.md` flags
as teaching to the test.

### Other benchmarks

| Benchmark | Metric | Score | Notes |
|---|---|---|---|
| MemBench (ACL 2025, 8,500 items) | R@5 | 80.3% | All categories |

We deliberately do not include a side-by-side comparison against Mem0,
Mastra, Hindsight, Supermemory, or Zep. Those projects publish different
metrics on different splits, and placing retrieval recall next to
end-to-end QA accuracy is not an honest comparison. See each project's
own research page for their published numbers.

**Reproducing every result:**

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace
pip install -e ".[dev]"
# see benchmarks/README.md for dataset download commands
python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json
```

---

## Knowledge graph

MemPalace includes a temporal entity-relationship graph with validity
windows — add, query, invalidate, timeline — backed by local SQLite.
Usage and tool reference:
[mempalaceofficial.com/concepts/knowledge-graph](https://mempalaceofficial.com/concepts/knowledge-graph.html).

## MCP server

29 MCP tools cover palace reads/writes, knowledge-graph operations,
cross-wing navigation, drawer management, and agent diaries. Installation
and the full tool list:
[mempalaceofficial.com/reference/mcp-tools](https://mempalaceofficial.com/reference/mcp-tools.html).

## Agents

Each specialist agent gets its own wing and diary in the palace.
Discoverable at runtime via `mempalace_list_agents` — no bloat in your
system prompt:
[mempalaceofficial.com/concepts/agents](https://mempalaceofficial.com/concepts/agents.html).

## Auto-save hooks

Two Claude Code hooks save periodically and before context compression:
[mempalaceofficial.com/guide/hooks](https://mempalaceofficial.com/guide/hooks.html).

`UserPromptSubmit` recall also carries forward the **previous assistant
reply**, not just the current user message. For Codex and Claude Code,
the stop hook extracts the latest assistant/agent reply from the session
transcript, stores it under `~/.mempalace/hook_state/`, and the next
recall uses the tail of that reply (500 chars) as structured context for
query rewrite and reranking. Short follow-ups like “why?” or “continue”
can therefore recall the right memories without pulling in unrelated
older sessions.

---

## Requirements

- Python 3.9+
- A vector-store backend (ChromaDB by default)
- ~300 MB disk for the default embedding model

No API key is required for the core benchmark path.

## Docs

- Getting started → [mempalaceofficial.com/guide/getting-started](https://mempalaceofficial.com/guide/getting-started.html)
- CLI reference → [mempalaceofficial.com/reference/cli](https://mempalaceofficial.com/reference/cli.html)
- Python API → [mempalaceofficial.com/reference/python-api](https://mempalaceofficial.com/reference/python-api.html)
- Full benchmark methodology → [benchmarks/BENCHMARKS.md](benchmarks/BENCHMARKS.md)
- Release notes → [CHANGELOG.md](CHANGELOG.md)
- Corrections and public notices → [docs/HISTORY.md](docs/HISTORY.md)

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).

<!-- Link Definitions -->
[version-shield]: https://img.shields.io/badge/version-3.3.3-4dc9f6?style=flat-square&labelColor=0a0e14
[release-link]: https://github.com/MemPalace/mempalace/releases
[python-shield]: https://img.shields.io/badge/python-3.9+-7dd8f8?style=flat-square&labelColor=0a0e14&logo=python&logoColor=7dd8f8
[python-link]: https://www.python.org/
[license-shield]: https://img.shields.io/badge/license-MIT-b0e8ff?style=flat-square&labelColor=0a0e14
[license-link]: https://github.com/MemPalace/mempalace/blob/main/LICENSE
[discord-shield]: https://img.shields.io/badge/discord-join-5865F2?style=flat-square&labelColor=0a0e14&logo=discord&logoColor=5865F2
[discord-link]: https://discord.com/invite/ycTQQCu6kn
