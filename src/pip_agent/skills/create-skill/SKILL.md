---
name: create-skill
description: >-
  Guide users through creating new Pip-Boy skills. Use when the user wants to
  create, write, or author a new skill, or asks about skill format or
  best practices.
tags: [skill, authoring]
---

# Creating Skills for Pip-Boy

Help the user create a well-structured skill. Follow the workflow below.

## Phase 1: Gather Requirements

Before writing, clarify with the user:

1. **Purpose**: What task or workflow should this skill address?
2. **Trigger**: When should the agent load this skill?
3. **Domain knowledge**: What does the agent need that it would not already know?
4. **Output format**: Are there specific templates or conventions required?

If you have conversation context, infer the skill from what was discussed.

## Phase 2: Create the Skill

### Directory layout

Each skill is a directory containing a `SKILL.md` file:

```
skill-name/
  SKILL.md              # Required -- main instructions
  reference.md          # Optional -- detailed docs
  examples.md           # Optional -- usage examples
```

User skills go in `.pip/skills/` at the project root:

```
.pip/skills/
  my-skill/
    SKILL.md
```

### Overriding built-in skills

If a user skill has the **same name** as a built-in skill, the user version
takes precedence. This lets projects replace default workflows with their own.
For example, a Perforce project can create `.pip/skills/code-review/SKILL.md`
to override the built-in code-review with team-specific standards.

### SKILL.md format

Every skill requires YAML frontmatter followed by a markdown body:

```markdown
---
name: my-skill
description: >-
  Brief description of what this skill does.
  Include when to use it.
tags: [topic1, topic2]
---

# My Skill

Step-by-step instructions here.
```

### Frontmatter fields

| Field | Required | Rules |
|-------|----------|-------|
| `name` | Yes | Lowercase, hyphens, max 64 chars. Must match directory name. |
| `description` | Yes | Non-empty. Describes WHAT the skill does and WHEN to use it. |
| `tags` | No | List of short keywords for discoverability. |

### Writing good descriptions

The description appears in the system prompt catalog. Write it in **third person**:

- **Good**: "Review code for quality and security. Use when the user asks for a code review."
- **Bad**: "I help you review code."

Include both **WHAT** (capabilities) and **WHEN** (trigger scenarios).

## Phase 3: Authoring Best Practices

1. **Be concise** -- The context window is shared. Only add knowledge the agent does not already have.
2. **Keep SKILL.md under 300 lines** -- Put detailed reference in separate files.
3. **Be specific** -- Give one recommended approach, not five alternatives.
4. **Use checklists** -- Break workflows into numbered steps or checkboxes.
5. **Include examples** -- Concrete input/output pairs beat abstract descriptions.

## Phase 4: Verify

Before finishing, confirm:

- [ ] `name` matches the directory name
- [ ] `description` includes WHAT and WHEN
- [ ] Body is under 300 lines
- [ ] No redundant information the agent already knows
- [ ] All file references are one level deep (linked from SKILL.md)
