#!/usr/bin/env python3
"""
MemPalace — Give your AI a memory. No API key required.

Two ways to ingest:
  Projects:      mempalace mine ~/projects/my_app          (code, docs, notes)
  Conversations: mempalace mine <convo-dir> --mode convos     (Claude Code, Claude.ai, ChatGPT, Slack exports)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init <dir>                  Detect rooms from folder structure
    mempalace split <dir>                 Split concatenated mega-files into per-session files
    mempalace mine <dir>                  Mine project files (default)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace search "query"              Find anything, exact words
    mempalace mcp                         Show MCP setup command
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/.claude/projects/-Users-you-Projects-my_app --mode convos --wing my_app
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

import os
import sys
import shlex
import argparse
from pathlib import Path

from .config import MempalaceConfig
from .hooks_cli import SUPPORTED_HARNESSES
from .version import __version__


_MEMPALACE_PROJECT_FILES = ("mempalace.yaml", "entities.json")


def _ensure_mempalace_files_gitignored(project_dir) -> bool:
    """If project_dir is a git repo, ensure MemPalace's per-project files
    are listed in .gitignore so they don't get committed by accident.

    Returns True if .gitignore was updated, False otherwise. Issue #185:
    `mempalace init` writes mempalace.yaml + entities.json into the
    project root, where they previously had no protection against being
    staged into git.
    """
    from pathlib import Path

    project_path = Path(project_dir).expanduser().resolve()
    if not (project_path / ".git").exists():
        return False
    gitignore = project_path / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [p for p in _MEMPALACE_PROJECT_FILES if p not in existing_lines]
    if not missing:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = prefix + "\n# MemPalace per-project files (issue #185)\n" + "\n".join(missing) + "\n"
    with open(gitignore, "a") as f:
        f.write(block)
    print(f"  Added {', '.join(missing)} to {gitignore.name}")
    return True


def cmd_init(args):
    import json
    from pathlib import Path
    from .entity_detector import confirm_entities
    from .project_scanner import discover_entities
    from .room_detector_local import detect_rooms_local

    cfg = MempalaceConfig()

    # Resolve entity-detection languages: --lang overrides config.
    lang_arg = getattr(args, "lang", None)
    if lang_arg:
        languages = [s.strip() for s in lang_arg.split(",") if s.strip()] or ["en"]
        cfg.set_entity_languages(languages)
    else:
        languages = cfg.entity_languages
    languages_tuple = tuple(languages)

    # Optional phase-2 LLM provider (opt-in via --llm).
    llm_provider = None
    if getattr(args, "llm", False):
        from .llm_client import LLMError, get_provider

        try:
            llm_provider = get_provider(
                name=args.llm_provider,
                model=args.llm_model,
                endpoint=args.llm_endpoint,
                api_key=args.llm_api_key,
            )
        except LLMError as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        ok, msg = llm_provider.check_available()
        if not ok:
            print(
                f"  ERROR: LLM provider '{args.llm_provider}' unavailable: {msg}",
                file=sys.stderr,
            )
            sys.exit(2)
        print(f"  LLM refinement enabled: {args.llm_provider}/{args.llm_model}")

    # Pass 1: discover entities — manifests + git authors first, prose detection
    # as supplement for names mentioned only in docs/notes. Optional phase-2
    # LLM refinement runs inside discover_entities when llm_provider is given.
    print(f"\n  Scanning for entities in: {args.dir}")
    if languages_tuple != ("en",):
        print(f"  Languages: {', '.join(languages_tuple)}")
    detected = discover_entities(args.dir, languages=languages_tuple, llm_provider=llm_provider)
    total = len(detected["people"]) + len(detected["projects"]) + len(detected["uncertain"])
    if total > 0:
        confirmed = confirm_entities(detected, yes=getattr(args, "yes", False))
        # Save confirmed entities to <project>/entities.json (per-project
        # audit trail — user can inspect or hand-edit) AND merge into the
        # global registry the miner reads at mine time.
        if confirmed["people"] or confirmed["projects"]:
            entities_path = Path(args.dir).expanduser().resolve() / "entities.json"
            with open(entities_path, "w", encoding="utf-8") as f:
                json.dump(confirmed, f, indent=2, ensure_ascii=False)
            print(f"  Entities saved: {entities_path}")

            from .miner import add_to_known_entities

            registry_path = add_to_known_entities(confirmed)
            print(f"  Registry updated: {registry_path}")
    else:
        print("  No entities detected — proceeding with directory-based rooms.")

    # Pass 2: detect rooms from folder structure
    detect_rooms_local(project_dir=args.dir, yes=getattr(args, "yes", False))
    cfg.init()

    # Pass 3: protect git repos from accidentally committing per-project files
    _ensure_mempalace_files_gitignored(args.dir)


def cmd_mine(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    include_ignored = []
    for raw in args.include_ignored or []:
        include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())

    if args.mode == "convos":
        from .convo_miner import mine_convos

        mine_convos(
            convo_dir=args.dir,
            palace_path=palace_path,
            wing=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            extract_mode=args.extract,
        )
    else:
        from .miner import mine

        mine(
            project_dir=args.dir,
            palace_path=palace_path,
            wing_override=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            respect_gitignore=not args.no_gitignore,
            include_ignored=include_ignored,
        )


def cmd_sweep(args):
    """Sweep a transcript file or directory.

    The sweeper deduplicates against its own prior writes via
    deterministic drawer IDs + a timestamp cursor. It does NOT currently
    coordinate with the file-level miners (miner.py / convo_miner.py) —
    those produce char-chunked drawers without compatible message
    metadata, so running both miners may store overlapping content under
    different IDs.
    """
    from .sweeper import sweep, sweep_directory

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    target = os.path.expanduser(args.target)

    if os.path.isfile(target):
        result = sweep(target, palace_path)
        print(
            f"  Swept {target}: +{result['drawers_added']} new, "
            f"{result['drawers_already_present']} already present, "
            f"{result['drawers_skipped']} skipped (< cursor)."
        )
    elif os.path.isdir(target):
        result = sweep_directory(target, palace_path)
        print(
            f"  Swept {result['files_succeeded']}/{result['files_attempted']} "
            f"files from {target}: +{result['drawers_added']} new, "
            f"{result['drawers_already_present']} already present, "
            f"{result['drawers_skipped']} skipped (< cursor)."
        )
        failures = result.get("failures") or []
        if failures:
            print(
                f"  WARNING: {len(failures)} file(s) failed to sweep - see stderr / logs for details.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        print(f"  ERROR: Not a file or directory: {target}", file=sys.stderr)
        sys.exit(1)


def cmd_search(args):
    from .searcher import search, SearchError

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    try:
        search(
            query=args.query,
            palace_path=palace_path,
            wing=args.wing,
            room=args.room,
            n_results=args.results,
        )
    except SearchError:
        sys.exit(1)


def cmd_wakeup(args):
    """Show L0 (identity) + L1 (essential story) — the wake-up context."""
    from .layers import MemoryStack

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    stack = MemoryStack(palace_path=palace_path)

    text = stack.wake_up(wing=args.wing)
    tokens = len(text) // 4
    print(f"Wake-up text (~{tokens} tokens):")
    print("=" * 50)
    print(text)


def cmd_split(args):
    """Split concatenated transcript mega-files into per-session files."""
    from .split_mega_files import main as split_main
    import sys

    # Rebuild argv for split_mega_files argparse
    # Expand ~ and resolve to absolute path so split_mega_files sees a real path
    argv = ["--source", str(Path(args.dir).expanduser().resolve())]
    if args.output_dir:
        argv += ["--output-dir", args.output_dir]
    if args.dry_run:
        argv.append("--dry-run")
    if args.min_sessions != 2:
        argv += ["--min-sessions", str(args.min_sessions)]

    old_argv = sys.argv
    sys.argv = ["mempalace split"] + argv
    try:
        split_main()
    finally:
        sys.argv = old_argv


def cmd_migrate(args):
    """Migrate palace from a different ChromaDB version."""
    from .migrate import migrate

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    migrate(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


def cmd_status(args):
    from .miner import status

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    status(palace_path=palace_path)


def cmd_doctor(args):
    """Run non-destructive palace health diagnostics.

    Exit codes:
        0 = ok
        1 = warn (operational issues, not data loss)
        2 = corrupt (data integrity issues — repair recommended)
    """
    from .health import check_palace_health

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    if not os.path.isdir(palace_path):
        print(f"\n  No palace directory at {palace_path}", file=sys.stderr)
        sys.exit(2)

    try:
        report = check_palace_health(palace_path)
    except ValueError as exc:
        # Programmer-error path — palace path doesn't exist (we already
        # guarded above, so this is highly unusual). Keep the same exit
        # code shape as a corrupt palace so wrappers don't have to
        # special-case it.
        print(f"\n  ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"\n{'=' * 55}")
    print("  MemPalace Doctor")
    print(f"{'=' * 55}\n")
    print(f"  Palace:                  {report.palace_path}")
    print(f"  Status:                  {report.status.upper()}")
    print(f"  SQLite integrity ok:     {report.sqlite_integrity_ok}")
    print(f"  Drawers (sqlite):        {report.drawer_count_sqlite}")
    print(f"  Drawers (verbatim):      {report.drawer_count_verbatim}")
    print(f"  Drawers (HNSW):          {report.drawer_count_hnsw}")
    print(f"  Checked at:              {report.checked_at.isoformat(timespec='seconds')}")

    if report.issues:
        print("\n  Issues:")
        for issue in report.issues:
            print(f"    [{issue.severity.upper()}] {issue.code}: {issue.message}")
    else:
        print("\n  No issues detected.")

    if report.status == "corrupt":
        print("\n  Recommended: mempalace repair --rebuild-from-verbatim")
        sys.exit(2)
    if report.status == "warn":
        sys.exit(1)
    sys.exit(0)


def cmd_repair(args):
    """Rebuild palace vector index from SQLite metadata.

    With ``--rebuild-from-verbatim`` (Layer 4): quarantines the existing
    HNSW segments, then rebuilds the index from the verbatim text stored
    in ``chroma.sqlite3``. Use this when the palace is too damaged for
    the standard ``repair`` path (which still requires ChromaDB to open
    cleanly) to complete.
    """
    if getattr(args, "rebuild_from_verbatim", False):
        return _cmd_repair_rebuild_from_verbatim(args)

    import shutil
    from .backends.chroma import ChromaBackend
    from .embedding import get_embedding_function
    from .migrate import confirm_destructive_action, contains_palace_database

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )
    db_path = os.path.join(palace_path, "chroma.sqlite3")

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return
    if not contains_palace_database(palace_path):
        print(f"\n  No palace database found at {db_path}")
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    backend = ChromaBackend()

    # Try to read existing drawers
    ef = get_embedding_function()
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers", embedding_function=ef)
        total = col.count()
        print(f"  Drawers found: {total}")
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print("  Cannot recover — palace may need to be re-mined from source files.")
        return

    if total == 0:
        print("  Nothing to repair.")
        return

    if not confirm_destructive_action(
        "Repair", palace_path, assume_yes=getattr(args, "yes", False)
    ):
        return

    # Extract all drawers in batches
    print("\n  Extracting drawers...")
    batch_size = 5000
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0
    while offset < total:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        all_ids.extend(batch["ids"])
        all_docs.extend(batch["documents"])
        all_metas.extend(batch["metadatas"])
        offset += batch_size
    print(f"  Extracted {len(all_ids)} drawers")

    # Backup and rebuild
    palace_path = os.path.normpath(palace_path)
    backup_path = palace_path + ".backup"
    if os.path.exists(backup_path):
        if not contains_palace_database(backup_path):
            print(
                "  Backup validation failed: backup path exists but does not contain chroma.sqlite3. "
                f"Please remove or rename: {backup_path}"
            )
            return
        shutil.rmtree(backup_path)
    print(f"  Backing up to {backup_path}...")
    shutil.copytree(palace_path, backup_path)

    print("  Rebuilding collection...")
    backend.delete_collection(palace_path, "mempalace_drawers")
    new_col = backend.create_collection(palace_path, "mempalace_drawers", embedding_function=ef)

    filed = 0
    for i in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[i : i + batch_size]
        batch_docs = all_docs[i : i + batch_size]
        batch_metas = all_metas[i : i + batch_size]
        new_col.add(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
        filed += len(batch_ids)
        print(f"  Re-filed {filed}/{len(all_ids)} drawers...")

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print(f"  Backup saved at {backup_path}")
    print(f"\n{'=' * 55}\n")


def _cmd_repair_rebuild_from_verbatim(args):
    """Implementation for ``mempalace repair --rebuild-from-verbatim``.

    Quarantines HNSW segments, then re-ingests every drawer from the
    verbatim text stored in chroma.sqlite3. Always non-interactive
    (the verbatim text is preserved end-to-end so this does not
    destroy data — the existing HNSW segments are quarantined, not
    deleted, so a forensic copy survives in
    ``~/.mempalace/quarantine/`` if anything goes wrong).
    """
    from .repair import rebuild_from_verbatim

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    if not os.path.isdir(palace_path):
        print(f"\n  No palace directory at {palace_path}", file=sys.stderr)
        sys.exit(2)
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        print(
            f"\n  No chroma.sqlite3 at {db_path}; nothing to rebuild from",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — Rebuild from Verbatim")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")
    print("  Step 1: quarantining HNSW segment files...")

    last_progress = {"phase": ""}

    def _progress(phase: str, processed: int, total: int) -> None:
        # Throttle: only print when the phase changes or every 500 drawers.
        if phase != last_progress["phase"]:
            if phase == "writing":
                print(f"  Step 2: re-ingesting {total} drawers...")
            last_progress["phase"] = phase
        if phase == "writing" and total and (processed % 500 == 0 or processed == total):
            print(f"    {processed}/{total} drawers re-filed")
        if phase == "done":
            print(f"  Step 3: rebuild complete ({processed} drawers).")

    try:
        report = rebuild_from_verbatim(palace_path, progress_cb=_progress)
    except Exception as exc:  # pragma: no cover - defensive top-level CLI guard
        print(f"\n  ERROR: rebuild_from_verbatim failed: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"\n  Drawers processed: {report.drawers_processed}")
    if report.drawers_failed:
        print(f"  Drawers failed:    {report.drawers_failed}")
        for path, err in report.failures[:5]:
            print(f"    - {path}: {err}")
        if len(report.failures) > 5:
            print(f"    ... and {len(report.failures) - 5} more")
    if report.quarantine_path is not None:
        print(f"  Quarantine path:   {report.quarantine_path}")
    print(f"  Duration:          {report.duration_seconds:.1f}s")
    print(f"\n{'=' * 55}\n")
    sys.exit(0)


def cmd_drain_recovery(args):
    """Replay orphaned recovery WAL files into the palace.

    Background: when ``_async_save_worker`` (the stop-hook's background
    writer) cannot acquire ``palace_write_lock`` within its budget, it
    persists the unwritten payload to ``~/.mempalace/recovery/<id>/``
    instead of dropping it on the floor. Those files are normally
    drained automatically on the next successful save, but operators
    can replay them manually here — useful when the next save is far
    in the future or when debugging stuck writers.

    Exit codes:
        0 = success (even if some files failed — partial drain is success)
        2 = palace path does not exist
    """
    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    if not os.path.isdir(palace_path):
        print(f"\n  No palace directory at {palace_path}", file=sys.stderr)
        sys.exit(2)

    from .recovery_wal import drain_recovery_wal, list_pending, recovery_dir_for_palace

    pending = list_pending(palace_path)

    print(f"\n{'=' * 55}")
    print("  MemPalace Recovery Drain")
    print(f"{'=' * 55}\n")
    print(f"  Palace:          {palace_path}")
    print(f"  Recovery dir:    {recovery_dir_for_palace(palace_path)}")
    print(f"  Pending files:   {len(pending)}")

    if not pending:
        print("\n  Nothing to drain.")
        sys.exit(0)

    if args.dry_run:
        print("\n  DRY RUN — files would be replayed in this order:")
        import json as _json

        for p in pending:
            try:
                line_count = sum(
                    1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip()
                )
            except OSError as exc:
                line_count = -1
                print(f"    {p.name}  (cannot read: {exc})")
                continue
            print(f"    {p.name}  ({line_count} record(s))")
            # Show op breakdown so operators can sanity-check before draining.
            try:
                ops: dict[str, int] = {}
                for raw in p.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = _json.loads(raw)
                    except _json.JSONDecodeError:
                        ops["<malformed>"] = ops.get("<malformed>", 0) + 1
                        continue
                    op = rec.get("op", "<missing op>")
                    ops[op] = ops.get(op, 0) + 1
                if ops:
                    parts = ", ".join(f"{k}={v}" for k, v in sorted(ops.items()))
                    print(f"      ops: {parts}")
            except OSError:
                pass
        print("\n  (dry run — nothing applied)")
        sys.exit(0)

    # Real drain.
    from .palace import PalaceWriteLockTimeout, get_collection, palace_write_lock
    from .hooks_cli import _replay_recovery_records

    print("\n  Draining...")
    try:
        with palace_write_lock(palace_path, timeout=30.0):
            col = get_collection(palace_path, create=True)
            try:
                col.refresh_for_write()
            except AttributeError:
                pass
            from datetime import datetime as _dt

            now = _dt.now()
            result = drain_recovery_wal(
                palace_path,
                apply_records=lambda recs: _replay_recovery_records(recs, col, now),
            )
    except PalaceWriteLockTimeout:
        print(
            "  ERROR: could not acquire palace_write_lock within 30s. "
            "Another writer is busy — try again shortly.",
            file=sys.stderr,
        )
        sys.exit(0)  # ops command — busy palace is not a hard failure

    print(f"  Files processed: {result.files_processed}")
    print(f"  Files failed:    {result.files_failed}")
    print(f"  Records:         {result.records_replayed}")
    print(f"  Duration:        {result.duration_seconds:.2f}s")

    if result.failures:
        print("\n  Failures (file left on disk for retry):")
        for path, err in result.failures[:10]:
            print(f"    {path.name}: {err}")
        if len(result.failures) > 10:
            print(f"    ... and {len(result.failures) - 10} more")

    print(f"\n{'=' * 55}\n")
    sys.exit(0)


def cmd_hook(args):
    """Run hook logic: reads JSON from stdin, outputs JSON to stdout."""
    from .hooks_cli import run_hook

    run_hook(hook_name=args.hook, harness=args.harness)


def cmd_instructions(args):
    """Output skill instructions to stdout."""
    from .instructions_cli import run_instructions

    run_instructions(name=args.name)


def cmd_singleton_install(args):
    from .singleton_manager import cmd_install

    cmd_install(args)



def cmd_singleton_start(args):
    from .singleton_manager import cmd_start

    cmd_start(args)



def cmd_singleton_stop(args):
    from .singleton_manager import cmd_stop

    cmd_stop(args)



def cmd_singleton_status(args):
    from .singleton_manager import cmd_status

    cmd_status(args)



def cmd_singleton_uninstall(args):
    from .singleton_manager import cmd_uninstall

    cmd_uninstall(args)


def cmd_update(args):
    """Pull latest code and sync plugins to installed agents."""
    from .updater import check, update

    if args.check:
        check()
        return
    agents = [a.strip() for a in args.agents.split(",") if a.strip()] if args.agents else None
    update(agents=agents, tag=args.tag or None, pull=not args.no_pull)



def cmd_mcp(args):
    """Show how to wire MemPalace into MCP-capable hosts."""
    base_server_cmd = "mempalace-mcp-bridge"
    direct_server_cmd = "mempalace-mcp"

    if args.palace:
        resolved_palace = str(Path(args.palace).expanduser())
        server_cmd = f"{base_server_cmd} --palace {shlex.quote(resolved_palace)}"
        direct_cmd = f"{direct_server_cmd} --palace {shlex.quote(resolved_palace)}"
    else:
        server_cmd = base_server_cmd
        direct_cmd = direct_server_cmd

    print("MemPalace MCP quick setup (singleton-preferred):")
    print(f"  claude mcp add mempalace -- {server_cmd}")
    print("\nDefault architecture:")
    print("  1. Start one local singleton: mempalace singleton install --start")
    print("  2. Point agents at mempalace-mcp-bridge")
    print("  3. Bridge talks to ~/.mempalace/mcp.sock when available")
    print("\nPer-agent fallback (no singleton):")
    print(f"  claude mcp add mempalace -- {direct_cmd}")
    print(f"  {direct_cmd}")

    if not args.palace:
        print("\nOptional custom palace:")
        print(f"  claude mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print(f"  {base_server_cmd} --palace /path/to/palace")


def cmd_compress(args):
    """Compress drawers in a wing using AAAK Dialect."""
    from .backends.chroma import ChromaBackend
    from .dialect import Dialect
    from .embedding import get_embedding_function

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    # Load dialect (with optional entity config)
    config_path = args.config
    if not config_path:
        for candidate in ["entities.json", os.path.join(palace_path, "entities.json")]:
            if os.path.exists(candidate):
                config_path = candidate
                break

    if config_path and os.path.exists(config_path):
        dialect = Dialect.from_config(config_path)
        print(f"  Loaded entity config: {config_path}")
    else:
        dialect = Dialect()

    # Connect to palace
    backend = ChromaBackend()
    ef = get_embedding_function()
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers", embedding_function=ef)
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        sys.exit(1)

    # Query drawers in batches to avoid SQLite variable limit (~999)
    where = {"wing": args.wing} if args.wing else None
    _BATCH = 500
    docs, metas, ids = [], [], []
    offset = 0
    while True:
        try:
            kwargs = {
                "include": ["documents", "metadatas"],
                "limit": _BATCH,
                "offset": offset,
            }
            if where:
                kwargs["where"] = where
            batch = col.get(**kwargs)
        except Exception as e:
            if not docs:
                print(f"\n  Error reading drawers: {e}")
                sys.exit(1)
            break
        batch_docs = batch.get("documents", [])
        if not batch_docs:
            break
        docs.extend(batch_docs)
        metas.extend(batch.get("metadatas", []))
        ids.extend(batch.get("ids", []))
        offset += len(batch_docs)
        if len(batch_docs) < _BATCH:
            break

    if not docs:
        wing_label = f" in wing '{args.wing}'" if args.wing else ""
        print(f"\n  No drawers found{wing_label}.")
        return

    print(
        f"\n  Compressing {len(docs)} drawers"
        + (f" in wing '{args.wing}'" if args.wing else "")
        + "..."
    )
    print()

    total_original = 0
    total_compressed = 0
    compressed_entries = []

    for doc, meta, doc_id in zip(docs, metas, ids):
        compressed = dialect.compress(doc, metadata=meta)
        stats = dialect.compression_stats(doc, compressed)

        total_original += stats["original_chars"]
        total_compressed += stats["summary_chars"]

        compressed_entries.append((doc_id, compressed, meta, stats))

        if args.dry_run:
            wing_name = meta.get("wing", "?")
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "?")).name
            print(f"  [{wing_name}/{room_name}] {source}")
            print(
                f"    {stats['original_tokens_est']}t -> {stats['summary_tokens_est']}t ({stats['size_ratio']:.1f}x)"
            )
            print(f"    {compressed}")
            print()

    # Store compressed versions (unless dry-run)
    if not args.dry_run:
        try:
            comp_col = backend.get_or_create_collection(
                palace_path, "mempalace_compressed", embedding_function=ef
            )
            for doc_id, compressed, meta, stats in compressed_entries:
                comp_meta = dict(meta)
                comp_meta["compression_ratio"] = round(stats["size_ratio"], 1)
                comp_meta["original_tokens"] = stats["original_tokens_est"]
                comp_col.upsert(
                    ids=[doc_id],
                    documents=[compressed],
                    metadatas=[comp_meta],
                )
            print(
                f"  Stored {len(compressed_entries)} compressed drawers in 'mempalace_compressed' collection."
            )
        except Exception as e:
            print(f"  Error storing compressed drawers: {e}")
            sys.exit(1)

    # Summary
    ratio = total_original / max(total_compressed, 1)
    # Estimate tokens from char count (~3.8 chars/token for English text)
    orig_tokens = max(1, int(total_original / 3.8))
    comp_tokens = max(1, int(total_compressed / 3.8))
    print(f"  Total: {orig_tokens:,}t -> {comp_tokens:,}t ({ratio:.1f}x compression)")
    if args.dry_run:
        print("  (dry run -- nothing stored)")


def main():
    version_label = f"MemPalace {__version__}"
    parser = argparse.ArgumentParser(
        description="MemPalace — Give your AI a memory. No API key required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{version_label}\n\n{__doc__}",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=version_label,
        help="Show version and exit",
    )
    parser.add_argument(
        "--palace",
        default=None,
        help="Where the palace lives (default: from ~/.mempalace/config.json or ~/.mempalace/palace)",
    )

    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Detect rooms from your folder structure")
    p_init.add_argument("dir", help="Project directory to set up")
    p_init.add_argument(
        "--yes",
        action="store_true",
        help="Auto-accept all detected entities (non-interactive)",
    )
    p_init.add_argument(
        "--lang",
        default=None,
        help=(
            "Comma-separated language codes for entity detection "
            "(e.g. 'en' or 'en,pt-br'). Defaults to value from config "
            "(MEMPALACE_ENTITY_LANGUAGES env var or config.json), or 'en'. "
            "When given, the value is also persisted to config.json."
        ),
    )
    p_init.add_argument(
        "--llm",
        action="store_true",
        help=(
            "Enable LLM-assisted entity refinement (opt-in, local-first). "
            "Runs after manifest/git/regex detection, asking the configured "
            "provider to reclassify ambiguous candidates. "
            "Ctrl-C during refinement returns partial results."
        ),
    )
    p_init.add_argument(
        "--llm-provider",
        default="ollama",
        choices=["ollama", "openai-compat", "anthropic"],
        help="LLM provider (default: ollama). Use --llm to enable.",
    )
    p_init.add_argument(
        "--llm-model",
        default="gemma4:e4b",
        help="Model name for the chosen provider (default: gemma4:e4b for Ollama).",
    )
    p_init.add_argument(
        "--llm-endpoint",
        default=None,
        help=(
            "Provider endpoint URL. Default for Ollama: http://localhost:11434. "
            "Required for openai-compat."
        ),
    )
    p_init.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "API key for the provider. For anthropic, defaults to $ANTHROPIC_API_KEY; "
            "for openai-compat, defaults to $OPENAI_API_KEY."
        ),
    )

    # mine
    p_mine = sub.add_parser("mine", help="Mine files into the palace")
    p_mine.add_argument("dir", help="Directory to mine")
    p_mine.add_argument(
        "--mode",
        choices=["projects", "convos"],
        default="projects",
        help="Ingest mode: 'projects' for code/docs (default), 'convos' for chat exports",
    )
    p_mine.add_argument("--wing", default=None, help="Wing name (default: directory name)")
    p_mine.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Don't respect .gitignore files when scanning project files",
    )
    p_mine.add_argument(
        "--include-ignored",
        action="append",
        default=[],
        help="Always scan these project-relative paths even if ignored; repeat or pass comma-separated paths",
    )
    p_mine.add_argument(
        "--agent",
        default="mempalace",
        help="Your name — recorded on every drawer (default: mempalace)",
    )
    p_mine.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    p_mine.add_argument(
        "--dry-run", action="store_true", help="Show what would be filed without filing"
    )
    p_mine.add_argument(
        "--extract",
        choices=["exchange", "general"],
        default="exchange",
        help="Extraction strategy for convos mode: 'exchange' (default) or 'general' (5 memory types)",
    )

    # sweep
    p_sweep = sub.add_parser(
        "sweep",
        help="Tandem miner: catch anything the primary miner missed "
        "(message-level, timestamp-coordinated, idempotent)",
    )
    p_sweep.add_argument(
        "target",
        help="A .jsonl transcript file, or a directory to scan recursively",
    )

    # search
    p_search = sub.add_parser("search", help="Find anything, exact words")
    p_search.add_argument("query", help="What to search for")
    p_search.add_argument("--wing", default=None, help="Limit to one project")
    p_search.add_argument("--room", default=None, help="Limit to one room")
    p_search.add_argument("--results", type=int, default=5, help="Number of results")

    # compress
    p_compress = sub.add_parser(
        "compress", help="Compress drawers using AAAK Dialect (~30x reduction)"
    )
    p_compress.add_argument("--wing", default=None, help="Wing to compress (default: all wings)")
    p_compress.add_argument(
        "--dry-run", action="store_true", help="Preview compression without storing"
    )
    p_compress.add_argument(
        "--config", default=None, help="Entity config JSON (e.g. entities.json)"
    )

    # wake-up
    p_wakeup = sub.add_parser("wake-up", help="Show L0 + L1 wake-up context (~600-900 tokens)")
    p_wakeup.add_argument("--wing", default=None, help="Wake-up for a specific project/wing")

    # split
    p_split = sub.add_parser(
        "split",
        help="Split concatenated transcript mega-files into per-session files (run before mine)",
    )
    p_split.add_argument("dir", help="Directory containing transcript files")
    p_split.add_argument(
        "--output-dir",
        default=None,
        help="Write split files here (default: same directory as source files)",
    )
    p_split.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be split without writing files",
    )
    p_split.add_argument(
        "--min-sessions",
        type=int,
        default=2,
        help="Only split files containing at least N sessions (default: 2)",
    )

    # hook
    p_hook = sub.add_parser(
        "hook",
        help="Run hook logic (reads JSON from stdin, outputs JSON to stdout)",
    )
    hook_sub = p_hook.add_subparsers(dest="hook_action")
    p_hook_run = hook_sub.add_parser("run", help="Execute a hook")
    p_hook_run.add_argument(
        "--hook",
        required=True,
        choices=["session-start", "stop", "precompact", "userprompt"],
        help="Hook name to run",
    )
    p_hook_run.add_argument(
        "--harness",
        required=True,
        choices=sorted(SUPPORTED_HARNESSES),
        help="Harness type (determines stdin JSON format)",
    )

    # instructions
    p_instructions = sub.add_parser(
        "instructions",
        help="Output skill instructions to stdout",
    )
    instructions_sub = p_instructions.add_subparsers(dest="instructions_name")
    for instr_name in ["init", "search", "mine", "help", "status"]:
        instructions_sub.add_parser(instr_name, help=f"Output {instr_name} instructions")

    # repair
    p_repair = sub.add_parser(
        "repair",
        help="Rebuild palace vector index from stored data (fixes segfaults after corruption)",
    )
    p_repair.add_argument(
        "--yes", action="store_true", help="Skip confirmation for destructive changes"
    )
    p_repair.add_argument(
        "--rebuild-from-verbatim",
        action="store_true",
        help=(
            "Quarantine the existing HNSW segments and rebuild the index from "
            "the verbatim text in chroma.sqlite3 (Layer 4 recovery). Use when "
            "the standard repair path can't open the palace. Quarantined "
            "segments are MOVED, not deleted, so a forensic copy survives."
        ),
    )

    # doctor
    sub.add_parser(
        "doctor",
        help=("Run non-destructive palace health diagnostics. Exit 0 ok, 1 warn, 2 corrupt."),
    )

    # singleton manager
    p_singleton = sub.add_parser(
        "singleton",
        help="Manage the shared local MemPalace MCP singleton (launchd on macOS, systemd --user on Linux)",
    )
    singleton_sub = p_singleton.add_subparsers(dest="singleton_action")
    p_singleton_install = singleton_sub.add_parser(
        "install",
        help="Install the platform-specific singleton service definition",
    )
    p_singleton_install.add_argument(
        "--start",
        action="store_true",
        help="Start the singleton immediately after installing the service definition",
    )
    singleton_sub.add_parser("start", help="Start (or restart) the singleton service")
    singleton_sub.add_parser("stop", help="Stop the singleton service")
    singleton_sub.add_parser("status", help="Show singleton service + socket status")
    singleton_sub.add_parser("uninstall", help="Remove the singleton service definition")

    # mcp
    sub.add_parser(
        "mcp",
        help="Show MCP setup command for connecting MemPalace to your AI client",
    )

    # status
    # migrate
    p_migrate = sub.add_parser(
        "migrate",
        help="Migrate palace from a different ChromaDB version (fixes 3.0.0 → 3.1.0 upgrade)",
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without changing anything",
    )
    p_migrate.add_argument(
        "--yes", action="store_true", help="Skip confirmation for destructive changes"
    )

    sub.add_parser("status", help="Show what's been filed")

    # update
    p_update = sub.add_parser(
        "update",
        help="Pull latest code and sync plugins to all installed agents",
    )
    p_update.add_argument(
        "--check",
        action="store_true",
        help="Show what would change without touching anything",
    )
    p_update.add_argument(
        "--agents",
        type=str,
        default="",
        help="Comma-separated agent list (claude,codex,hermes,cursor). Default: auto-detect",
    )
    p_update.add_argument(
        "--tag",
        type=str,
        default="",
        help="Check out a specific tag instead of pulling the current branch",
    )
    p_update.add_argument(
        "--no-pull",
        action="store_true",
        help="Skip git pull, only re-run install.sh (which then reinstalls + syncs plugins)",
    )

    # drain-recovery
    p_drain = sub.add_parser(
        "drain-recovery",
        help=(
            "Replay orphaned recovery WAL files (from async_save_worker "
            "lock timeouts) into the palace"
        ),
    )
    p_drain.add_argument(
        "--dry-run",
        action="store_true",
        help="List pending files and record counts without applying",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Handle two-level subcommands
    if args.command == "hook":
        if not getattr(args, "hook_action", None):
            p_hook.print_help()
            return
        cmd_hook(args)
        return

    if args.command == "instructions":
        name = getattr(args, "instructions_name", None)
        if not name:
            p_instructions.print_help()
            return
        args.name = name
        cmd_instructions(args)
        return

    if args.command == "singleton":
        action = getattr(args, "singleton_action", None)
        singleton_dispatch = {
            "install": cmd_singleton_install,
            "start": cmd_singleton_start,
            "stop": cmd_singleton_stop,
            "status": cmd_singleton_status,
            "uninstall": cmd_singleton_uninstall,
        }
        if action not in singleton_dispatch:
            p_singleton.print_help()
            return
        singleton_dispatch[action](args)
        return

    dispatch = {
        "init": cmd_init,
        "mine": cmd_mine,
        "split": cmd_split,
        "search": cmd_search,
        "sweep": cmd_sweep,
        "mcp": cmd_mcp,
        "compress": cmd_compress,
        "wake-up": cmd_wakeup,
        "repair": cmd_repair,
        "doctor": cmd_doctor,
        "migrate": cmd_migrate,
        "status": cmd_status,
        "update": cmd_update,
        "drain-recovery": cmd_drain_recovery,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
