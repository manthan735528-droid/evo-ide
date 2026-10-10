# What changed

Three features added on top of v6. No existing behavior was removed —
everything below is additive.

## 1. Persistent short-term memory

Before: the chat history the model uses for context lived only in RAM
(`AgentOrchestrator.history`). Restart the server and it was gone.

Now: every turn is flushed to `sandbox/.chat_memory.json` immediately
after the model responds, and reloaded automatically the next time the
server starts. Conversation continuity survives restarts.

Files: `core/agent_orchestrator.py`

## 2. Attach files from the sandbox — no more guessing paths

Before: the model only ever saw your *active* editor tab in full, plus
lightweight signatures of everything else. To give it a specific file
you had to open it as a tab first.

Now: a **+** button sits to the left of the chat composer. Click it and
a popover lists every file currently in the sandbox (pulled live from
`/api/files`) — click any to attach it, click again to remove it.
Attached files show as chips above the input and get sent to the model
as full file content alongside your prompt, whether or not that file
is open in a tab. The list is path-safe server-side (a traversal
attempt like `../etc/passwd` is rejected, not silently allowed).

Files: `core/context_manager.py`, `core/agent_orchestrator.py`,
`core/server_handler.py`, `static/index.html`, `static/app.js`,
`static/styles.css`

## 3. Autosave / auto-sync

Before: edits only hit disk on Ctrl+S, or implicitly right before you
hit Send or Run. Typing alone never saved.

Now: ~700ms after you stop typing, the active file is saved to disk
automatically (silently — no toast spam). It also flushes immediately
when the editor loses focus, and best-effort on tab close via
`navigator.sendBeacon`. A small "synced HH:MM:SS" indicator in the
status bar shows the last autosave. Manual Ctrl+S still works exactly
as before, with the same toast confirmation.

Files: `static/app.js`

## Testing notes

All three were exercised directly (not just read over):
- Simulated a server restart with a fresh orchestrator instance and
  confirmed history reloads from `.chat_memory.json`.
- Fed the attachment path resolver a legitimate file and a directory
  traversal attempt (`../etc/passwd`) — the real file's content came
  back in the prompt, the traversal attempt was rejected.
- Hit `/api/save` directly and confirmed the write lands on disk
  immediately.
- Booted the real server and posted to `/api/chat_stream` with an
  `attachments` list — the request flowed end-to-end through the new
  code path with no errors (only stopped at "Ollama not running,"
  which is expected with no model host available).
