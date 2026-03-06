# TradeOS Scripts

## First-time setup (any machine)

  bash scripts/setup.sh

  This creates .venv/ in repo root with all dependencies.
  Run once per machine. Re-run if requirements.txt changes.

## Daily workflow

  source activate.sh                          # activate venv
  python scripts/refresh_token.py             # 90s token refresh
  tmux new -s tradeos
  python main.py 2>&1 | tee logs/paper_session_XX.log

  Quick token check only:
    python scripts/verify_token.py

## Token refresh process (why it can't be fully automated)
  Zerodha requires browser login + 2FA every day by design.
  This is a security feature — not a limitation of this script.
  The script automates everything except the browser login itself.
