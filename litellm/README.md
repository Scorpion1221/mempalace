# LiteLLM Proxy for MemPalace

MemPalace uses Gemini for both embedding (palace vectorisation) and the recall
LLM (rerank + save extraction). This directory ships a minimal LiteLLM proxy
that wraps the Gemini API behind an OpenAI-compatible endpoint on port 4000 —
matching the defaults in `scripts/mempalace-env.template`.

## Quick start

```bash
cd litellm
cp .env.example .env
# edit .env and paste your GEMINI_API_KEY (from https://aistudio.google.com/apikey)
docker compose up -d
curl http://127.0.0.1:4000/health/readiness  # should return 200
```

That's it. MemPalace's default `~/.mempalace/env` points to `http://127.0.0.1:4000`
with master key `sk-litellm-local`, so everything wires up automatically.

## What this provides

| Model alias (what MemPalace calls)      | Upstream                       | Purpose                     |
|-----------------------------------------|--------------------------------|-----------------------------|
| `gemini-embedding-2-preview`            | `gemini-embedding-001` (3072d) | Palace vectorisation        |
| `gemini-3.1-flash-lite-preview`         | `gemini-2.5-flash-lite`        | Recall rerank + save LLM    |
| `gemini-2.5-flash`                      | `gemini-2.5-flash`             | Higher-quality rerank (opt) |
| `gemini-2.5-pro`                        | `gemini-2.5-pro`               | Benchmark / max quality     |

The alias names are preserved so that palaces created before the real model
names settled still work — changing them would invalidate every existing
embedding dimension check.

## Security

- The proxy binds to `127.0.0.1` only. Don't expose it on the LAN — the
  master key is a convenience, not a real secret, and the upstream Gemini
  key is worth protecting.
- Change `master_key` in `config.yaml` to something random for anything
  beyond local dev, and update `MEMPAL_*_KEY` in `~/.mempalace/env`.

## Operations

```bash
docker compose logs -f              # tail proxy logs
docker compose restart              # reload after editing config.yaml
docker compose down                 # stop proxy
docker compose pull && docker compose up -d   # upgrade proxy image
```

## Dimension warning

MemPalace stores embeddings in ChromaDB with a fixed dimensionality. The
palace is initialised with whatever dimension the first embedding call
returns (3072 for `gemini-embedding-001` with `output_dimensionality: 3072`).
**Changing `output_dimensionality` in `config.yaml` after the palace exists
will silently break every upsert** — ChromaDB will reject vectors with the
wrong dimension. See `CHANGELOG.md` for the history of this failure mode.
