# Evo IDE V6.1 — Monolithic CoT & Dynamic Search Engine

Target architecture: `huihui_ai/qwen3.5-abliterated:9b` on a local Ollama daemon.
Standard library only — no pip install required for the engine itself.

## Files

| File | Role |
| --- | --- |
| `evo_engine.py` | The whole engine: transport, `<think>` splitter, artifact parser, sandbox writer, patcher, executor, self-heal loop, CLI. |
| `prompts.py` | The system mandate (prompt data, no logic). |
| `evo_config.json` | Optional config; every key is also settable by CLI flag or env var. |

## Setup

```bash
ollama serve
ollama pull huihui_ai/qwen3.5-abliterated:9b

python3 evo_engine.py --selftest    # 27 offline checks, no daemon needed
```

## Usage

```bash
# generate one monolithic artifact
python3 evo_engine.py "build a csv -> sqlite loader with a CLI"

# generate, execute, and self-heal on traceback
python3 evo_engine.py "hackernews top-10 scraper" --run

# ground the model in live docs (Phase 2 SEARCH)
python3 evo_engine.py "use this API" --url https://hn.algolia.com/api

# edit an existing workspace file
python3 evo_engine.py "add retry with backoff" --attach loader.py --diff

# parse + diff without touching disk
python3 evo_engine.py "refactor the parser" --dry-run --diff

# interactive
python3 evo_engine.py --repl        # :mode auto | :think off | :q
```

Exit codes: `0` ok · `1` usage · `3` daemon unreachable · `4` mandate violation · `5` artifact failed to run.

## How the mandate is enforced

The prompt asks; the engine verifies. Model output is untrusted input and is
audited **before** anything is written to disk.

| Directive | Enforcement |
| --- | --- |
| 1 — Cognitive harness | `ReasoningSplitter` is a streaming state machine that survives a tag torn across chunk boundaries (`<th` + `ink>`). A turn with no reasoning is flagged. |
| 2 — Search protocol | `SEARCH:` / `RETRIEVED:` / `UNVERIFIED:` lines are extracted from the trace; unverified assumptions are surfaced in the report instead of silently shipping. |
| 3 — Pipeline | `Agent.turn()` runs the phases in order: fetch context → stream reasoning → audit → synthesize. |
| 4 — Monolithic output | `max_files_per_turn: 1`. A second `File:` block is a hard violation; the engine re-prompts once with `REPAIR_FORMAT_PROMPT` before failing with exit 4. Elision markers (`# ... rest unchanged ...`) are rejected — they would otherwise overwrite a real file with a stub. |
| 5 — Fix format | `apply_fix()` restores indentation the model dropped and replaces the correct `def`/`class` suite rather than a guessed line count. |
| 7 — urllib headers | The engine's own HTTP uses `Request(url, headers=...)` exclusively. |
| 8 — User-controlled exec | `[RUN_CODE]` counts only **outside** a fence; fences are stripped before the check. |

## Two fixes carried over from the previous build

**Live run mode.** The old executor did `from config import RUN_MODE`, which
snapshots the value at import time — so the `/api/run_mode` and `/api/auto_run`
endpoints mutated `config.RUN_MODE` while `run_code()` kept reading the stale
copy, making both endpoints silent no-ops. Here every read goes through
`cfg.run_mode`, and `cfg.auto_run` is a derived `@property` that cannot drift.

**The `<think>` tag.** The prompts previously instructed `<thinking>`, but both
the reasoning splitter and the browser's `stripThink()` key on `<think>`. The
mismatch meant reasoning was never stripped: it rendered in the visible answer
and was fed into the artifact parser.

## Safety

- Writes are jailed to `root`. Absolute paths, `~`, and `..` traversal are rejected.
- Writes are atomic (temp file + `replace`) with timestamped `.bak` backups.
- Execution is opt-in (`run_mode: manual` by default) and timeout-bounded.
- Auto-install skips stdlib names and maps import names to distributions (`bs4` → `beautifulsoup4`).
