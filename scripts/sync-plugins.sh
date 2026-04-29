#!/usr/bin/env bash
# sync-mempalace-plugins.sh — One command to sync plugin files AND propagate
# env vars from a single source (~/.mempalace/env) into each agent's native
# config format.
#
# Agents covered: Claude Code, Codex, Hermes, Cursor.
# One edit in ~/.mempalace/env → one run → all selected agents aligned.
#
# Usage:
#   bash sync-plugins.sh [--all] [--claude] [--codex] [--hermes] [--cursor]
#
#   No flags = --all (sync everything; Hermes/Cursor auto-skip if not installed).
#   Mixing flags: bash sync-plugins.sh --claude --codex   (sync only those two).
set -euo pipefail

REPO="$HOME/git/mempalace"
HERMES_REPO="$REPO/integrations/hermes"
ENV_FILE="$HOME/.mempalace/env"
ENV_TEMPLATE="$REPO/scripts/mempalace-env.template"

# Parse flags — positive "which agents to sync" instead of skip list.
SYNC_CLAUDE=false
SYNC_CODEX=false
SYNC_HERMES=false
SYNC_CURSOR=false
if [ $# -eq 0 ]; then
    SYNC_CLAUDE=true; SYNC_CODEX=true; SYNC_HERMES=true; SYNC_CURSOR=true
else
    for arg in "$@"; do
        case "$arg" in
            --all)     SYNC_CLAUDE=true; SYNC_CODEX=true; SYNC_HERMES=true; SYNC_CURSOR=true ;;
            --claude)  SYNC_CLAUDE=true ;;
            --codex)   SYNC_CODEX=true ;;
            --hermes)  SYNC_HERMES=true ;;
            --cursor)  SYNC_CURSOR=true ;;
            --help|-h)
                echo "Usage: bash sync-plugins.sh [--all] [--claude] [--codex] [--hermes] [--cursor]"
                echo "  No flags        Sync all four (Hermes/Cursor auto-skip if not installed)"
                echo "  --all           Same as no flags"
                echo "  --claude        Sync Claude Code only"
                echo "  --codex         Sync Codex only"
                echo "  --hermes        Sync Hermes only"
                echo "  --cursor        Sync Cursor only"
                echo "  Flags combine:  --claude --codex = sync just those two"
                exit 0
                ;;
            *) echo "Unknown option: $arg (use --help)"; exit 1 ;;
        esac
    done
fi

# Vars propagated from ~/.mempalace/env into each agent's native config.
# REQUIRED_VARS must be non-empty or the script aborts (embedding is needed
# for any palace operation). OPTIONAL_VARS are propagated when set but their
# absence is tolerated — offline / Path D installs intentionally leave them
# empty. Legacy MEMPAL_RECALL_* aliases are still honored at runtime but are
# NOT propagated or validated here.
REQUIRED_VARS="MEMPAL_EMBEDDING_MODEL MEMPAL_EMBEDDING_ENDPOINT MEMPAL_EMBEDDING_KEY"
OPTIONAL_VARS="MEMPAL_LLM_ENDPOINT MEMPAL_LLM_MODEL MEMPAL_LLM_KEY"
PROPAGATED_VARS="$REQUIRED_VARS $OPTIONAL_VARS"

# Smart copy: preserve local env-var fallback defaults in hook scripts
# (user's override survives a sync).
_sed_inplace() {
    if sed --version 2>/dev/null | grep -q GNU; then
        sed -i "$@"
    else
        sed -i '' "$@"
    fi
}

smart_copy_hook() {
    local src="$1" dst="$2"
    if [ ! -f "$dst" ]; then
        cp "$src" "$dst"
        chmod +x "$dst"  # hook scripts MUST be executable — agents invoke
                         # them directly (no `bash script.sh`), so missing
                         # +x → exit 126 ("Permission denied"). Defensive:
                         # if the source was somehow shipped without +x
                         # (cp preserves mode), force the bit on every copy.
        echo "  → $(basename "$dst") (new)"
        return
    fi
    local saved_lines=""
    while IFS= read -r line; do
        local default_val
        default_val=$(echo "$line" | sed -n 's/.*:-\(.*\)}.*/\1/p')
        if [ -n "$default_val" ]; then
            saved_lines="${saved_lines}${line}"$'\n'
        fi
    done < <(grep '^export ' "$dst" 2>/dev/null || true)
    cp "$src" "$dst"
    chmod +x "$dst"  # see new-file branch above for rationale.
    if [ -n "$saved_lines" ]; then
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            local var_name
            var_name=$(echo "$line" | sed 's/export \([A-Z_]*\)=.*/\1/')
            if [ -n "$var_name" ] && grep -q "^export ${var_name}=" "$dst"; then
                _sed_inplace "s|^export ${var_name}=.*|${line}|" "$dst"
                echo "  → preserved local $var_name"
            fi
        done <<< "$saved_lines"
        echo "  → $(basename "$dst") (merged)"
    else
        echo "  → $(basename "$dst") (updated)"
    fi
}

echo "=== Syncing MemPalace plugins to all 4 agents ==="

# --- [0/8] Load single-source env file --------------------------------------
echo "[0/8] Loading env from $ENV_FILE..."
if [ ! -f "$ENV_FILE" ]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    cp "$ENV_TEMPLATE" "$ENV_FILE"
    echo "  → created $ENV_FILE from template (edit this to customise)"
fi
# shellcheck disable=SC1090
source "$ENV_FILE"
MISSING=""
for var in $REQUIRED_VARS; do
    if [ -z "${!var:-}" ]; then
        MISSING="$MISSING $var"
    fi
done
if [ -n "$MISSING" ]; then
    echo "  ⚠ After sourcing $ENV_FILE, these required vars are still empty:$MISSING"
    echo "    Edit $ENV_FILE and re-run. Aborting."
    exit 1
fi
OPTIONAL_MISSING=""
for var in $OPTIONAL_VARS; do
    if [ -z "${!var:-}" ]; then
        OPTIONAL_MISSING="$OPTIONAL_MISSING $var"
    fi
done
if [ -n "$OPTIONAL_MISSING" ]; then
    echo "  ⚠ Optional LLM vars not set:$OPTIONAL_MISSING (offline mode — auto-save uses verbatim fallback)"
fi
LOADED_COUNT=0
for var in $PROPAGATED_VARS; do
    [ -n "${!var:-}" ] && LOADED_COUNT=$((LOADED_COUNT + 1))
done
echo "  ✓ $LOADED_COUNT MEMPAL_* vars loaded from single source"

# --- [1/8] Python package snapshot install ----------------------------------
echo "[1/8] Installing Python package (snapshot, not editable)..."
pip install --force-reinstall --no-deps "$REPO" -q 2>/dev/null
echo "  → $(python3 -c 'import mempalace; print(f"mempalace {mempalace.__version__}")')"

# --- [2/8] Claude Code: sync plugin cache + upsert env in settings.json -----
CLAUDE_CACHE=""
if [ -f "$HOME/.claude/plugins/installed_plugins.json" ]; then
    CLAUDE_CACHE=$(python3 -c "
import json
with open('$HOME/.claude/plugins/installed_plugins.json') as f:
    data = json.load(f)
for key, entries in data.get('plugins', {}).items():
    if 'mempalace' in key.lower() and entries:
        print(entries[0].get('installPath', ''))
        break
" 2>/dev/null)
fi
if [ -z "$CLAUDE_CACHE" ] || [ ! -d "$CLAUDE_CACHE" ]; then
    # `find` exits 1 when the dir doesn't exist; pipefail then kills the whole
    # script under macOS bash 3.2 + `set -euo pipefail`. Suppress that — empty
    # CLAUDE_CACHE is handled by the next branch and is the expected case
    # before the user has installed the Claude Code plugin yet.
    CLAUDE_CACHE=$( { find "$HOME/.claude/plugins/cache/mempalace/mempalace" -maxdepth 1 -mindepth 1 -type d 2>/dev/null || true; } | head -1)
fi
if ! $SYNC_CLAUDE; then
    echo "[2/8] Claude Code: skipped (not in sync list)"
elif [ -n "$CLAUDE_CACHE" ] && [ -d "$CLAUDE_CACHE" ]; then
    echo "[2/8] Syncing Claude Code plugin + settings.json env..."
    for f in "$REPO/.claude-plugin/hooks/"mempal-*.sh; do
        [ -f "$f" ] && smart_copy_hook "$f" "$CLAUDE_CACHE/hooks/$(basename "$f")"
    done
    cp "$REPO/.claude-plugin/plugin.json" "$CLAUDE_CACHE/plugin.json" 2>/dev/null && echo "  → plugin.json synced" || true

    # Deploy canonical skill (skills/mempalace/SKILL.md) into plugin cache.
    # The .claude-plugin/skills/mempalace/SKILL.md in the repo is a symlink
    # back to the canonical file — agents need a real file in their runtime
    # cache, so we resolve and copy here instead of preserving the symlink.
    mkdir -p "$CLAUDE_CACHE/skills/mempalace"
    cp "$REPO/skills/mempalace/SKILL.md" "$CLAUDE_CACHE/skills/mempalace/SKILL.md" \
        && echo "  → skills/mempalace/SKILL.md synced from canonical" || true

    # Also sync to the user-installed skills dir (~/.claude/skills/mempalace/).
    # Claude Code reads from BOTH the plugin cache AND ~/.claude/skills/<name>/,
    # and Paperclip-spawned subagents (claude_local adapter) often resolve the
    # latter path first. If we only update plugin cache, those subagents see
    # stale skill content — confirmed in the wild for SUP-94 (2026-04-26).
    USER_SKILL_DIR="$HOME/.claude/skills/mempalace"
    if [ -d "$USER_SKILL_DIR" ]; then
        cp "$REPO/skills/mempalace/SKILL.md" "$USER_SKILL_DIR/SKILL.md" \
            && echo "  → skills/mempalace/SKILL.md synced to $USER_SKILL_DIR" || true
    fi

    # Upsert env vars into ~/.claude/settings.json
    CLAUDE_SETTINGS="$HOME/.claude/settings.json"
    if [ -f "$CLAUDE_SETTINGS" ]; then
        python3 - <<PYEOF
import json, os
path = "$CLAUDE_SETTINGS"
with open(path) as f:
    cfg = json.load(f)
env = cfg.setdefault("env", {})
vars_to_set = "$PROPAGATED_VARS".split()
changed = []
for v in vars_to_set:
    new_val = os.environ.get(v, "")
    if env.get(v) != new_val:
        env[v] = new_val
        changed.append(v)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
if changed:
    print(f"  → updated {len(changed)} env vars in settings.json: {', '.join(changed)}")
else:
    print("  → settings.json env already in sync")
PYEOF
    fi
else
    echo "[2/8] Claude Code plugin cache not found, skipping"
fi

# --- [3/8] Codex: install/sync plugin + upsert config.toml ------------------
# Codex's "plugin manager" is filesystem-based — there is no IDE-side
# `/plugin install` step like Claude Code has. Plugins live in
# ~/.agents/plugins/<name>/ and get registered in ~/.codex/config.toml under
# [plugins."<name>"]. So this step does the full install: mkdir + copy +
# register, not just refresh.
CODEX_PLUGIN="$HOME/.agents/plugins/mempalace/.codex-plugin"
if ! $SYNC_CODEX; then
    echo "[3/8] Codex: skipped (not in sync list)"
else
    if [ ! -d "$CODEX_PLUGIN" ]; then
        echo "[3/8] Installing Codex plugin (first time at $CODEX_PLUGIN)..."
        mkdir -p "$CODEX_PLUGIN/hooks" "$CODEX_PLUGIN/skills"
    else
        echo "[3/8] Syncing Codex plugin + config.toml env..."
    fi
    for f in "$REPO/.codex-plugin/hooks/"*.sh; do
        [ -f "$f" ] && smart_copy_hook "$f" "$CODEX_PLUGIN/hooks/$(basename "$f")"
    done
    cp "$REPO/.codex-plugin/plugin.json" "$CODEX_PLUGIN/plugin.json" 2>/dev/null && echo "  → plugin.json synced" || true
    cp "$REPO/.codex-plugin/hooks.json" "$CODEX_PLUGIN/hooks.json" 2>/dev/null && echo "  → hooks.json synced (plugin-local)" || true

    # Merge mempalace's hook entries into ~/.codex/hooks.json (USER-LEVEL).
    # Codex reads user-level hooks from ~/.codex/hooks.json with absolute
    # paths; the plugin-local hooks.json is not enough on its own. Without
    # this merge, codex_hooks=true and the plugin being registered still
    # leaves the UserPromptSubmit/SessionStart/Stop hooks unfired.
    #
    # Merge semantics:
    #   - Replace existing mempalace entries (identified by command path
    #     containing the absolute mempalace plugin path) before appending —
    #     keeps re-runs idempotent.
    #   - Preserve unrelated hook entries the user has for the same events.
    #   - Resolve ${CODEX_PLUGIN_ROOT} → absolute $CODEX_PLUGIN.
    USER_HOOKS_JSON="$HOME/.codex/hooks.json"
    SRC_HOOKS_JSON="$REPO/.codex-plugin/hooks.json"
    if [ -f "$SRC_HOOKS_JSON" ]; then
        export USER_HOOKS_JSON SRC_HOOKS_JSON CODEX_PLUGIN
        python3 - <<'PYEOF'
import json, os, pathlib
src_path = pathlib.Path(os.environ["SRC_HOOKS_JSON"])
dst_path = pathlib.Path(os.environ["USER_HOOKS_JSON"])
plugin_root = os.environ["CODEX_PLUGIN"]

src = json.loads(src_path.read_text())
src_hooks = src.get("hooks", {})

# Resolve ${CODEX_PLUGIN_ROOT} placeholder to absolute path so user-level
# hooks.json doesn't depend on Codex's plugin-context env expansion.
def resolve(obj):
    if isinstance(obj, dict):
        return {k: resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve(v) for v in obj]
    if isinstance(obj, str):
        return obj.replace("${CODEX_PLUGIN_ROOT}", plugin_root)
    return obj
src_hooks = resolve(src_hooks)

# Load existing user hooks (or initialise).
if dst_path.exists():
    try:
        dst = json.loads(dst_path.read_text() or "{}")
    except json.JSONDecodeError:
        # Don't clobber a broken user file; back it up and start fresh.
        backup = dst_path.with_suffix(".json.broken")
        dst_path.rename(backup)
        print(f"  ⚠ {dst_path} was not valid JSON — backed up to {backup}")
        dst = {}
else:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst = {}

dst.setdefault("hooks", {})

# Mempalace entries are identified by the absolute path of our plugin dir
# appearing in the inner "command" string. Anything else is left alone.
def is_mempalace(matcher_block):
    for h in matcher_block.get("hooks", []):
        if plugin_root in str(h.get("command", "")):
            return True
    return False

events_changed = 0
for event, src_matchers in src_hooks.items():
    existing = dst["hooks"].get(event, [])
    # Strip our previous entries (by command-path identity) — preserves any
    # unrelated entries the user added for the same event.
    kept = [m for m in existing if not is_mempalace(m)]
    dst["hooks"][event] = kept + src_matchers
    events_changed += 1

dst_path.write_text(json.dumps(dst, indent=2, ensure_ascii=False) + "\n")
print(f"  → merged {events_changed} hook event(s) into {dst_path}")
PYEOF
    fi

    # Sync the 5 Codex skill stubs (search/status/mine/help/init). Each is a
    # tiny SKILL.md that calls `mempalace instructions <name>` — the actual
    # docstring lives in mempalace/instructions/*.md inside the Python package,
    # which got refreshed by the snapshot install in [1/8]. So the stubs are
    # the only thing to copy.
    for s in "$REPO/.codex-plugin/skills/"*/SKILL.md; do
        [ -f "$s" ] || continue
        name=$(basename "$(dirname "$s")")
        mkdir -p "$CODEX_PLUGIN/skills/$name"
        cp "$s" "$CODEX_PLUGIN/skills/$name/SKILL.md"
    done
    echo "  → 5 codex skill stubs synced"

    # Upsert env vars + plugin registration into ~/.codex/config.toml.
    # Three blocks managed:
    #   - [mcp_servers.mempalace]   — command + env (created if missing)
    #   - [shell_environment_policy.set] — newline KV (only MEMPAL_* touched)
    #   - [plugins."mempalace"]      — enabled = true (created if missing)
    CODEX_CONFIG="$HOME/.codex/config.toml"
    if [ -f "$CODEX_CONFIG" ]; then
        # Quoted heredoc: bash does NOT interpolate inside, so Python comments
        # may safely contain (), [], $, etc. Pass shell vars via env instead.
        export CODEX_CONFIG PROPAGATED_VARS
        python3 - <<'PYEOF'
import os, re
path = os.environ["CODEX_CONFIG"]
with open(path) as f:
    content = f.read()
vars_to_set = os.environ["PROPAGATED_VARS"].split() + ["SSL_CERT_FILE"]
env_vals = {v: os.environ.get(v, "") for v in vars_to_set}

# 0. [mcp_servers.mempalace] — bootstrap the block if missing so the env
#    upsert below has something to write into. Without this, a fresh Codex
#    config silently never gets the mempalace MCP server registered.
if not re.search(r'^\[mcp_servers\.mempalace\]', content, re.MULTILINE):
    if content and not content.endswith("\n"):
        content += "\n"
    content += "\n[mcp_servers.mempalace]\ncommand = \"mempalace-mcp\"\nenv = {}\n"
    print("  → bootstrapped [mcp_servers.mempalace] block")

# 0b. [plugins."mempalace"] — register the plugin so Codex picks it up at
#     startup. Without this, the files in ~/.agents/plugins/mempalace/ are
#     just dead weight on disk.
if not re.search(r'^\[plugins\."mempalace"\]', content, re.MULTILINE):
    if content and not content.endswith("\n"):
        content += "\n"
    content += "\n[plugins.\"mempalace\"]\nenabled = true\n"
    print("  → registered [plugins.\"mempalace\"] enabled = true")

# 0c. [shell_environment_policy.set] — bootstrap with env populated if
#     missing. Without this, fresh Codex configs fail [7/8] validation
#     with all "Codex (shell): MEMPAL_* is missing" errors — the upserter
#     at step 2 silently skips because re.search returns None.
#
#     Note: we populate the bootstrapped block with the env vars directly
#     instead of writing an empty block + relying on the upserter. The
#     upserter substitutes group(2) of `(\[...\])([^\[]*)` — group(2)
#     starts right after the `]` and has no leading `\n`, so an empty
#     bootstrap produces `[shell_environment_policy.set]MEMPAL_FOO=...`
#     (invalid TOML) when the upserter writes back. Pre-populating sidesteps
#     the regex's missing-leading-newline behavior; the upserter then
#     processes a non-empty body correctly.
if not re.search(r'^\[shell_environment_policy\.set\]', content, re.MULTILINE):
    if content and not content.endswith("\n"):
        content += "\n"
    content += "\n[shell_environment_policy.set]\n"
    for v in vars_to_set:
        val = env_vals.get(v, "")
        if val:
            content += f'{v} = "{val}"\n'
    print("  → bootstrapped [shell_environment_policy.set] block (populated)")

# 0d. [features].codex_hooks = true — Codex's hooks are gated behind this
#     feature flag. Without it, the hooks.json events (UserPromptSubmit,
#     SessionStart, Stop) won't fire even though hooks.json is on disk.
#     Idempotent: only adds if not already mentioned (preserves an explicit
#     `codex_hooks = false` if a user has intentionally disabled it).
features_match = re.search(r'^\[features\]([^\[]*)', content, re.MULTILINE | re.DOTALL)
if features_match:
    if not re.search(r'^\s*codex_hooks\s*=', features_match.group(1), re.MULTILINE):
        body = features_match.group(1).rstrip("\n")
        new_body = body + "\ncodex_hooks = true\n\n"
        content = content[:features_match.start(1)] + new_body + content[features_match.end(1):]
        print("  → added codex_hooks = true to existing [features] block")
else:
    if content and not content.endswith("\n"):
        content += "\n"
    content += "\n[features]\ncodex_hooks = true\n"
    print("  → bootstrapped [features] block with codex_hooks = true")

# 1. [mcp_servers.mempalace] env = { ... } — one-line inline table.
#    Rebuild the inline value entirely since single-line TOML is painful to
#    partial-edit.
pairs = ", ".join(f'{k} = "{v}"' for k, v in env_vals.items() if v)
m = re.search(r'(\[mcp_servers\.mempalace\][^\[]*?)env\s*=\s*\{[^}]*\}', content, re.DOTALL)
if m:
    content = content[:m.start()] + m.group(1) + "env = { " + pairs + " }" + content[m.end():]

# 2. [shell_environment_policy.set] — newline-separated KEY = "VAL" entries.
#    Only touch the MEMPAL_* set; preserve any unrelated keys (e.g.
#    CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS, SSL_CERT_FILE) already present.
sp_match = re.search(r'(\[shell_environment_policy\.set\])([^\[]*)', content, re.DOTALL)
if sp_match:
    block_body = sp_match.group(2)
    lines = block_body.split("\n")
    new_lines = []
    seen = set()
    for line in lines:
        s = line.strip()
        key_match = re.match(r'([A-Z_][A-Z0-9_]*)\s*=', s)
        if key_match:
            key = key_match.group(1)
            if key in vars_to_set:
                val = env_vals.get(key, "")
                if val:
                    new_lines.append(f'{key} = "{val}"')
                    seen.add(key)
                    continue
                else:
                    continue  # drop
        new_lines.append(line)
    # Append any MEMPAL_* vars not already in the block.
    insert_idx = len(new_lines)
    for i in range(len(new_lines) - 1, -1, -1):
        if new_lines[i].strip() == "":
            insert_idx = i
        else:
            break
    for v in vars_to_set:
        if v not in seen and env_vals.get(v):
            new_lines.insert(insert_idx, f'{v} = "{env_vals[v]}"')
            insert_idx += 1
    new_body = "\n".join(new_lines)
    content = content[:sp_match.start(2)] + new_body + content[sp_match.end(2):]

with open(path, "w") as f:
    f.write(content)
print("  → config.toml env upserted in [mcp_servers.mempalace] and [shell_environment_policy.set]")
PYEOF
    else
        echo "  ⚠ ~/.codex/config.toml not found — Codex CLI not installed?"
        echo "    Install Codex first, then re-run: bash $REPO/scripts/sync-plugins.sh --codex"
    fi
fi

# --- [4/8] Hermes: sync plugin + upsert launchd plist env -------------------
HERMES_RUNTIME="$HOME/.hermes/hermes-agent/plugins/memory/mempalace"
HERMES_PLIST="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"
if ! $SYNC_HERMES; then
    echo "[4/8] Hermes: skipped (not in sync list)"
elif [ -f "$HERMES_REPO/plugins/memory/mempalace/__init__.py" ]; then
    echo "[4/8] Syncing Hermes plugin + plist env..."
    mkdir -p "$HERMES_RUNTIME"
    for f in "$HERMES_REPO/plugins/memory/mempalace/"*.py \
             "$HERMES_REPO/plugins/memory/mempalace/"*.yaml \
             "$HERMES_REPO/plugins/memory/mempalace/"*.md; do
        [ -e "$f" ] || continue
        cp "$f" "$HERMES_RUNTIME/$(basename "$f")"
    done
    echo "  → plugin files synced"

    HERMES_VENV="$HOME/.hermes/hermes-agent/venv"
    if [ -f "$HERMES_VENV/bin/python" ]; then
        "$HERMES_VENV/bin/python" -m pip install --force-reinstall --no-deps "$REPO" -q 2>/dev/null && echo "  → Hermes venv updated" || true
    fi

    # Upsert env vars into the launchd plist.
    if [ -f "$HERMES_PLIST" ]; then
        for var in $PROPAGATED_VARS; do
            val="${!var:-}"
            [ -z "$val" ] && continue
            # PlistBuddy's Add fails if key exists, Set fails if key doesn't.
            # Try Set first, fall back to Add.
            /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:$var $val" "$HERMES_PLIST" 2>/dev/null \
                || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:$var string $val" "$HERMES_PLIST" 2>/dev/null || true
        done
        echo "  → 7 env vars upserted in launchd plist"
    fi
else
    echo "[4/8] Hermes plugin source not found, skipping"
fi

# --- [5/8] Cursor: symlink plugin + register hooks + launchctl env ---------
if ! $SYNC_CURSOR; then
    echo "[5/8] Cursor: skipped (not in sync list)"
else
CURSOR_PLUGIN="$HOME/.cursor/plugins/local/mempalace"
CURSOR_HOOKS_JSON="$HOME/.cursor/hooks.json"
echo "[5/8] Installing Cursor plugin..."
mkdir -p "$HOME/.cursor/plugins/local"
if [ ! -L "$CURSOR_PLUGIN" ] && [ ! -d "$CURSOR_PLUGIN" ]; then
    ln -s "$REPO/.cursor-plugin" "$CURSOR_PLUGIN"
    echo "  → symlink $CURSOR_PLUGIN → $REPO/.cursor-plugin"
elif [ -L "$CURSOR_PLUGIN" ]; then
    echo "  → symlink exists"
else
    # Non-symlink directory exists (e.g. user copied manually). Sync files.
    for f in "$REPO/.cursor-plugin/hooks/"*.sh; do
        [ -f "$f" ] && smart_copy_hook "$f" "$CURSOR_PLUGIN/hooks/$(basename "$f")"
    done
    cp "$REPO/.cursor-plugin/plugin.json" "$CURSOR_PLUGIN/plugin.json" 2>/dev/null && echo "  → plugin.json synced" || true
    cp "$REPO/.cursor-plugin/hooks.json" "$CURSOR_PLUGIN/hooks.json" 2>/dev/null && echo "  → hooks.json synced" || true
fi

# Cursor does NOT auto-discover plugin-local hooks.json (verified empirically
# 2026-04-25 — only ~/.cursor/hooks.json, project hooks, and Claude Code
# compatibility settings are read). So we merge our hook entries into the
# user-scoped hooks.json with absolute paths to the plugin's hook script.
HOOK_CMD="$CURSOR_PLUGIN/hooks/mempal-hook.sh"
python3 - <<PYEOF
import json, os, pathlib
path = pathlib.Path("$CURSOR_HOOKS_JSON")
hook_cmd = "$HOOK_CMD"
if path.exists():
    with open(path) as f:
        cfg = json.load(f)
else:
    cfg = {"version": 1, "hooks": {}}
cfg.setdefault("version", 1)
hooks = cfg.setdefault("hooks", {})

events = {
    "sessionStart": "session-start",
    "stop": "stop",
    "preCompact": "precompact",
}
# NOTE: beforeSubmitPrompt intentionally NOT wired. Empirically (Cursor
# 3.1.17, tested 2026-04-25) both `user_message` and `additional_context`
# response fields are silently dropped from the agent's prompt context for
# this event — Cursor's docs describe it as permission-only, and that turns
# out to be literal. Running the hook regardless would cost ~5s of LLM
# rewrite+rerank per user message with no user-visible benefit. Cursor gets
# its recall via sessionStart (map) + the agent calling mempalace_search
# MCP on demand (driven by sessionStart instructions).
changed = []
for cursor_event, mempal_arg in events.items():
    arr = hooks.setdefault(cursor_event, [])
    desired_cmd = f"{hook_cmd} {mempal_arg}"
    # Drop any pre-existing mempalace hook for this event so we don't dupe.
    arr = [h for h in arr if "mempal-hook.sh" not in (h.get("command") or "")]
    arr.append({
        "command": desired_cmd,
        "type": "command",
        "timeout": 30,
    })
    hooks[cursor_event] = arr
    changed.append(cursor_event)

# Prune mempalace hooks from any event that is NOT in the current events
# map. This removes stale hooks from previous script versions (e.g. the
# beforeSubmitPrompt entry that was registered before we confirmed
# Cursor's user_message field doesn't actually work for context injection).
pruned = []
for event_name in list(hooks.keys()):
    if event_name in events:
        continue
    arr = hooks[event_name]
    filtered = [h for h in arr if "mempal-hook.sh" not in (h.get("command") or "")]
    if len(filtered) != len(arr):
        pruned.append(event_name)
    if filtered:
        hooks[event_name] = filtered
    else:
        del hooks[event_name]

path.parent.mkdir(parents=True, exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(f"  → registered {len(changed)} hooks in {path}: {', '.join(changed)}")
if pruned:
    print(f"  → pruned stale mempalace hooks from: {', '.join(pruned)}")
PYEOF

# Push env vars into the macOS GUI session so Cursor (launched via
# LaunchServices, which does NOT source ~/.zshrc) sees them.
for var in $PROPAGATED_VARS; do
    val="${!var:-}"
    [ -z "$val" ] && continue
    launchctl setenv "$var" "$val"
done
echo "  → 7 env vars set via launchctl (current GUI session)"

# Persist across reboots via a LaunchAgent plist.
ENV_PLIST="$HOME/Library/LaunchAgents/ai.mempalace.env.plist"
python3 - <<PYEOF
import os, plistlib
path = "$ENV_PLIST"
vars_to_set = "$PROPAGATED_VARS".split()
# Build a single shell line: launchctl setenv K1 V1 && launchctl setenv K2 V2 && ...
parts = []
for v in vars_to_set:
    val = os.environ.get(v, "").replace('"', '\\"')
    parts.append(f'launchctl setenv {v} "{val}"')
cmd = " && ".join(parts)
plist = {
    "Label": "ai.mempalace.env",
    "ProgramArguments": ["/bin/sh", "-c", cmd],
    "RunAtLoad": True,
    "KeepAlive": False,
}
with open(path, "wb") as f:
    plistlib.dump(plist, f)
print("  → wrote $ENV_PLIST (reapplies env on login)")
PYEOF
# Reload so the plist is active immediately.
launchctl unload "$ENV_PLIST" 2>/dev/null || true
launchctl load "$ENV_PLIST" 2>/dev/null || true
fi

# --- [6/8] Restart Hermes if running ----------------------------------------
echo "[6/8] Restarting Hermes gateway..."
if $SYNC_HERMES && pgrep -f "hermes_cli.main gateway" >/dev/null 2>&1; then
    kill $(pgrep -f "hermes_cli.main gateway") 2>/dev/null
    sleep 2
    if pgrep -f "hermes_cli.main gateway" >/dev/null 2>&1; then
        echo "  → Hermes restarted (launchd respawn)"
    else
        echo "  → Hermes killed, waiting for launchd respawn..."
        sleep 3
    fi
else
    echo "  → Hermes not running, skipping"
fi

# --- [7/8] Validation: all agents' env values MATCH ~/.mempalace/env --------
echo "[7/8] Validating env var propagation..."
DRIFT=0
check_agent_var() {
    local agent="$1" var="$2" actual="$3"
    local expected="${!var:-}"
    if [ -z "$actual" ]; then
        echo "  ⚠ $agent: $var is missing"
        DRIFT=1
    elif [ "$actual" != "$expected" ]; then
        echo "  ⚠ $agent: $var drift (got '$actual', expected '$expected')"
        DRIFT=1
    fi
}

# Claude Code (settings.json)
if [ -f "$HOME/.claude/settings.json" ]; then
    for var in $PROPAGATED_VARS; do
        actual=$(python3 -c "import json; print(json.load(open('$HOME/.claude/settings.json')).get('env', {}).get('$var', ''))" 2>/dev/null || echo "")
        check_agent_var "Claude Code" "$var" "$actual"
    done
fi

# Codex (config.toml — two sections)
if [ -f "$HOME/.codex/config.toml" ]; then
    # Python does the TOML parsing reliably (awk range matching on section
    # headers that start with '[' is fragile — the range's terminator regex
    # matches the section header line itself).
    CODEX_ACTUAL=$(python3 - <<'PYEOF'
import json, re
with open(f"{__import__('os').path.expanduser('~')}/.codex/config.toml") as f:
    content = f.read()
out = {"mcp": {}, "shell": {}}
# MCP inline table
m = re.search(r'\[mcp_servers\.mempalace\].*?env\s*=\s*\{([^}]*)\}', content, re.DOTALL)
if m:
    for pair in m.group(1).split(","):
        kv = pair.strip().split("=", 1)
        if len(kv) == 2:
            k, v = kv[0].strip(), kv[1].strip().strip('"')
            out["mcp"][k] = v
# Shell-env block — read lines between the header and the next '[' header.
sp = re.search(r'\[shell_environment_policy\.set\]\n(.*?)(?=^\[|\Z)', content, re.DOTALL | re.MULTILINE)
if sp:
    for line in sp.group(1).splitlines():
        mm = re.match(r'\s*([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]*)"', line)
        if mm:
            out["shell"][mm.group(1)] = mm.group(2)
print(json.dumps(out))
PYEOF
)
    for var in $PROPAGATED_VARS; do
        actual_mcp=$(echo "$CODEX_ACTUAL" | python3 -c "import json,sys; print(json.load(sys.stdin)['mcp'].get('$var',''))")
        check_agent_var "Codex (MCP)" "$var" "$actual_mcp"
        actual_shell=$(echo "$CODEX_ACTUAL" | python3 -c "import json,sys; print(json.load(sys.stdin)['shell'].get('$var',''))")
        check_agent_var "Codex (shell)" "$var" "$actual_shell"
    done
fi

# Hermes (plist)
if [ -f "$HERMES_PLIST" ]; then
    for var in $PROPAGATED_VARS; do
        actual=$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$var" "$HERMES_PLIST" 2>/dev/null || echo "")
        check_agent_var "Hermes" "$var" "$actual"
    done
fi

# Cursor (launchctl)
for var in $PROPAGATED_VARS; do
    actual=$(launchctl getenv "$var" 2>/dev/null || echo "")
    check_agent_var "Cursor (launchctl)" "$var" "$actual"
done

if [ $DRIFT -eq 0 ]; then
    echo "  ✓ all agents match ~/.mempalace/env"
else
    echo "  ⚠ drift detected above — re-run this script or manually reconcile"
fi

# Skill drift check — canonical SKILL must match what's deployed in each
# agent's runtime. Codex 5-stub structure is intentionally different and is
# verified in-tree (the .codex-plugin/skills/* files are the canonical
# source for Codex's slash-command stubs, not the main SKILL.md).
echo "  Skill content check:"
CANONICAL_SKILL="$REPO/skills/mempalace/SKILL.md"
CANONICAL_MD5=$(md5 -q "$CANONICAL_SKILL" 2>/dev/null || md5sum "$CANONICAL_SKILL" | awk '{print $1}')
SKILL_DRIFT=0
# Repo-side: .claude-plugin must symlink-resolve to canonical content.
REPO_CLAUDE_SKILL="$REPO/.claude-plugin/skills/mempalace/SKILL.md"
if [ -f "$REPO_CLAUDE_SKILL" ]; then
    actual_md5=$(md5 -q "$REPO_CLAUDE_SKILL" 2>/dev/null || md5sum "$REPO_CLAUDE_SKILL" | awk '{print $1}')
    if [ "$actual_md5" != "$CANONICAL_MD5" ]; then
        echo "    ⚠ .claude-plugin/skills/mempalace/SKILL.md content differs from canonical"
        echo "      (it should be a symlink to ../../../skills/mempalace/SKILL.md)"
        SKILL_DRIFT=1
    fi
fi
# Runtime: Claude Code plugin cache.
if [ -n "${CLAUDE_CACHE:-}" ] && [ -f "$CLAUDE_CACHE/skills/mempalace/SKILL.md" ]; then
    actual_md5=$(md5 -q "$CLAUDE_CACHE/skills/mempalace/SKILL.md" 2>/dev/null || md5sum "$CLAUDE_CACHE/skills/mempalace/SKILL.md" | awk '{print $1}')
    if [ "$actual_md5" != "$CANONICAL_MD5" ]; then
        echo "    ⚠ Claude Code runtime SKILL.md drifted from canonical (CLAUDE_CACHE)"
        SKILL_DRIFT=1
    fi
fi
[ $SKILL_DRIFT -eq 0 ] && echo "    ✓ canonical SKILL.md matches .claude-plugin and Claude Code runtime"

# --- [8/8] Summary ----------------------------------------------------------
echo "[8/8] Summary"
echo "  single-source env: $ENV_FILE"
echo "  propagated to: Claude Code, Codex (2 blocks), Hermes, Cursor (launchctl + plist)"
echo ""
echo "Done. Claude Code / Codex / Cursor need a new session to pick up changes."
echo "Hermes already restarted."
