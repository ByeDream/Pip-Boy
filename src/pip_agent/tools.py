from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from pip_agent.config import settings

WORKDIR = Path.cwd()


def safe_path(raw: str) -> Path:
    """Resolve a path and ensure it lives inside WORKDIR."""
    resolved = (WORKDIR / raw).resolve()
    if not resolved.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes working directory: {raw}")
    return resolved


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic tool-use format)
# ---------------------------------------------------------------------------

BASH_SCHEMA = {
    "name": "bash",
    "description": (
        "Execute a shell command and return its output. "
        "On Windows this runs in cmd.exe, on Unix in /bin/sh."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds. Default 120.",
            },
            "background": {
                "type": "boolean",
                "description": (
                    "If true, run in background and return immediately. "
                    "Result delivered later via notification."
                ),
            },
        },
        "required": ["command"],
    },
}

READ_SCHEMA = {
    "name": "read",
    "description": (
        "Read a file and return its contents with line numbers. "
        "Optionally specify offset (1-indexed) and limit to read a slice."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file (relative to working directory).",
            },
            "offset": {
                "type": "integer",
                "description": "Starting line number (1-indexed). Default 1.",
            },
            "limit": {
                "type": "integer",
                "description": "Number of lines to read. Default: all.",
            },
        },
        "required": ["file_path"],
    },
}

WRITE_SCHEMA = {
    "name": "write",
    "description": "Create or overwrite a file with the given content.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file (relative to working directory).",
            },
            "content": {
                "type": "string",
                "description": "The content to write.",
            },
        },
        "required": ["file_path", "content"],
    },
}

EDIT_SCHEMA = {
    "name": "edit",
    "description": (
        "Find and replace a unique string in a file. "
        "old_string must appear exactly once in the file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file (relative to working directory).",
            },
            "old_string": {
                "type": "string",
                "description": "The exact string to find (must be unique in the file).",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement string.",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}

GLOB_SCHEMA = {
    "name": "glob",
    "description": "List files matching a glob pattern within the working directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py' or '*.txt'.",
            },
            "path": {
                "type": "string",
                "description": "Base directory for the search (relative to working directory). Default: '.'.",
            },
        },
        "required": ["pattern"],
    },
}

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web and return results with titles, URLs, and snippets.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return. Default 5.",
            },
        },
        "required": ["query"],
    },
}

WEB_FETCH_SCHEMA = {
    "name": "web_fetch",
    "description": "Fetch a URL and return its content as readable text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch.",
            },
        },
        "required": ["url"],
    },
}

TASK_SCHEMA = {
    "name": "task",
    "description": (
        "Delegate a task to a sub-agent that runs in an isolated context. "
        "The sub-agent starts with a fresh conversation, performs the task "
        "using tools, and returns only a concise summary. Use this for "
        "research, exploration, or any multi-step work whose intermediate "
        "details don't need to persist in your conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "A detailed description of the task for the sub-agent.",
            },
        },
        "required": ["prompt"],
    },
}

TASK_CREATE_SCHEMA = {
    "name": "task_create",
    "description": (
        "Create stories or tasks. Omit 'story' to create stories (big goals); "
        "provide 'story' to create tasks within that story. "
        "Load the 'task-planning' skill for detailed guidance."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "story": {
                "type": "string",
                "description": "Story ID to add tasks to. Omit to create stories instead.",
            },
            "tasks": {
                "type": "array",
                "description": "Array of new stories or tasks to create.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique identifier (alphanumeric, dashes, underscores).",
                        },
                        "title": {
                            "type": "string",
                            "description": "Short description.",
                        },
                        "blocked_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "IDs that must complete first (stories or tasks at same level).",
                        },
                    },
                    "required": ["id", "title"],
                },
            },
        },
        "required": ["tasks"],
    },
}

TASK_UPDATE_SCHEMA = {
    "name": "task_update",
    "description": (
        "Update stories or tasks. Omit 'story' to update story metadata "
        "(title/blocked_by only; status is auto-derived). "
        "Provide 'story' to update tasks (status, title, blocked_by). "
        "Completing all tasks in a story auto-deletes it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "story": {
                "type": "string",
                "description": "Story ID containing the tasks. Omit to update stories.",
            },
            "tasks": {
                "type": "array",
                "description": "Array of updates.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "ID of the story or task to update.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "New status (tasks only; stories derive status automatically).",
                        },
                        "title": {
                            "type": "string",
                            "description": "New title.",
                        },
                        "blocked_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Replace entire blocking IDs list.",
                        },
                        "add_blocked_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "IDs to add to blocking list (ignored if blocked_by is set).",
                        },
                        "remove_blocked_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "IDs to remove from blocking list (ignored if blocked_by is set).",
                        },
                        "owner": {
                            "type": "string",
                            "description": "Owner/agent claiming this task.",
                        },
                    },
                    "required": ["id"],
                },
            },
        },
        "required": ["tasks"],
    },
}

TASK_LIST_SCHEMA = {
    "name": "task_list",
    "description": (
        "Show the task graph. Omit 'story' for a Kanban overview of all stories "
        "and ready tasks. Provide 'story' for detailed task view of one story."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "story": {
                "type": "string",
                "description": "Story ID to inspect. Omit for global overview.",
            },
        },
    },
}

TASK_REMOVE_SCHEMA = {
    "name": "task_remove",
    "description": (
        "Remove stories or tasks. Omit 'story' to remove entire stories. "
        "Provide 'story' to remove tasks within it. "
        "Fails if other items depend on them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "story": {
                "type": "string",
                "description": "Story ID containing the tasks. Omit to remove stories.",
            },
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IDs of stories or tasks to remove.",
            },
        },
        "required": ["task_ids"],
    },
}

TASK_TOOL_NAMES = frozenset({"task_create", "task_update", "task_list", "task_remove"})

CHECK_BACKGROUND_SCHEMA = {
    "name": "check_background",
    "description": "Check background task status. Omit task_id to list all tasks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID to inspect. Omit to list all.",
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def run_bash(tool_input: dict) -> str:
    command = tool_input["command"]
    timeout = tool_input.get("timeout", 120)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]"


def run_read(tool_input: dict) -> str:
    path = safe_path(tool_input["file_path"])
    if not path.is_file():
        return f"File not found: {tool_input['file_path']}"

    lines = path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    offset = tool_input.get("offset", 1)
    limit = tool_input.get("limit", total)

    start = max(offset - 1, 0)
    end = start + limit
    selected = lines[start:end]

    numbered = [f"{start + i + 1:6}|{line}" for i, line in enumerate(selected)]
    header = f"[{path.name}: {total} lines total, showing {start + 1}-{min(end, total)}]"
    return header + "\n" + "\n".join(numbered)


def run_write(tool_input: dict) -> str:
    path = safe_path(tool_input["file_path"])
    content = tool_input["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content.encode('utf-8'))} bytes to {tool_input['file_path']}"


def run_edit(tool_input: dict) -> str:
    path = safe_path(tool_input["file_path"])
    if not path.is_file():
        return f"File not found: {tool_input['file_path']}"

    content = path.read_text(encoding="utf-8")
    old = tool_input["old_string"]
    new = tool_input["new_string"]

    count = content.count(old)
    if count == 0:
        return "old_string not found in file."
    if count > 1:
        return f"old_string appears {count} times; must be unique. Add more context."

    content = content.replace(old, new, 1)
    path.write_text(content, encoding="utf-8")
    return f"Edited {tool_input['file_path']} (replaced 1 occurrence)."


def run_glob(tool_input: dict) -> str:
    base = safe_path(tool_input.get("path", "."))
    if not base.is_dir():
        return f"Directory not found: {tool_input.get('path', '.')}"

    pattern = tool_input["pattern"]
    matches = sorted(base.glob(pattern))
    paths = []
    for m in matches[:200]:
        try:
            paths.append(str(m.relative_to(WORKDIR)))
        except ValueError:
            continue
    if not paths:
        return "(no matches)"
    return "\n".join(paths)


def _search_tavily(query: str, max_results: int) -> str:
    from tavily import TavilyClient  # type: ignore[import-untyped]

    client = TavilyClient(api_key=settings.search_api_key)
    response = client.search(query, max_results=max_results)
    results = response.get("results", [])
    if not results:
        return "(no results)"
    parts = []
    for r in results:
        parts.append(f"[{r.get('title', '')}]({r.get('url', '')})\n{r.get('content', '')}")
    return "\n\n".join(parts)


def _search_duckduckgo(query: str, max_results: int) -> str:
    from ddgs import DDGS  # type: ignore[import-untyped]

    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    if not results:
        return "(no results)"
    parts = []
    for r in results:
        parts.append(f"[{r.get('title', '')}]({r.get('href', '')})\n{r.get('body', '')}")
    return "\n\n".join(parts)


def run_web_search(tool_input: dict) -> str:
    query = tool_input["query"]
    max_results = tool_input.get("max_results", 5)

    if settings.search_api_key:
        try:
            return _search_tavily(query, max_results)
        except Exception:
            pass
    return _search_duckduckgo(query, max_results)


def run_web_fetch(tool_input: dict) -> str:
    import httpx

    url = tool_input["url"]
    max_chars = 8000
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        return f"[fetch error: {e}]"

    content_type = resp.headers.get("content-type", "")
    text = resp.text

    if "html" in content_type:
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated at {max_chars} chars]"
    return text or "(empty response)"


# ---------------------------------------------------------------------------
# Dispatch map and public API
# ---------------------------------------------------------------------------

TOOL_DISPATCH: dict[str, Callable[[dict], str]] = {
    "bash": run_bash,
    "read": run_read,
    "write": run_write,
    "edit": run_edit,
    "glob": run_glob,
    "web_search": run_web_search,
    "web_fetch": run_web_fetch,
}

ALL_TOOLS = [
    BASH_SCHEMA,
    READ_SCHEMA,
    WRITE_SCHEMA,
    EDIT_SCHEMA,
    GLOB_SCHEMA,
    WEB_SEARCH_SCHEMA,
    WEB_FETCH_SCHEMA,
    TASK_SCHEMA,
    TASK_CREATE_SCHEMA,
    TASK_UPDATE_SCHEMA,
    TASK_LIST_SCHEMA,
    TASK_REMOVE_SCHEMA,
    CHECK_BACKGROUND_SCHEMA,
]


def execute_tool(name: str, tool_input: dict) -> str:
    handler = TOOL_DISPATCH.get(name)
    if handler is None:
        return f"Unknown tool: {name}"
    try:
        return handler(tool_input)
    except ValueError as e:
        return f"[blocked] {e}"
    except Exception as e:
        return f"[error] {e}"
