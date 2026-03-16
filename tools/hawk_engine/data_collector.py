"""
HAWK — Tier 1 Data Collector.

Fetches NSE market data for evening/morning analysis.
Priority: KiteConnect → nsetools → hardcoded fallback.
Never crashes — partial data is better than no data.
"""
from __future__ import annotations

from datetime import date, datetime

import structlog

log = structlog.get_logger()

# Well-known index instrument tokens (NSE segment)
INDEX_TOKENS = {
    "nifty_50": 256265,
    "bank_nifty": 260105,
    "india_vix": 264969,
}

# Sector index names → keys for kite.ohlc() lookup
SECTOR_INDICES = [
    ("NIFTY IT", "nifty_it"),
    ("NIFTY BANK", "nifty_bank"),
    ("NIFTY PHARMA", "nifty_pharma"),
    ("NIFTY AUTO", "nifty_auto"),
    ("NIFTY METAL", "nifty_metal"),
    ("NIFTY REALTY", "nifty_realty"),
    ("NIFTY ENERGY", "nifty_energy"),
    ("NIFTY FMCG", "nifty_fmcg"),
]


def _init_kiteconnect() -> "KiteConnect | None":
    """Create shared KiteConnect instance. Returns None on auth failure."""
    try:
        from tools.hawk_engine.config import load_secrets
        secrets = load_secrets()
        api_key = secrets.get("zerodha", {}).get("api_key", "")
        access_token = secrets.get("zerodha", {}).get("access_token", "")
        if api_key and access_token:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=api_key)
            kite.set_access_token(access_token)
            # Quick probe to verify token is valid
            kite.profile()
            log.info("hawk_kite_init_ok")
            return kite
        log.warning("hawk_kite_no_credentials")
    except Exception as exc:
        log.warning("hawk_kite_init_failed", error=str(exc))
    return None


def _fetch_nifty50_stocks(kite_instruments: list[dict] | None = None) -> list[str]:
    """Get NIFTY 50 constituent list.

    Uses hardcoded list as canonical source.
    If kite_instruments provided, verifies each symbol resolves.
    Falls back to nsetools as secondary attempt.
    """
    from tools.hawk_engine.config import NIFTY_50_STOCKS
    stocks = list(NIFTY_50_STOCKS)

    # Verify against KiteConnect instruments if available
    if kite_instruments:
        valid_symbols = {
            i["tradingsymbol"] for i in kite_instruments
            if i.get("instrument_type") == "EQ"
        }
        verified = [s for s in stocks if s in valid_symbols]
        removed = set(stocks) - set(verified)
        if removed:
            log.warning("hawk_nifty50_unresolved", symbols=sorted(removed))
        if verified:
            log.info("hawk_nifty50_fetched", source="hardcoded+kite_verified", count=len(verified))
            return verified

    # Secondary: nsetools
    try:
        from nsetools import Nse
        nse = Nse()
        nse_stocks = nse.get_index_stock_list("NIFTY 50")
        if nse_stocks and len(nse_stocks) >= 40:
            log.info("hawk_nifty50_fetched", source="nsetools", count=len(nse_stocks))
            return nse_stocks
    except Exception as exc:
        log.warning("hawk_nifty50_nsetools_failed", error=str(exc))

    # Final fallback: hardcoded as-is
    log.info("hawk_nifty50_fetched", source="hardcoded", count=len(stocks))
    return stocks


def _fetch_bhavcopy(
    target_date: date,
    stocks: list[str],
    kite: "KiteConnect | None" = None,
    token_map: dict[str, int] | None = None,
) -> tuple[list[dict], list[str]]:
    """Fetch bhavcopy (daily OHLCV) data for given stocks.

    Priority: KiteConnect → nsepython enrichment for delivery_pct.
    Returns (bhavcopy_list, sources_used).
    """
    sources_used: list[str] = []
    delivery_map: dict[str, float] = {}

    # Try nsepython for delivery_pct data (enrichment source)
    try:
        from nsepython import nse_eq_symbols
        bhav = nse_eq_symbols(target_date.strftime("%d-%m-%Y"), "EQ")
        if bhav is not None and not bhav.empty:
            for _, row in bhav.iterrows():
                symbol = str(row.get("SYMBOL", ""))
                if symbol in stocks:
                    delivery_map[symbol] = float(
                        row.get("DELIV_PER", row.get("DELIVERY_PERCENTAGE", 0))
                    )
            if delivery_map:
                log.info("hawk_delivery_pct_fetched", source="nsepython", count=len(delivery_map))
    except Exception as exc:
        log.warning("hawk_delivery_nsepython_failed", error=str(exc))

    # Primary: KiteConnect historical_data
    if kite and token_map:
        try:
            result = []
            from_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            to_dt = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
            for symbol in stocks:
                token = token_map.get(symbol)
                if not token:
                    continue
                try:
                    data = kite.historical_data(token, from_dt, to_dt, "day")
                    if data:
                        d = data[-1]
                        open_p = float(d["open"])
                        close_p = float(d["close"])
                        result.append({
                            "symbol": symbol,
                            "open": open_p,
                            "high": float(d["high"]),
                            "low": float(d["low"]),
                            "close": close_p,
                            "volume": int(d["volume"]),
                            "delivery_pct": delivery_map.get(symbol, 0.0),
                            "change_pct": round((close_p - open_p) / open_p * 100, 2) if open_p > 0 else 0.0,
                        })
                except Exception:
                    continue
            if result:
                sources_used.append("kiteconnect")
                if delivery_map:
                    sources_used.append("nsepython")
                log.info("hawk_bhavcopy_fetched", source="kiteconnect", count=len(result))
                return result, sources_used
        except Exception as exc:
            log.warning("hawk_bhavcopy_kite_failed", error=str(exc))

    # Fallback: nsepython full bhavcopy
    try:
        from nsepython import nse_eq_symbols
        bhav = nse_eq_symbols(target_date.strftime("%d-%m-%Y"), "EQ")
        if bhav is not None and not bhav.empty:
            result = []
            for _, row in bhav.iterrows():
                symbol = str(row.get("SYMBOL", ""))
                if symbol not in stocks:
                    continue
                open_p = float(row.get("OPEN_PRICE", row.get("OPEN", 0)))
                close_p = float(row.get("CLOSE_PRICE", row.get("CLOSE", 0)))
                result.append({
                    "symbol": symbol,
                    "open": open_p,
                    "high": float(row.get("HIGH_PRICE", row.get("HIGH", 0))),
                    "low": float(row.get("LOW_PRICE", row.get("LOW", 0))),
                    "close": close_p,
                    "volume": int(row.get("TTL_TRD_QNTY", row.get("TOTAL_TRADED_QUANTITY", 0))),
                    "delivery_pct": float(row.get("DELIV_PER", row.get("DELIVERY_PERCENTAGE", 0))),
                    "change_pct": round((close_p - open_p) / open_p * 100, 2) if open_p > 0 else 0.0,
                })
            if result:
                sources_used.append("nsepython")
                log.info("hawk_bhavcopy_fetched", source="nsepython", count=len(result))
                return result, sources_used
    except Exception as exc:
        log.warning("hawk_bhavcopy_nsepython_failed", error=str(exc))

    # Last resort: KiteConnect without shared instance (legacy path)
    try:
        from tools.hawk_engine.config import load_secrets
        secrets = load_secrets()
        api_key = secrets.get("zerodha", {}).get("api_key", "")
        access_token = secrets.get("zerodha", {}).get("access_token", "")
        if api_key and access_token:
            from kiteconnect import KiteConnect
            kite_local = KiteConnect(api_key=api_key)
            kite_local.set_access_token(access_token)
            instruments = kite_local.instruments("NSE")
            local_map = {
                i["tradingsymbol"]: i["instrument_token"]
                for i in instruments if i["tradingsymbol"] in stocks
            }
            result = []
            from_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            to_dt = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
            for symbol, token in local_map.items():
                try:
                    data = kite_local.historical_data(token, from_dt, to_dt, "day")
                    if data:
                        d = data[-1]
                        open_p = float(d["open"])
                        close_p = float(d["close"])
                        result.append({
                            "symbol": symbol,
                            "open": open_p,
                            "high": float(d["high"]),
                            "low": float(d["low"]),
                            "close": close_p,
                            "volume": int(d["volume"]),
                            "delivery_pct": 0.0,
                            "change_pct": round((close_p - open_p) / open_p * 100, 2) if open_p > 0 else 0.0,
                        })
                except Exception:
                    continue
            if result:
                sources_used.append("kiteconnect")
                log.info("hawk_bhavcopy_fetched", source="kiteconnect_standalone", count=len(result))
                return result, sources_used
    except Exception as exc:
        log.warning("hawk_bhavcopy_kite_standalone_failed", error=str(exc))

    log.error("hawk_bhavcopy_all_sources_failed")
    return [], sources_used


def _fetch_fii_dii(target_date: date) -> tuple[dict, list[str]]:
    """Fetch FII/DII flow data.

    nse_fii was removed from nsepython. Try alternative imports,
    fall back to zeros with warning.
    """
    sources_used: list[str] = []

    # Try nsepython nse_fii (may not exist in newer versions)
    try:
        from nsepython import nse_fii
        fii_data = nse_fii()
        if fii_data is not None and isinstance(fii_data, dict):
            sources_used.append("nsepython")
            log.info("hawk_fii_dii_fetched", source="nsepython")
            return {
                "fii_net_equity": float(fii_data.get("fii_net", fii_data.get("FII_NET", 0))),
                "dii_net_equity": float(fii_data.get("dii_net", fii_data.get("DII_NET", 0))),
            }, sources_used
    except ImportError:
        log.warning("hawk_fii_dii_import_removed", note="nse_fii removed from nsepython")
    except Exception as exc:
        log.warning("hawk_fii_dii_failed", error=str(exc))

    # Try nsepython nse_fii_dii (alternative function name in newer versions)
    try:
        from nsepython import nse_fii_dii
        fii_data = nse_fii_dii()
        if fii_data is not None and isinstance(fii_data, dict):
            sources_used.append("nsepython")
            log.info("hawk_fii_dii_fetched", source="nsepython_v2")
            return {
                "fii_net_equity": float(fii_data.get("fii_net", fii_data.get("FII_NET", 0))),
                "dii_net_equity": float(fii_data.get("dii_net", fii_data.get("DII_NET", 0))),
            }, sources_used
    except ImportError:
        pass
    except Exception as exc:
        log.warning("hawk_fii_dii_v2_failed", error=str(exc))

    log.warning("hawk_fii_dii_all_failed", note="Returning zeros — no FII/DII source available")
    return {"fii_net_equity": 0.0, "dii_net_equity": 0.0}, sources_used


def _fetch_indices(kite: "KiteConnect | None" = None) -> tuple[dict, list[str]]:
    """Fetch major indices — Nifty 50, Bank Nifty, India VIX.

    Priority: KiteConnect → nsetools.
    """
    sources_used: list[str] = []

    # Primary: KiteConnect with known instrument tokens
    if kite:
        try:
            token_keys = {
                INDEX_TOKENS["nifty_50"]: "nifty_50",
                INDEX_TOKENS["bank_nifty"]: "bank_nifty",
                INDEX_TOKENS["india_vix"]: "india_vix",
            }
            quotes = kite.ohlc([f"NSE:{t}" for t in token_keys.keys()])
            # ohlc() with tokens: keys are "NSE:<token>"
            # Try alternative: use quote() which returns more data
            quotes = kite.quote([f"NSE:{t}" for t in token_keys.keys()])

            indices = {}
            for token, key in token_keys.items():
                q = quotes.get(f"NSE:{token}", {})
                if q:
                    indices[key] = {
                        "close": float(q.get("last_price", 0)),
                        "change_pct": float(q.get("ohlc", {}).get("close", 0)),
                    }
            if indices:
                sources_used.append("kiteconnect")
                log.info("hawk_indices_fetched", source="kiteconnect", count=len(indices))
                return indices, sources_used
        except Exception as exc:
            log.warning("hawk_indices_kite_failed", error=str(exc))

    # Fallback: nsetools
    try:
        from nsetools import Nse
        nse = Nse()

        indices = {}
        for idx_name, key in [("NIFTY 50", "nifty_50"), ("NIFTY BANK", "bank_nifty")]:
            try:
                q = nse.get_index_quote(idx_name)
                if q:
                    indices[key] = {
                        "close": float(q.get("lastPrice", q.get("last", 0))),
                        "change_pct": float(q.get("pChange", q.get("percentChange", 0))),
                    }
            except Exception:
                pass

        # VIX
        try:
            q = nse.get_index_quote("INDIA VIX")
            if q:
                indices["india_vix"] = {
                    "close": float(q.get("lastPrice", q.get("last", 0))),
                    "change_pct": float(q.get("pChange", q.get("percentChange", 0))),
                }
        except Exception:
            pass

        if indices:
            sources_used.append("nsetools")
            log.info("hawk_indices_fetched", source="nsetools", count=len(indices))
            return indices, sources_used
    except Exception as exc:
        log.warning("hawk_indices_nsetools_failed", error=str(exc))

    return {}, sources_used


def _fetch_sectors(
    kite: "KiteConnect | None" = None,
    kite_instruments: list[dict] | None = None,
) -> tuple[dict, list[str]]:
    """Fetch sector index performance.

    Priority: KiteConnect → nsetools.
    """
    sources_used: list[str] = []

    # Primary: KiteConnect — look up sector index tokens from instruments
    if kite and kite_instruments:
        try:
            # Build index token map from instruments list
            idx_map: dict[str, int] = {}
            for inst in kite_instruments:
                if inst.get("instrument_type") == "IN":
                    idx_map[inst["tradingsymbol"]] = inst["instrument_token"]

            sectors = {}
            for idx_name, key in SECTOR_INDICES:
                token = idx_map.get(idx_name)
                if not token:
                    continue
                try:
                    quotes = kite.quote([f"NSE:{token}"])
                    q = quotes.get(f"NSE:{token}", {})
                    if q:
                        sectors[key] = {
                            "close": float(q.get("last_price", 0)),
                            "change_pct": float(q.get("ohlc", {}).get("close", 0)),
                        }
                except Exception:
                    continue

            if sectors:
                sources_used.append("kiteconnect")
                log.info("hawk_sectors_fetched", source="kiteconnect", count=len(sectors))
                return sectors, sources_used
        except Exception as exc:
            log.warning("hawk_sectors_kite_failed", error=str(exc))

    # Fallback: nsetools
    try:
        from nsetools import Nse
        nse = Nse()
        sectors = {}
        for idx_name, key in SECTOR_INDICES:
            try:
                q = nse.get_index_quote(idx_name)
                if q:
                    sectors[key] = {
                        "close": float(q.get("lastPrice", q.get("last", 0))),
                        "change_pct": float(q.get("pChange", q.get("percentChange", 0))),
                    }
            except Exception:
                continue
        if sectors:
            sources_used.append("nsetools")
            log.info("hawk_sectors_fetched", source="nsetools", count=len(sectors))
            return sectors, sources_used
    except Exception as exc:
        log.warning("hawk_sectors_failed", error=str(exc))

    return {}, sources_used


def _derive_movers(bhavcopy: list[dict], top_n: int = 5) -> tuple[list[dict], list[dict]]:
    """Derive top gainers and losers from bhavcopy data."""
    if not bhavcopy:
        return [], []
    sorted_by_change = sorted(bhavcopy, key=lambda x: x.get("change_pct", 0))
    losers = sorted_by_change[:top_n]
    gainers = list(reversed(sorted_by_change[-top_n:]))
    return gainers, losers


def _derive_unusual_delivery(
    bhavcopy: list[dict], threshold: float = 1.5
) -> list[dict]:
    """
    Flag stocks with unusually high delivery percentage.

    For MVP: flag if delivery_pct > threshold * 50 (rough avg baseline).
    Future: compare against 20-day rolling average.
    """
    unusual = []
    baseline = 50.0  # Rough NIFTY 50 average delivery %
    for stock in bhavcopy:
        dpct = stock.get("delivery_pct", 0)
        if dpct > baseline * threshold:
            unusual.append({
                "symbol": stock["symbol"],
                "delivery_pct": dpct,
                "close": stock.get("close", 0),
                "change_pct": stock.get("change_pct", 0),
                "ratio": round(dpct / baseline, 2) if baseline > 0 else 0,
            })
    return sorted(unusual, key=lambda x: x.get("ratio", 0), reverse=True)


def collect_evening_data(
    target_date: date | None = None,
    hawk_config: dict | None = None,
) -> dict:
    """
    Collect all Tier 1 data for evening analysis.

    Creates a shared KiteConnect instance once and passes it to all fetch functions.
    Falls back to nsetools/nsepython/hardcoded if KiteConnect auth fails.

    Args:
        target_date: Date to collect data for. Defaults to today.
        hawk_config: HAWK config dict (from config/hawk.yaml).

    Returns:
        Structured dict with all available data + metadata about sources used.
    """
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

    if target_date is None:
        target_date = datetime.now(IST).date()
    if hawk_config is None:
        hawk_config = {}

    delivery_threshold = hawk_config.get("delivery_threshold", 1.5)

    all_sources: list[str] = []
    all_fallbacks: list[str] = []

    # Shared KiteConnect instance — created once, used everywhere
    kite = _init_kiteconnect()
    kite_instruments: list[dict] | None = None
    token_map: dict[str, int] | None = None

    if kite:
        try:
            kite_instruments = kite.instruments("NSE")
            token_map = {
                i["tradingsymbol"]: i["instrument_token"]
                for i in kite_instruments
                if i.get("instrument_type") == "EQ"
            }
            log.info("hawk_instruments_loaded", count=len(token_map))
        except Exception as exc:
            log.warning("hawk_instruments_failed", error=str(exc))
            kite = None  # Disable kite for subsequent calls

    # 1. NIFTY 50 stock list
    stocks = _fetch_nifty50_stocks(kite_instruments)

    # 2. Bhavcopy
    bhavcopy, bhav_sources = _fetch_bhavcopy(target_date, stocks, kite, token_map)
    all_sources.extend(bhav_sources)
    if "kiteconnect" in bhav_sources and "nsepython" not in bhav_sources:
        all_fallbacks.append("bhavcopy:no_delivery_pct")
    if not bhav_sources:
        all_fallbacks.append("bhavcopy:all_failed")

    # 3. FII/DII
    fii_dii, fii_sources = _fetch_fii_dii(target_date)
    all_sources.extend(fii_sources)
    if not fii_sources:
        all_fallbacks.append("fii_dii:zeros")

    # 4. Indices
    indices, idx_sources = _fetch_indices(kite)
    all_sources.extend(idx_sources)
    if not idx_sources:
        all_fallbacks.append("indices:empty")

    # 5. Sectors
    sectors, sec_sources = _fetch_sectors(kite, kite_instruments)
    all_sources.extend(sec_sources)
    if not sec_sources:
        all_fallbacks.append("sectors:empty")

    # 6. Derived data
    top_gainers, top_losers = _derive_movers(bhavcopy)
    unusual_delivery = _derive_unusual_delivery(bhavcopy, delivery_threshold)

    # Deduplicate sources
    unique_sources = list(dict.fromkeys(all_sources))

    result = {
        "date": target_date.isoformat(),
        "bhavcopy": bhavcopy,
        "fii_dii": fii_dii,
        "indices": indices,
        "sectors": sectors,
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "unusual_delivery": unusual_delivery,
        "regime": "unknown",  # Populated by caller if TradeOS state available
        "metadata": {
            "data_sources": unique_sources,
            "fallbacks_used": all_fallbacks,
            "stocks_in_universe": len(stocks),
            "bhavcopy_count": len(bhavcopy),
        },
    }

    log.info(
        "hawk_data_collected",
        date=target_date.isoformat(),
        bhavcopy_count=len(bhavcopy),
        sources=unique_sources,
        fallbacks=all_fallbacks,
    )

    return result
