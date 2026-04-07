# Pip-Boy

<p align="center">
  <img src="docs/Imgs/Pip-BoyAdArtPrint.jpg" width="480" alt="Pip-Boy 3000 Mark IV" />
</p>

A personal assistant agent with persistent memory and a configurable persona, providing a chat-based interface for workstation interaction.

## Features

- **Conversational Interface** — Interactive chat-based communication
- **Persistent Memory** — Retains context across sessions
- **Persona System** — Configurable agent personality and behavior
- **Tool Integration** — Extensible capabilities for workstation operations

## Quick Start

**Prerequisites:** Python ≥ 3.11

```bash
# 1. Clone the repo
git clone https://github.com/ByeDream/Pip-Boy.git
cd Pip-Boy

# 2. Create & activate a virtual environment
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

# 3. Install the project (editable mode)
pip install -e .

# 4. Configure environment variables
cp .env.example .env
# Then edit .env and fill in your ANTHROPIC_API_KEY (required)
# See the Configuration table below for all available options

# 5. Run
python -m pip_agent
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes** | — | Your Anthropic API key |
| `ANTHROPIC_BASE_URL` | No | *(api.anthropic.com)* | Custom API endpoint |
| `MODEL` | No | `claude-sonnet-4-6` | Model to use |
| `MAX_TOKENS` | No | `8096` | Max response tokens |
| `SEARCH_API_KEY` | No | — | Tavily API key; falls back to DuckDuckGo |
| `SUBAGENT_MAX_ROUNDS` | No | `15` | Sub-agent max conversation rounds |
| `COMPACT_THRESHOLD` | No | `50000` | Token count to trigger compaction |
| `COMPACT_MICRO_AGE` | No | `3` | Micro-conversation expiry rounds |
| `TRANSCRIPTS_DIR` | No | `.transcripts` | Transcript storage directory |
| `TASKS_DIR` | No | `.tasks` | Task storage directory |
| `VERBOSE` | No | `true` | Verbose output |
| `PROFILER_ENABLED` | No | `false` | Enable performance profiling |

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
