# tools/ — CLI Tools and Utilities

Session reports, HAWK AI engine, historical data downloader, database backfill utilities.

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
tradeos data download --all      # Download all historical candles
tradeos data download --interval 15min --days 1095
tradeos data status              # Show download coverage
```

## Conventions

- All tools accessible via `tradeos` CLI subcommands
- Never call Python scripts directly in production
- HAWK engine uses OpenRouter for multi-model consensus (4 LLMs)
