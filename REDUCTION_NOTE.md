# Evo IDE — Reduced Build

## What was removed (10 files)

| File | Why removed |
|------|-------------|
| `evo_engine.py` | Monolithic V6.1 rewrite that duplicates the entire modular stack already present in `agent_orchestrator.py` + `ollama_client.py` + `context_manager.py` + `stream_handler.py` + `backend_utils.py`. Keeping both stacks means maintaining two engines that do the same job. |
| `gui_bridge.py` | Only consumed by the Next.js TypeScript bridge (`sandbox-bridge.ts`). With the TS frontend removed, this bridge has no caller. |
| `live_sandbox.py` | Imported exclusively by `gui_bridge.py`. The HTTP server stack uses `backend_utils.run_code()` instead; `live_sandbox.py` is an unused drop-in replacement. |
| `mac_terminal_exec.py` | macOS-only visible-terminal helper. Never imported by the server stack. The sandboxed execution in `backend_utils.py` covers the same need cross-platform. |
| `vscode_bridge.py` | Optional VS Code companion extension bridge. Standalone; never imported by the server stack. |
| `use-sandbox.ts` | Next.js React hook. Dead code — the working frontend is vanilla JS (`app.js` + `index.html`). |
| `sandbox-bridge.ts` | Next.js server-side bridge to `gui_bridge.py`. Dead code. |
| `sandbox-types.ts` | TypeScript types for the Next.js bridge. Dead code. |
| `utils.ts` | Tailwind class-merge utility for the Next.js frontend. Dead code. |
| `README(1).md` | Duplicate V5.1 README. `README.md` (V6.1) is the canonical doc. |

## Result
- **Before:** 34 files  
- **After:** 24 files  
- **Reduction:** ~29% fewer files, zero functional loss to the working server + vanilla-JS frontend stack.

## How to run
```bash
python3 server.py
# Then open http://localhost:8000
```
