#!/usr/bin/env python3
"""Quick token validity check. No refresh."""
from pathlib import Path
import yaml
from kiteconnect import KiteConnect

ROOT = Path(__file__).parent.parent
with open(ROOT / "config" / "secrets.yaml") as f:
    s = yaml.safe_load(f)
z = s["zerodha"]
kite = KiteConnect(api_key=z["api_key"])
kite.set_access_token(z["access_token"])
try:
    p = kite.profile()
    print(f"✅ Token valid. User: {p['user_name']} | "
          f"token_date: {z.get('token_date')}")
except Exception as e:
    print(f"❌ Token invalid: {e}")
    print("Run: python scripts/refresh_token.py")
