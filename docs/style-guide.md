# Docs style guide

> **Documentation conventions — baseline `006f95f8`.** See the current-state snapshot for canonical scope, numbered source citations and complete docs navigation. Keep dated report bodies with explicit non-governing warnings; do not turn old proofs into runtime guarantees. Use actual `[[docs/path-without-md|Wiki counterpart]]` navigation, not invented wiki query URLs, and leave filesystem inventory/sync to the coordinator.

## Principles

- **Be concrete.** Prefer commands, examples, and “expected output” over prose.
- **Don’t invent behavior.** If unsure, link to the source file and mark it as “verify”.
- **Optimize for scanning.** Short sections, bullets, and tables.
- **Call out risk.** Anything destructive or security-sensitive should be labeled clearly.

## Markdown conventions

- Use sentence-case headings.
- Prefer fenced code blocks with a language (`bash`, `yaml`, `json`).
- For warnings/notes, use simple callouts:

```md
> **Note**
> ...

> **Warning**
> ...
```

## Common templates

### Procedure

1. Prereqs
2. Steps
3. Verify
4. Troubleshooting

### Config reference entry

- **Name**
- **Where set** (`.env`, env var, compose)
- **Default**
- **Example**
- **Notes / pitfalls**
