# AGENTS.md

## Project

Pip is a personal assistant agent with persistent memory and a configurable persona, providing a chat-based interface for workstation interaction.

## Public Repository Policy

This is a public GitHub repository. All generated code, comments, commit messages, and documentation MUST NOT contain:

- Personal or developer-identifying information (names, emails, usernames)
- Internal hostnames, IP addresses, or private URLs
- API keys, tokens, passwords, or any credentials
- Subjective motivations or personal context about the project

## Tech Stack

- Python 3.11+
- Agent framework (TBD)
- Runs on Windows as a local service

## Conventions

- Use `src/pip_agent/` layout for all package source code (`pip_agent` to avoid collision with the Python package manager)
- Tests go in `tests/` at the project root
- Follow PEP 8; use type hints throughout
- Keep dependencies in `pyproject.toml`; pin versions in `requirements.txt` for deployment
- All configuration via environment variables or config files — never hardcoded
- All code comments and documentation must be written in English

## Profiler

Pip includes a lightweight `Profiler` class (`pip_agent.profiler`) that is **disabled by default** (`PROFILER_ENABLED=false`). When implementing any new module or feature, you **must** add profiler sampling around performance-sensitive operations:

- **API calls**: wrap with `profiler.start("api")` / `profiler.stop(input_tokens=..., output_tokens=...)`
- **Tool execution**: wrap with `profiler.start("tool:<name>")` / `profiler.stop()`
- **I/O or network**: wrap with `profiler.start("<label>")` / `profiler.stop()`
- Call `profiler.flush()` at the end of each user-facing turn to emit collected samples

The profiler is a no-op when disabled, so there is no performance overhead in production.
