# tools/ — CLI Tools and Utilities

Session reports, HAWK AI engine, historical data downloader, backtester engine, database backfill utilities.

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
tradeos backtest run --from 2025-09-01 --to 2026-03-16
tradeos backtest run --exit-mode trailing --atr-mult 1.5 --from 2025-09-01 --to 2026-03-16
tradeos backtest optimize --param atr_multiplier --range 1.0:0.25:3.0 --from 2025-09-01 --to 2026-03-16
tradeos backtest compare --modes fixed,trailing,partial --from 2025-09-01 --to 2026-03-16
tradeos backtest show --last-run  # Show most recent backtest
```

## Conventions

- All tools accessible via `tradeos` CLI subcommands
- Never call Python scripts directly in production
- HAWK engine uses OpenRouter for multi-model consensus (4 LLMs)
