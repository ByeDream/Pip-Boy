import subprocess

BASH_TOOL = {
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
        },
        "required": ["command"],
    },
}

ALL_TOOLS = [BASH_TOOL]


def run_bash(command: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]"
    except Exception as e:
        return f"[error: {e}]"


def execute_tool(name: str, tool_input: dict) -> str:
    if name == "bash":
        return run_bash(tool_input["command"])
    return f"Unknown tool: {name}"
