# AGENTS.md

## Project

Pip-Boy is a personal assistant agent with persistent memory and a configurable persona, providing a chat-based interface for workstation interaction.

## Public Repository Policy

This is a public GitHub repository. All generated code, comments, commit messages, and documentation MUST NOT contain:

- Personal or developer-identifying information (names, emails, usernames)
- Internal hostnames, IP addresses, or private URLs
- API keys, tokens, passwords, or any credentials
- Subjective motivations or personal context about the project

## Core Philosophy

Embodies a Zen-like minimalism that values simplicity and clarity above all. This approach reflects:

- **Wabi-sabi philosophy**: Embracing simplicity and the essential. Each line serves a clear purpose without unnecessary embellishment.
- **Occam's Razor thinking**: The solution should be as simple as possible, but no simpler.
- **Trust in emergence**: Complex systems work best when built from simple, well-defined components that do one thing well.
- **Present-moment focus**: The code handles what's needed now rather than anticipating every possible future scenario.
- **Pragmatic trust**: The developer trusts external systems enough to interact with them directly, handling failures as they occur rather than assuming they'll happen.

This development philosophy values clear documentation, readable code, and belief that good architecture emerges from simplicity rather than being imposed through complexity.

## Design Principles

- **CONSTRAIN, SURFACE, NEVER COACH**: Intelligence is trained, not coded. Provide eyes (information) and hands (tools) — never inject hints, suggestions, or coaching.
- **PROFILE**: Wrap performance-sensitive operations (API calls, tool execution, I/O) with `Profiler` (`pip_agent.profiler`). Disabled by default, zero overhead.
- **XML-WRAP INJECTIONS**: When injecting system-generated content into user messages, wrap it in XML tags to create a clear boundary between user intent and system context.

## Tech Stack

- Python 3.11+

## Conventions

- Use `src/pip_agent/` layout for all package source code (`pip_agent` to avoid collision with the Python package manager)
- Tests go in `tests/` at the project root
- Follow PEP 8; use type hints throughout
- Keep dependencies in `pyproject.toml` (single source of truth); no separate `requirements.txt` in this repo
- All configuration via environment variables or config files — never hardcoded
- Communicate and write plans in Chinese, keeping technical terms in English
- All code files and documentation for commits must be written in English
