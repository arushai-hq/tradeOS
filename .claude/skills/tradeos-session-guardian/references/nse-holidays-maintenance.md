# NSE Holidays Maintenance Guide

## What This File Is For

`config/nse_holidays.yaml` is a manually maintained calendar of NSE equity market
non-trading days. Phase 0 CHECK 4 reads this file to determine whether today is a
trading day before connecting to Zerodha.

This file must be updated **once per year** — in December — with the following year's
holiday list. NSE publishes the calendar in December or January for the upcoming year.

---

## File Format

```yaml
# config/nse_holidays.yaml
# NSE equity market trading holidays (BSE alignment — same dates for equities)
# Source: https://www.nseindia.com/resources/exchange-communication-holidays
# Updated: [date of last update]

"2026":
  - "2026-01-14"  # Makar Sankranti / Pongal
  - "2026-01-26"  # Republic Day
  - "2026-02-19"  # Chhatrapati Shivaji Maharaj Jayanti
  - "2026-03-13"  # Holi
  - "2026-03-30"  # Id-Ul-Fitr (Ramzan Id) — confirm date closer to event
  - "2026-04-02"  # Shri Ram Navami
  - "2026-04-03"  # Good Friday
  - "2026-04-14"  # Dr. Baba Saheb Ambedkar Jayanti
  - "2026-05-01"  # Maharashtra Day
  - "2026-08-15"  # Independence Day
  - "2026-08-27"  # Ganesh Chaturthi
  - "2026-10-02"  # Mahatma Gandhi Jayanti / Dussehra
  - "2026-10-20"  # Diwali (Laxmi Pujan)
  - "2026-10-21"  # Diwali (Balipratipada)
  - "2026-11-05"  # Prakash Gurpurb Sri Guru Nanak Dev Ji
  - "2026-11-19"  # Christmas (if applicable — check NSE circular)
  - "2026-12-25"  # Christmas

"2027":
  # Add next year's list in December 2026
```

**Key format rules:**
- Top-level key is the year as a **quoted string** (`"2026"`, not `2026`)
  - Both quoted string and integer keys are supported in code, but quoted string is canonical
- Each date is a quoted ISO 8601 string (`"YYYY-MM-DD"`)
- Keep the comment for each holiday — aids future maintenance
- The file must cover at minimum the current calendar year

---

## Where to Get the Official Holiday List

**Official source:** NSE India publishes the holiday calendar for the following year.
1. Visit: https://www.nseindia.com/resources/exchange-communication-holidays
2. Look for "Capital Markets" (CM) holidays — these are the equity market holidays
3. Download or copy the list for the following year
4. Note: Some dates (like Id-Ul-Fitr) depend on moon sighting — confirm closer to the date

**Alternative: BSE holiday list** (identical for equities, sometimes published earlier)
1. Visit: https://www.bseindia.com/markets/PublicIssues/HolidayList.aspx

---

## When to Update

| When | What to do |
|------|-----------|
| **Every December** | Add the following year's holiday list to `nse_holidays.yaml` |
| **November/December** | Monitor NSE circulars for any late additions or cancellations |
| **Before major elections** | NSE may add trading holidays for election days — check circulars |
| **After NSE circular** | Any mid-year additions (rare) require immediate update |

Phase 2 MONITOR C warns automatically on December 31 if next year's list is missing.

---

## Weekend Detection

Saturday and Sunday are non-trading days and are detected by `weekday()` in Phase 0 CHECK 4.
These do not need to be listed in `nse_holidays.yaml`.

```python
weekday = datetime.now(IST).weekday()  # Monday=0, Sunday=6
if weekday >= 5:  # Saturday=5, Sunday=6
    # Non-trading day
```

Weekends are not in the file because `weekday()` is reliable and doesn't require maintenance.

---

## What Happens If the File Is Missing

If `config/nse_holidays.yaml` is not found at startup, Phase 0 CHECK 4 logs a WARNING and
**continues without holiday detection**. This is intentional — the system can trade even
without the holiday file; it just won't auto-detect non-trading days (the WebSocket simply
won't receive any ticks on holidays, which is harmless).

```python
except FileNotFoundError:
    log.warning("nse_holidays_file_missing",
                path="config/nse_holidays.yaml",
                note="Cannot check NSE holidays — proceeding without check")
    return  # Non-fatal
```

Maintaining the file is best practice, but not a hard requirement for system operation.

---

## Gitignore Note

`config/nse_holidays.yaml` is **safe to commit** — it contains no secrets, only public data.
Add it to version control alongside `config/settings.yaml`.
