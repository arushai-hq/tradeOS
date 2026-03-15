# tools/ — CLI Tools and Utilities

Session reports, HAWK AI engine, database backfill utilities.

## Skills

| Skill | When to use |
|-------|-------------|
| tradeos-operations | VPS deployment, daily workflow, CLI reference |
| tradeos-testing | Test standards and conventions |

## Commands

```bash
tradeos report auto              # EOD auto-report
tradeos hawk run --run evening   # HAWK evening analysis
tradeos hawk run --run morning   # HAWK morning update
tradeos hawk eval                # Evaluate yesterday's picks
```

## Conventions

- All tools accessible via `tradeos` CLI subcommands
- Never call Python scripts directly in production
- HAWK engine uses OpenRouter for multi-model consensus (4 LLMs)
