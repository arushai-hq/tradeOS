"""
HAWK — Tier 1 Data Collector.

Fetches NSE market data for evening/morning analysis.
Priority: nsepython → nsetools → hardcoded fallback.
Never crashes — partial data is better than no data.
"""
from __future__ import annotations

from datetime import date, datetime

import structlog

log = structlog.get_logger()


def _fetch_nifty50_stocks(source: str) -> list[str]:
    """Get NIFTY 50 constituent list."""
    if source == "nsetools":
        try:
            from nsetools import Nse
            nse = Nse()
            stocks = nse.get_index_stock_list("NIFTY 50")
            if stocks and len(stocks) >= 40:
                log.info("hawk_nifty50_fetched", source="nsetools", count=len(stocks))
                return stocks
        except Exception as exc:
            log.warning("hawk_nifty50_nsetools_failed", error=str(exc))

    # Fallback: hardcoded list
    from tools.hawk_engine.config import NIFTY_50_STOCKS
    log.info("hawk_nifty50_fetched", source="hardcoded", count=len(NIFTY_50_STOCKS))
    return NIFTY_50_STOCKS


def _fetch_bhavcopy(target_date: date, stocks: list[str]) -> tuple[list[dict], list[str]]:
    """
    Fetch bhavcopy data for given stocks.

    Returns:
        (bhavcopy_list, sources_used)
    """
    sources_used: list[str] = []

    # Try nsepython
    try:
        from nsepython import nse_eq_symbols
        bhav = nse_eq_symbols(target_date.strftime("%d-%m-%Y"), "EQ")
        if bhav is not None and not bhav.empty:
            result = []
            for _, row in bhav.iterrows():
                symbol = str(row.get("SYMBOL", ""))
                if symbol not in stocks:
                    continue
                result.append({
                    "symbol": symbol,
                    "open": float(row.get("OPEN_PRICE", row.get("OPEN", 0))),
                    "high": float(row.get("HIGH_PRICE", row.get("HIGH", 0))),
                    "low": float(row.get("LOW_PRICE", row.get("LOW", 0))),
                    "close": float(row.get("CLOSE_PRICE", row.get("CLOSE", 0))),
                    "volume": int(row.get("TTL_TRD_QNTY", row.get("TOTAL_TRADED_QUANTITY", 0))),
                    "delivery_pct": float(row.get("DELIV_PER", row.get("DELIVERY_PERCENTAGE", 0))),
                    "change_pct": float(row.get("NET_TRDVAL", 0)),
                })
            if result:
                # Compute change_pct from OHLC if not available
                for item in result:
                    if item["open"] > 0:
                        item["change_pct"] = round(
                            (item["close"] - item["open"]) / item["open"] * 100, 2
                        )
                sources_used.append("nsepython")
                log.info("hawk_bhavcopy_fetched", source="nsepython", count=len(result))
                return result, sources_used
    except Exception as exc:
        log.warning("hawk_bhavcopy_nsepython_failed", error=str(exc))

    # Fallback: KiteConnect (if available)
    try:
        from tools.hawk_engine.config import load_secrets
        secrets = load_secrets()
        api_key = secrets.get("zerodha", {}).get("api_key", "")
        access_token = secrets.get("zerodha", {}).get("access_token", "")
        if api_key and access_token:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=api_key)
            kite.set_access_token(access_token)
            instruments = kite.instruments("NSE")
            token_map = {i["tradingsymbol"]: i["instrument_token"] for i in instruments if i["tradingsymbol"] in stocks}

            result = []
            from_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            to_dt = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
            for symbol, token in token_map.items():
                try:
                    data = kite.historical_data(token, from_dt, to_dt, "day")
                    if data:
                        d = data[-1]
                        result.append({
                            "symbol": symbol,
                            "open": float(d["open"]),
                            "high": float(d["high"]),
                            "low": float(d["low"]),
                            "close": float(d["close"]),
                            "volume": int(d["volume"]),
                            "delivery_pct": 0.0,  # KiteConnect doesn't provide delivery %
                            "change_pct": round((float(d["close"]) - float(d["open"])) / float(d["open"]) * 100, 2) if float(d["open"]) > 0 else 0.0,
                        })
                except Exception:
                    continue
            if result:
                sources_used.append("kiteconnect")
                log.info("hawk_bhavcopy_fetched", source="kiteconnect", count=len(result))
                return result, sources_used
    except Exception as exc:
        log.warning("hawk_bhavcopy_kite_failed", error=str(exc))

    log.error("hawk_bhavcopy_all_sources_failed")
    return [], sources_used


def _fetch_fii_dii(target_date: date) -> tuple[dict, list[str]]:
    """Fetch FII/DII flow data."""
    sources_used: list[str] = []

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
    except Exception as exc:
        log.warning("hawk_fii_dii_failed", error=str(exc))

    return {"fii_net_equity": 0.0, "dii_net_equity": 0.0}, sources_used


def _fetch_indices() -> tuple[dict, list[str]]:
    """Fetch major indices — Nifty 50, Bank Nifty, India VIX."""
    sources_used: list[str] = []

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


def _fetch_sectors() -> tuple[dict, list[str]]:
    """Fetch sector index performance."""
    sources_used: list[str] = []

    sector_indices = [
        ("NIFTY IT", "nifty_it"),
        ("NIFTY BANK", "nifty_bank"),
        ("NIFTY PHARMA", "nifty_pharma"),
        ("NIFTY AUTO", "nifty_auto"),
        ("NIFTY METAL", "nifty_metal"),
        ("NIFTY REALTY", "nifty_realty"),
        ("NIFTY ENERGY", "nifty_energy"),
        ("NIFTY FMCG", "nifty_fmcg"),
    ]

    try:
        from nsetools import Nse
        nse = Nse()
        sectors = {}
        for idx_name, key in sector_indices:
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

    nifty50_source = hawk_config.get("nifty50_source", "nsetools")
    delivery_threshold = hawk_config.get("delivery_threshold", 1.5)

    all_sources: list[str] = []
    all_fallbacks: list[str] = []

    # 1. NIFTY 50 stock list
    stocks = _fetch_nifty50_stocks(nifty50_source)

    # 2. Bhavcopy
    bhavcopy, bhav_sources = _fetch_bhavcopy(target_date, stocks)
    all_sources.extend(bhav_sources)
    if "nsepython" not in bhav_sources and "kiteconnect" in bhav_sources:
        all_fallbacks.append("bhavcopy:kiteconnect")

    # 3. FII/DII
    fii_dii, fii_sources = _fetch_fii_dii(target_date)
    all_sources.extend(fii_sources)

    # 4. Indices
    indices, idx_sources = _fetch_indices()
    all_sources.extend(idx_sources)

    # 5. Sectors
    sectors, sec_sources = _fetch_sectors()
    all_sources.extend(sec_sources)

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
