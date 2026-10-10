#!/bin/bash
# Evo IDE V6 - Mac launcher
# Zero third-party dependencies (Python stdlib only).
# Target model: Qwen2.5-Coder-14B-Instruct-abliterated Q4_K_M
set -e

cd "$(dirname "$0")"

MODEL="${EVO_IDE_MODEL:-hf.co/bartowski/Qwen2.5-Coder-14B-Instruct-abliterated-GGUF:Q4_K_M}"
PORT="${EVO_IDE_PORT:-8000}"

if ! command -v python3 &> /dev/null; then
    echo "python3 not found. Install it from https://www.python.org/downloads/ or 'brew install python3'."
    exit 1
fi

# ── Ollama checks ──────────────────────────────────────────
if ! command -v ollama &> /dev/null; then
    echo "ERROR: ollama not found. Install with: brew install ollama"
    exit 1
fi

if ! curl -s -o /dev/null --max-time 2 http://localhost:11434/api/tags; then
    echo "Ollama is not running — starting it in the background..."
    nohup ollama serve > /tmp/ollama.log 2>&1 &
    for i in $(seq 1 20); do
        sleep 0.5
        if curl -s -o /dev/null --max-time 2 http://localhost:11434/api/tags; then
            break
        fi
    done
fi

# Pull the target model if not present (idempotent; large download first time)
if ! ollama list 2>/dev/null | grep -qi "Qwen2.5-Coder-14B-Instruct-abliterated"; then
    echo "Pulling target model (one-time, ~9 GB): $MODEL"
    ollama pull "$MODEL"
fi

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "")"

echo "Starting Evo IDE V6 on http://localhost:${PORT} ..."
if [ -n "$LAN_IP" ]; then
    echo "Same Wi-Fi devices can connect at: http://${LAN_IP}:${PORT}"
else
    echo "Could not detect a Wi-Fi/LAN IP — check you're connected to a network."
fi
echo "Model: $MODEL"
exec python3 server.py
