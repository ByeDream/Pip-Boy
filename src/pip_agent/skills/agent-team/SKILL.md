---
name: agent-team
description: >-
  Agent Team: delegate work to subagents using the SDK Task tool,
  coordinate parallel workstreams, and track progress with TodoWrite.
tags: [team, collaboration]
---

# Agent Team

## Overview

Use the SDK-native `Task` tool to spawn subagents. Each subagent runs as
a full Claude Code instance with all built-in tools and MCP access.

Use `TodoWrite` to plan and track work across subagents.

## Spawning a Subagent

```
Task(description="short label", prompt="detailed instructions...")
```

- **description**: 3-5 word label shown in the UI
- **prompt**: full context the subagent needs — it has no access to your
  conversation history. Include: goal, relevant file paths, constraints,
  and what to return in its final message.

The subagent returns a single message when done. Extract its result and
continue.

## Parallel Execution

Launch multiple `Task` calls in the same response to run subagents
concurrently:

```
Task(description="implement auth", prompt="...")
Task(description="write tests", prompt="...")
```

Both run in parallel; results arrive when each finishes.

## Coordination Patterns

### Fan-out / Fan-in

1. Use `TodoWrite` to create a task list
2. Spawn one `Task` per work item
3. Collect results, update todos, integrate

### Pipeline

1. Spawn Task A
2. Use A's result as input to Task B
3. Continue chaining as needed

### Review Loop

1. Spawn a Task to implement
2. Spawn a second Task to review the output
3. If review finds issues, spawn another implementation Task

## Best Practices

- **Be explicit in prompts**: subagents have no shared context
- **Include file paths**: tell subagents exactly which files to read/modify
- **Scope work narrowly**: smaller, focused tasks succeed more reliably
- **Request structured output**: ask subagents to return results in a
  specific format for easier integration
