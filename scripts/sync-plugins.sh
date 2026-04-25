#!/usr/bin/env bash
# sync-mempalace-plugins.sh — One command to sync all 4 agents' plugin files
# AND propagate env vars from a single source (~/.mempalace/env) into each
# agent's native config format.
#
# Agents covered: Claude Code, Codex, Hermes, Cursor.
# One edit in ~/.mempalace/env → one run → all four agents aligned.
set -euo pipefail

REPO="$HOME/git/mempalace"
HERMES_REPO="$REPO/integrations/hermes"
ENV_FILE="$HOME/.mempalace/env"
ENV_TEMPLATE="$REPO/scripts/mempalace-env.template"

# The 7 vars we propagate. Extra SSL_CERT_FILE handled separately where needed.
PROPAGATED_VARS="MEMPAL_EMBEDDING_MODEL MEMPAL_EMBEDDING_ENDPOINT MEMPAL_EMBEDDING_KEY \
MEMPAL_RECALL_LLM MEMPAL_RECALL_ENDPOINT MEMPAL_RECALL_MODEL MEMPAL_RECALL_KEY"

# Smart copy: preserve local env-var fallback defaults in hook scripts
# (user's override survives a sync).
smart_copy_hook() {
    local src="$1" dst="$2"
    if [ ! -f "$dst" ]; then
        cp "$src" "$dst"
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
    if [ -n "$saved_lines" ]; then
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            local var_name
            var_name=$(echo "$line" | sed 's/export \([A-Z_]*\)=.*/\1/')
            if [ -n "$var_name" ] && grep -q "^export ${var_name}=" "$dst"; then
                sed -i '' "s|^export ${var_name}=.*|${line}|" "$dst"
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
for var in $PROPAGATED_VARS; do
    if [ -z "${!var:-}" ]; then
        MISSING="$MISSING $var"
    fi
done
if [ -n "$MISSING" ]; then
    echo "  ⚠ After sourcing $ENV_FILE, these vars are still empty:$MISSING"
    echo "    Edit $ENV_FILE and re-run. Aborting."
    exit 1
fi
echo "  ✓ 7 MEMPAL_* vars loaded from single source"

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
    CLAUDE_CACHE=$(find "$HOME/.claude/plugins/cache/mempalace/mempalace" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | head -1)
fi
if [ -n "$CLAUDE_CACHE" ] && [ -d "$CLAUDE_CACHE" ]; then
    echo "[2/8] Syncing Claude Code plugin + settings.json env..."
    for f in "$REPO/.claude-plugin/hooks/"mempal-*.sh; do
        [ -f "$f" ] && smart_copy_hook "$f" "$CLAUDE_CACHE/hooks/$(basename "$f")"
    done
    cp "$REPO/.claude-plugin/plugin.json" "$CLAUDE_CACHE/plugin.json" 2>/dev/null && echo "  → plugin.json synced" || true

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

# --- [3/8] Codex: sync plugin + upsert config.toml --------------------------
CODEX_PLUGIN="$HOME/.agents/plugins/mempalace/.codex-plugin"
if [ -d "$CODEX_PLUGIN" ]; then
    echo "[3/8] Syncing Codex plugin + config.toml env..."
    for f in "$REPO/.codex-plugin/hooks/"*.sh; do
        [ -f "$f" ] && smart_copy_hook "$f" "$CODEX_PLUGIN/hooks/$(basename "$f")"
    done
    cp "$REPO/.codex-plugin/plugin.json" "$CODEX_PLUGIN/plugin.json" 2>/dev/null && echo "  → plugin.json synced" || true
    cp "$REPO/.codex-plugin/hooks.json" "$CODEX_PLUGIN/hooks.json" 2>/dev/null && echo "  → hooks.json synced" || true

    # Upsert env vars into ~/.codex/config.toml in BOTH required locations:
    # [mcp_servers.mempalace].env and [shell_environment_policy.set]
    CODEX_CONFIG="$HOME/.codex/config.toml"
    if [ -f "$CODEX_CONFIG" ]; then
        python3 - <<PYEOF
import os, re
path = "$CODEX_CONFIG"
with open(path) as f:
    content = f.read()
vars_to_set = "$PROPAGATED_VARS".split() + ["SSL_CERT_FILE"]
env_vals = {v: os.environ.get(v, "") for v in vars_to_set}

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
    fi
else
    echo "[3/8] Codex plugin dir not found, skipping"
fi

# --- [4/8] Hermes: sync plugin + upsert launchd plist env -------------------
HERMES_RUNTIME="$HOME/.hermes/hermes-agent/plugins/memory/mempalace"
HERMES_PLIST="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"
if [ -d "$HERMES_RUNTIME" ] && [ -f "$HERMES_REPO/plugins/memory/mempalace/__init__.py" ]; then
    echo "[4/8] Syncing Hermes plugin + plist env..."
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
            val="${!var}"
            # PlistBuddy's Add fails if key exists, Set fails if key doesn't.
            # Try Set first, fall back to Add.
            /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:$var $val" "$HERMES_PLIST" 2>/dev/null \
                || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:$var string $val" "$HERMES_PLIST" 2>/dev/null || true
        done
        echo "  → 7 env vars upserted in launchd plist"
    fi
else
    echo "[4/8] Hermes plugin dir not found, skipping"
fi

# --- [5/8] Cursor: symlink plugin + launchctl setenv + LaunchAgent plist ----
CURSOR_PLUGIN="$HOME/.cursor/plugins/local/mempalace"
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

# Push env vars into the macOS GUI session so Cursor (launched via
# LaunchServices, which does NOT source ~/.zshrc) sees them.
for var in $PROPAGATED_VARS; do
    launchctl setenv "$var" "${!var}"
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

# --- [6/8] Restart Hermes if running ----------------------------------------
echo "[6/8] Restarting Hermes gateway..."
if pgrep -f "hermes_cli.main gateway" >/dev/null 2>&1; then
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
    local expected="${!var}"
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

# --- [8/8] Summary ----------------------------------------------------------
echo "[8/8] Summary"
echo "  single-source env: $ENV_FILE"
echo "  propagated to: Claude Code, Codex (2 blocks), Hermes, Cursor (launchctl + plist)"
echo ""
echo "Done. Claude Code / Codex / Cursor need a new session to pick up changes."
echo "Hermes already restarted."
