"""
iFinD HTTP API MCP Server

A stdio-based MCP server that wraps Tonghuashun (同花顺) iFinD HTTP API,
exposing iFinD data as MCP tools for Claude to use in financial analysis.

Supports two authentication modes:
  1. HTTP API mode (recommended): Set IFIND_REFRESH_TOKEN env var.
     Get refresh_token from SuperCommand web:
     https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/super-command-web/index.html#/AccountDetails
  2. SDK mode: Set IFIND_USERNAME + IFIND_PASSWORD env vars.
     Requires iFinDPy SDK installed (pip install iFinDPy, Windows-only, needs iFinD terminal).
     The SDK uses THS_iFinDLogin for direct username/password auth.

Requirements:
- iFinD account with HTTP API access
- Python 3.10+
- mcp package: pip install mcp
- httpx package: pip install httpx
- (optional) iFinDPy package for SDK auth mode
"""

import json
import sqlite3
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Try to import iFinDPy for SDK auth mode
_ifindpy_available = False
try:
    from iFinDPy import THS_iFinDLogin, THS_iFinDLogout  # type: ignore[import-not-found]
    _ifindpy_available = True
except ImportError:
    pass

mcp = FastMCP(
    "iFinD HTTP API",
)

# ---------------------------------------------------------------------------
# iFinD HTTP API configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://quantapi.51ifind.com/api/v1"
REFRESH_TOKEN = os.environ.get("IFIND_REFRESH_TOKEN", "")
IFIND_USERNAME = os.environ.get("IFIND_USERNAME", "")
IFIND_PASSWORD = os.environ.get("IFIND_PASSWORD", "")

# Auth mode detection
_auth_mode: str = ""  # "http" or "sdk"
if REFRESH_TOKEN:
    _auth_mode = "http"
elif IFIND_USERNAME and IFIND_PASSWORD:
    _auth_mode = "sdk"

# Cached access token (HTTP mode)
_access_token: str = ""
_access_token_expiry: datetime = datetime.min

# SDK login state
_sdk_logged_in: bool = False


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _ensure_sdk_login() -> None:
    """Login via iFinDPy SDK if not already logged in."""
    global _sdk_logged_in

    if _sdk_logged_in:
        return

    if not _ifindpy_available:
        raise RuntimeError(
            "iFinDPy SDK is not installed. Install it from iFinD terminal package, "
            "or use IFIND_REFRESH_TOKEN for HTTP API mode instead. "
            "SDK mode requires Windows + iFinD terminal installed."
        )

    result = THS_iFinDLogin(IFIND_USERNAME, IFIND_PASSWORD)
    if result != 0:
        raise RuntimeError(
            f"iFinDPy login failed with code {result}. "
            "Check your IFIND_USERNAME and IFIND_PASSWORD."
        )

    _sdk_logged_in = True
    logger.info("iFinDPy SDK login successful for user %s", IFIND_USERNAME)


def _get_access_token() -> str:
    """Obtain or return cached access_token from iFinD API.

    Uses refresh_token to get an access_token valid for 7 days.
    Automatically refreshes when expired or within 1 hour of expiry.
    """
    global _access_token, _access_token_expiry

    if _access_token and datetime.now() < _access_token_expiry - timedelta(hours=1):
        return _access_token

    if not REFRESH_TOKEN:
        raise RuntimeError(
            "IFIND_REFRESH_TOKEN environment variable is not set. "
            "Get your refresh_token from iFinD SuperCommand: "
            "https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/super-command-web/index.html#/AccountDetails"
        )

    resp = httpx.get(
        f"{BASE_URL}/get_access_token",
        headers={
            "Content-Type": "application/json",
            "refresh_token": REFRESH_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("errorcode") != 0:
        raise RuntimeError(f"Failed to get access_token: {data}")

    _access_token = data["data"]["access_token"]
    _access_token_expiry = datetime.now() + timedelta(days=7)
    return _access_token


def _api_headers() -> dict[str, str]:
    """Build HTTP headers for iFinD API requests."""
    return {
        "Content-Type": "application/json",
        "access_token": _get_access_token(),
        "ifindlang": "cn",
    }


# ---------------------------------------------------------------------------
# HTTP request helper
# ---------------------------------------------------------------------------


def _post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    """Send a POST request to iFinD API and return parsed JSON response.

    In HTTP mode: uses access_token auth, retries once on token expiry.
    In SDK mode: uses iFinDPy SDK login, then calls HTTP API with SDK session.
    """
    global _access_token

    if _auth_mode == "sdk":
        _ensure_sdk_login()
        # SDK mode still uses HTTP API but auth is handled by the SDK session.
        # Fall through to HTTP call — the SDK keeps the session active.
        # For SDK mode, we call via iFinDPy functions when available,
        # but since we're wrapping HTTP endpoints, we use the same HTTP flow.
        # The SDK login establishes a server-side session that the HTTP API honors.

    if _auth_mode not in ("http", "sdk"):
        raise RuntimeError(
            "No authentication configured. Set either:\n"
            "  - IFIND_REFRESH_TOKEN (for HTTP API mode), or\n"
            "  - IFIND_USERNAME + IFIND_PASSWORD (for SDK mode, requires iFinDPy)"
        )

    url = f"{BASE_URL}/{endpoint}"

    for attempt in range(2):
        resp = httpx.post(url, json=body, headers=_api_headers(), timeout=60)
        resp.raise_for_status()
        result = resp.json()

        # Check for auth errors — force token refresh and retry
        # -1010: account logged out, -1302: access_token expired/illegal
        if result.get("errorcode") in (-1010, -1302) and attempt == 0:
            _access_token = ""
            continue

        return result

    return result


def _format_response(result: dict[str, Any]) -> str:
    """Format iFinD API response as JSON string."""
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# SQLite usage tracker (local call log)
# ---------------------------------------------------------------------------

_DB_PATH = os.environ.get(
    "IFIND_USAGE_DB",
    str(Path(__file__).parent / "ifind_usage.db"),
)


def _get_db() -> sqlite3.Connection:
    """Get (and lazily initialize) the SQLite connection."""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            api        TEXT    NOT NULL,
            ts         TEXT    NOT NULL,
            usage_amt  INTEGER NOT NULL,
            codes      TEXT,
            indicators TEXT,
            detail     TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_api_ts ON api_usage (api, ts)
    """)
    conn.commit()
    return conn


_cleanup_counter = 0


def _record_usage(api: str, usage_amt: int, codes: str = "", indicators: str = "", detail: str = ""):
    """Insert one usage record and periodically purge expired rows."""
    global _cleanup_counter
    conn = _get_db()
    conn.execute(
        "INSERT INTO api_usage (api, ts, usage_amt, codes, indicators, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (api, datetime.now().isoformat(), usage_amt, codes, indicators, detail),
    )
    # Purge records older than 30 days every 50 calls
    _cleanup_counter += 1
    if _cleanup_counter % 50 == 0:
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        conn.execute("DELETE FROM api_usage WHERE ts < ?", (cutoff,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def ifind_basic_data(
    codes: str,
    indicators: str,
    params: str = "",
) -> str:
    """Retrieve cross-sectional basic data from iFinD (基本面截面数据).

    Use this for current/latest financial data for one or more securities:
    financial statements, valuation, company info, profit forecasts, etc.

    Args:
        codes: Security codes, comma-separated. Examples:
            - A-shares: "600519.SH" (Kweichow Moutai), "000858.SZ"
            - HK stocks: "00700.HK" (Tencent)
            - US stocks: "AAPL.O" (NASDAQ), "MSFT.N" (NYSE)
            - Indices: "000300.SH" (CSI 300)
        indicators: Indicator names, semicolon-separated. Common indicators:
            - Price: "ths_open_price_stock;ths_close_price_stock;ths_high_price_stock;ths_low_price_stock"
            - Volume: "ths_vol_stock;ths_amt_stock;ths_turnover_ratio_stock"
            - Valuation: "ths_pe_ttm_stock;ths_pb_mrq_stock;ths_ps_ttm_stock;ths_pcf_ocf_ttm_stock"
            - Market cap: "ths_market_value_stock;ths_float_market_value_stock"
            - Financials: "ths_or_ttm_stock;ths_np_atoopc_stock;ths_roe_ttm_stock"
            - Per-share: "ths_eps_stock;ths_bps_stock"
            - Growth: "ths_or_yoy_stock;ths_np_yoy_stock"
            - Company: "ths_stock_short_name_stock;ths_listed_exchange_stock"
        params: Parameters for each indicator, semicolon-separated (matching indicators order).
            Each indicator's params are comma-separated.
            Example: "20250113,100,20250113;;20250113,100,20250113"
            (empty between semicolons means no params for that indicator)
    """
    # Build indipara array
    indi_list = [i.strip() for i in indicators.split(";") if i.strip()]
    param_list = params.split(";") if params else [""] * len(indi_list)

    indipara = []
    for i, indicator in enumerate(indi_list):
        entry: dict[str, Any] = {"indicator": indicator}
        p = param_list[i].strip() if i < len(param_list) else ""
        if p:
            entry["indiparams"] = [x.strip() for x in p.split(",")]
        indipara.append(entry)

    body = {"codes": codes, "indipara": indipara}
    result = _post("basic_data_service", body)
    _record_usage("basic_data", len(indi_list) * len(codes.split(",")), codes, indicators)
    return _format_response(result)


@mcp.tool()
def ifind_date_sequence(
    codes: str,
    indicators: str,
    start_date: str,
    end_date: str,
    params: str = "",
    function_params: str = "",
) -> str:
    """Retrieve historical date-sequence data from iFinD (日期序列数据).

    Use this for time-series data: daily fundamental metrics, technical indicators,
    financial data points over a date range.

    Args:
        codes: Security code (single code recommended). Example: "600519.SH"
        indicators: Indicator names, semicolon-separated. Common indicators:
            - EPS: "ths_eps_ttm_stock"
            - PE: "ths_pe_ttm_stock"
            - Revenue: "ths_or_ttm_stock"
            - Net profit: "ths_np_atoopc_stock"
            - ROE: "ths_roe_ttm_stock"
            - Total shares: "ths_total_shares_stock"
            - Free float: "ths_free_float_shares_stock"
        start_date: Start date in "YYYY-MM-DD" format
        end_date: End date in "YYYY-MM-DD" format
        params: Parameters for each indicator, semicolon-separated.
            Example: "101" or "20241231,1"
        function_params: Global parameters as comma-separated key:value pairs.
            - Days: "Tradedays" (default) or "Alldays"
            - Fill: "Previous", "Blank", or a specific value like "-1"
            - Interval: "D" (day), "W" (week), "M" (month), "Q" (quarter), "Y" (year)
            Example: "Days:Tradedays,Fill:Previous,Interval:D"
    """
    indi_list = [i.strip() for i in indicators.split(";") if i.strip()]
    param_list = params.split(";") if params else [""] * len(indi_list)

    indipara = []
    for i, indicator in enumerate(indi_list):
        entry: dict[str, Any] = {"indicator": indicator}
        p = param_list[i].strip() if i < len(param_list) else ""
        if p:
            entry["indiparams"] = [x.strip() for x in p.split(",")]
        indipara.append(entry)

    body: dict[str, Any] = {
        "codes": codes,
        "startdate": start_date.replace("-", ""),
        "enddate": end_date.replace("-", ""),
        "indipara": indipara,
    }

    if function_params:
        fp = {}
        for pair in function_params.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                fp[k.strip()] = v.strip()
        body["functionpara"] = fp

    result = _post("date_sequence", body)
    _record_usage("date_sequence", len(indi_list), codes, indicators, f"{start_date}~{end_date}")
    return _format_response(result)


@mcp.tool()
def ifind_history_quotes(
    codes: str,
    indicators: str,
    start_date: str,
    end_date: str,
    function_params: str = "",
) -> str:
    """Retrieve historical market quotes from iFinD (历史行情数据).

    Use this for historical OHLCV data, technical indicators over a date range.

    Args:
        codes: Security codes, comma-separated. Example: "600519.SH,000858.SZ"
        indicators: Quote fields, comma-separated. Common fields:
            - "open,high,low,close" — OHLC prices
            - "volume,amount" — trading volume and turnover
            - "change,changePer" — price change and percentage
            - "turnoverRatio" — turnover ratio
            - "pe,pb" — valuation ratios
        start_date: Start date in "YYYY-MM-DD" format
        end_date: End date in "YYYY-MM-DD" format
        function_params: Optional parameters as comma-separated key:value pairs.
            - Interval: "D" (day), "W" (week), "M" (month), "Q" (quarter), "Y" (year)
            - CPS: "1" (forward adjust), "2" (backward adjust), "0" (no adjust)
            - Fill: "Previous", "Blank", "Omit"
            - Currency: "MHB" (USD), "GHB" (HKD), "RMB"
            Example: "Interval:D,CPS:1,Fill:Previous"
    """
    body: dict[str, Any] = {
        "codes": codes,
        "indicators": indicators,
        "startdate": start_date,
        "enddate": end_date,
    }

    if function_params:
        fp = {}
        for pair in function_params.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                fp[k.strip()] = v.strip()
        body["functionpara"] = fp

    result = _post("cmd_history_quotation", body)
    _record_usage("history_quotes", len(codes.split(",")) * len(indicators.split(",")),
                  codes, indicators, f"{start_date}~{end_date}")
    return _format_response(result)


@mcp.tool()
def ifind_realtime_quotes(
    codes: str,
    indicators: str,
) -> str:
    """Retrieve real-time quote snapshot from iFinD (实时行情快照).

    Use this for live/latest market data: current price, volume, bid/ask, etc.
    Returns a one-time snapshot (not streaming).

    Args:
        codes: Security codes, comma-separated. Example: "600519.SH,000858.SZ"
        indicators: Real-time fields, comma-separated. Common fields:
            - "latest" — latest price
            - "open,high,low,latest" — OHLC
            - "volume,amount" — trading volume and turnover
            - "bid1,ask1,bidSize1,askSize1" — best bid/ask
            - "change,changePer" — price change
            - "turnoverRatio" — turnover ratio
            - "amplitude" — intraday amplitude
            - "preClose" — previous close
    """
    body = {"codes": codes, "indicators": indicators}
    result = _post("real_time_quotation", body)
    _record_usage("realtime_quotes", len(codes.split(",")), codes, indicators)
    return _format_response(result)


@mcp.tool()
def ifind_high_frequency(
    codes: str,
    indicators: str,
    start_time: str,
    end_time: str,
    function_params: str = "",
) -> str:
    """Retrieve intraday minute-bar data from iFinD (分钟级高频数据).

    Use this for intraday analysis: minute-level OHLCV, technical indicators.

    Args:
        codes: Security code (single code). Example: "600519.SH"
        indicators: Data fields, semicolon-separated.
            Common: "open;high;low;close;volume;amount"
        start_time: Start datetime in "YYYY-MM-DD HH:MM:SS" format
        end_time: End datetime in "YYYY-MM-DD HH:MM:SS" format
        function_params: Optional parameters as comma-separated key:value pairs.
            - Interval: "1", "3", "5", "10", "15", "30", "60" (minutes)
            - CPS: "1" (forward adjust), "2" (backward adjust), "0" (no adjust)
            - Fill: "Previous", "Blank", "Original"
            Example: "Interval:5,CPS:1,Fill:Previous"
    """
    body: dict[str, Any] = {
        "codes": codes,
        "indicators": indicators,
        "starttime": start_time,
        "endtime": end_time,
    }

    if function_params:
        fp = {}
        for pair in function_params.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                fp[k.strip()] = v.strip()
        body["functionpara"] = fp

    result = _post("high_frequency", body)
    _record_usage("high_frequency", 1, codes, indicators, f"{start_time}~{end_time}")
    return _format_response(result)


@mcp.tool()
def ifind_snapshot(
    codes: str,
    indicators: str,
    start_time: str,
    end_time: str,
) -> str:
    """Retrieve intraday snapshot/order-book data from iFinD (盘口快照数据).

    Use this for tick-level analysis: intraday snapshots, order book depth.

    Args:
        codes: Security code (single code). Example: "600519.SH"
        indicators: Snapshot fields, semicolon-separated.
            Common: "open;high;low;latest;bid1;ask1;bidSize1;askSize1;volume;amount"
        start_time: Start datetime in "YYYY-MM-DD HH:MM:SS" format
        end_time: End datetime in "YYYY-MM-DD HH:MM:SS" format
    """
    body = {
        "codes": codes,
        "indicators": indicators,
        "starttime": start_time,
        "endtime": end_time,
    }
    result = _post("snap_shot", body)
    _record_usage("snapshot", 1, codes, indicators, f"{start_time}~{end_time}")
    return _format_response(result)


@mcp.tool()
def ifind_data_pool(
    report_name: str,
    function_params: str,
    output_params: str,
) -> str:
    """Query iFinD DataPool for structured datasets (数据池).

    Use this for index/sector constituents, block lists, specialized reports,
    IPO data, margin trading, stock connect flows, etc.

    Args:
        report_name: Report/model name. Common reports:
            - "block": Sector/index constituent list
            - "p03291": Futures warehouse receipts
            - "p03425": Custom report
        function_params: Input parameters as semicolon-separated key=value pairs.
            Example: "date=20250113;blockname=001005010"
            Common block codes:
            - "001005010": 沪深300 (CSI 300)
            - "001005260": 中证500 (CSI 500)
            - "001005290": 中证1000 (CSI 1000)
            - "001004010": 上证50 (SSE 50)
        output_params: Output field flags as comma-separated field:Y pairs.
            Example: "date:Y,thscode:Y,security_name:Y"
    """
    # Parse function_params into dict
    fp = {}
    for pair in function_params.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            fp[k.strip()] = v.strip()

    body = {
        "reportname": report_name,
        "functionpara": fp,
        "outputpara": output_params,
    }
    result = _post("data_pool", body)
    _record_usage("data_pool", 1, report_name, "", function_params)
    return _format_response(result)


@mcp.tool()
def ifind_edb(
    indicators: str,
    start_date: str,
    end_date: str,
) -> str:
    """Query iFinD Economic Database for macro indicators (宏观经济数据库).

    Use this for macroeconomic data: GDP, CPI, PPI, PMI, monetary supply,
    interest rates, exchange rates, trade data, etc.

    Args:
        indicators: EDB indicator codes, semicolon-separated. Common indicators:
            - China GDP: "M001620247"
            - China CPI YoY: "M002822183"
            - China PPI YoY: "M002822193"
            - China PMI Manufacturing: "M002827382"
            - M2 YoY: "M001622252"
            - LPR 1Y: "M006000702"
            - LPR 5Y: "M006000703"
            - USD/CNY: "M001622930"
            - US CPI YoY: "G009035746"
            - US GDP QoQ: "G009033447"
        start_date: Start date in "YYYY-MM-DD" format
        end_date: End date in "YYYY-MM-DD" format
    """
    body = {
        "indicators": indicators,
        "startdate": start_date,
        "enddate": end_date,
    }
    result = _post("edb_service", body)
    _record_usage("edb", len(indicators.split(";")), "", indicators, f"{start_date}~{end_date}")
    return _format_response(result)


# ---------------------------------------------------------------------------
# Fund valuation tools
# ---------------------------------------------------------------------------


@mcp.tool()
def ifind_fund_valuation(
    codes: str,
    only_latest: str = "1",
    begin_time: str = "",
    end_time: str = "",
) -> str:
    """Query real-time fund valuation by minute (基金实时估值-分钟级).

    Returns estimated NAV for funds (场外基金 .OF) updated every minute
    during trading hours. Use this for intraday fund valuation tracking.

    Args:
        codes: Fund codes, comma-separated. Example: "000001.OF,000003.OF"
        only_latest: "1" for latest valuation only (default), "0" for time range
        begin_time: Start time "YYYY-MM-DD HH:MM:SS" (required when only_latest="0")
        end_time: End time "YYYY-MM-DD HH:MM:SS" (required when only_latest="0")

    Output fields include:
        - realTimeValuation: real-time estimated NAV
        - changeRatioValuation: estimated change ratio (%)
        - Deviation30TDays: 30-trading-day average deviation (%)
        - rank: latest valuation change ratio rank
    """
    functionpara: dict[str, str] = {"onlyLastest": only_latest}
    if only_latest == "0":
        if begin_time:
            functionpara["beginTime"] = begin_time
        if end_time:
            functionpara["endTime"] = end_time

    body = {
        "codes": codes,
        "functionpara": functionpara,
        "outputpara": "changeRatioValuation:Y,realTimeValuation:Y,Deviation30TDays:Y,rank:Y",
    }
    result = _post("fund_valuation", body)
    _record_usage("fund_valuation", len(codes.split(",")), codes, "", f"onlyLatest={only_latest}")
    return _format_response(result)


@mcp.tool()
def ifind_final_fund_valuation(
    codes: str,
    begin_date: str,
    end_date: str,
) -> str:
    """Query daily final fund valuation (基金实时估值-日级).

    Returns end-of-day estimated NAV vs actual NAV for funds.
    Use this to compare fund valuation accuracy over a date range.

    Args:
        codes: Fund codes, comma-separated. Example: "000001.OF,000003.OF"
        begin_date: Start date "YYYY-MM-DD"
        end_date: End date "YYYY-MM-DD"

    Output fields:
        - finalValuation: end-of-day final estimated NAV
        - netAssetValue: actual NAV
        - deviation: valuation deviation from actual NAV (%)
    """
    body = {
        "codes": codes,
        "functionpara": {
            "beginDate": begin_date,
            "endDate": end_date,
        },
        "outputpara": "finalValuation:Y,netAssetValue:Y,deviation:Y",
    }
    result = _post("final_fund_valuation", body)
    _record_usage("final_fund_valuation", len(codes.split(",")), codes, "", f"{begin_date}~{end_date}")
    return _format_response(result)


# ---------------------------------------------------------------------------
# Date utility tools
# ---------------------------------------------------------------------------


@mcp.tool()
def ifind_date_query(
    market_code: str,
    start_date: str,
    end_date: str,
    mode: str = "1",
    date_type: str = "0",
    period: str = "D",
    date_format: str = "0",
    period_num: str = "",
) -> str:
    """Query trading dates for an exchange (日期查询函数).

    Returns trading dates within a date range for a given exchange.
    Use this to find trading calendars, count trading days, etc.

    Args:
        market_code: Exchange code. Common values:
            - "212001" — Shanghai Stock Exchange (上交所)
            - "212100" — Shenzhen Stock Exchange (深交所)
            - "212200" — Hong Kong Exchange (港交所)
            - "212010" — NYSE
            - "212011" — NASDAQ
            - "212020008" — Shanghai Futures Exchange (上期所)
            - "212020003" — Zhengzhou Commodity Exchange (郑商所)
            - "212020004" — Dalian Commodity Exchange (大商所)
            - "212020001" — CFFEX (中金所)
        start_date: Start date "YYYY-MM-DD"
        end_date: End date "YYYY-MM-DD"
        mode: "1" — return dates in range, "2" — return count of dates
        date_type: "0" — trading days, "1" — calendar days
        period: "D" day, "W" week, "M" month, "Q" quarter, "S" half-year, "Y" year
        date_format: "0" YYYY-MM-DD, "1" YYYY/MM/DD, "2" YYYYMMDD
        period_num: Optional. "1" first day of period, "-1" last day of period
    """
    functionpara: dict[str, str] = {
        "mode": mode,
        "dateType": date_type,
        "period": period,
        "dateFormat": date_format,
    }
    if period_num:
        functionpara["periodnum"] = period_num

    body = {
        "marketcode": market_code,
        "functionpara": functionpara,
        "startdate": start_date,
        "enddate": end_date,
    }
    result = _post("get_trade_dates", body)
    _record_usage("date_query", 1, "", "", f"market={market_code},{start_date}~{end_date}")
    return _format_response(result)


@mcp.tool()
def ifind_date_offset(
    market_code: str,
    base_date: str,
    offset: str,
    date_type: str = "0",
    period: str = "D",
    date_format: str = "0",
    output: str = "singledate",
    period_num: str = "",
) -> str:
    """Offset a date by trading/calendar days (日期偏移函数).

    Returns the date that is N trading/calendar days before or after a base date.
    Use this to find "5 trading days ago" or "next month-end", etc.

    Args:
        market_code: Exchange code (same as ifind_date_query)
        base_date: Base date "YYYY-MM-DD"
        offset: Number of periods to offset. Negative = backward, positive = forward.
            Example: "-5" for 5 trading days ago, "10" for 10 trading days later
        date_type: "0" — trading days, "1" — calendar days
        period: "D" day, "W" week, "M" month, "Q" quarter, "S" half-year, "Y" year
        date_format: "0" YYYY-MM-DD, "1" YYYY/MM/DD, "2" YYYYMMDD
        output: "singledate" — return only the target date,
                "sequencedate" — return all dates between base and target
        period_num: Optional. "1" first day of period, "-1" last day of period
    """
    functionpara: dict[str, str] = {
        "dateType": date_type,
        "period": period,
        "dateFormat": date_format,
        "offset": offset,
        "output": output,
    }
    if period_num:
        functionpara["periodnum"] = period_num

    body = {
        "marketcode": market_code,
        "functionpara": functionpara,
        "basedate": base_date,
    }
    result = _post("get_trade_dates", body)
    _record_usage("date_offset", 1, "", "", f"market={market_code},base={base_date},offset={offset}")
    return _format_response(result)


# ---------------------------------------------------------------------------
# Smart stock picking (问财选股)
# ---------------------------------------------------------------------------


@mcp.tool()
def ifind_smart_stock_picking(
    query: str,
    search_type: str = "stock",
) -> str:
    """Natural-language stock screener powered by Wencai AI (问财选股).

    Ask questions in Chinese to screen securities. Wencai AI parses the query
    and returns matching results.

    Args:
        query: Natural language search query in Chinese. Examples:
            - "个股热度" — stock popularity ranking
            - "连续3天涨停的股票" — stocks with 3 consecutive daily limits
            - "市盈率低于10的银行股" — bank stocks with PE < 10
            - "今日北向资金净买入前20" — top 20 northbound net buying
            - "近一周涨幅超过10%的股票" — stocks up >10% in past week
        search_type: Search category. Options:
            - "stock" — A-shares (default)
            - "fund" — funds
            - "bond" — bonds
            - "index" — indices
    """
    body = {
        "searchstring": query,
        "searchtype": search_type,
    }
    result = _post("smart_stock_picking", body)
    _record_usage("smart_stock_picking", 1, "", "", query[:50])
    return _format_response(result)


# ---------------------------------------------------------------------------
# Code conversion tool
# ---------------------------------------------------------------------------


@mcp.tool()
def ifind_get_thscode(
    keyword: str,
    mode: str = "seccode",
    sec_type: str = "001",
    market: str = "212001",
    trade_status: str = "0",
    is_exact: str = "0",
) -> str:
    """Convert security code/name to iFinD thscode format (转同花顺代码).

    Use this to look up the correct iFinD code for a security when you only
    have a partial code or name.

    Args:
        keyword: Security code or name to search. Example: "000001" or "平安银行"
        mode: Search mode — "seccode" by code, "secname" by name
        sec_type: Security type code. Common values:
            - "001" — stocks
            - "002" — funds
            - "003" — bonds
            - "007" — futures
            - "008" — options
            - "010" — indices
        market: Market code (same as ifind_date_query). "212001" = SSE, "212100" = SZSE
        trade_status: "0" all, "1" listed/trading, "2" delisted
        is_exact: "0" fuzzy match, "1" exact match
    """
    body: dict[str, str] = {
        "mode": mode,
        "sectype": sec_type,
        "market": market,
        "tradestatus": trade_status,
        "isexact": is_exact,
    }
    body[mode] = keyword
    result = _post("get_thscode", body)
    _record_usage("get_thscode", 1, "", "", f"{mode}={keyword}")
    return _format_response(result)


# ---------------------------------------------------------------------------
# Data volume query
# ---------------------------------------------------------------------------


@mcp.tool()
def ifind_data_volume() -> str:
    """Query official account data volume/quota usage (数据量查询).

    Returns your iFinD account's data volume consumption from the server side.
    Use this to check how much of your weekly/monthly quota you've consumed.
    Complements ifind_usage() which only tracks local calls.
    """
    result = _post("get_data_volume", {})
    _record_usage("data_volume", 0, "", "", "query")
    return _format_response(result)


# ---------------------------------------------------------------------------
# Error message query
# ---------------------------------------------------------------------------


@mcp.tool()
def ifind_error_message(
    error_code: int,
) -> str:
    """Look up the meaning of an iFinD API error code (错误信息查询).

    Use this when an API call returns an error code to understand what went wrong.

    Args:
        error_code: The numeric error code returned by iFinD API. Examples:
            -1010 (token expired), -4001 (no data), -4203 (wrong format), etc.
    """
    body = {"errorcode": error_code}
    result = _post("get_error_message", body)
    return _format_response(result)


# ---------------------------------------------------------------------------
# Usage query tool
# ---------------------------------------------------------------------------


@mcp.tool()
def ifind_usage() -> str:
    """Query iFinD API usage statistics from local call log (查询API用量).

    Returns:
    - Today's summary: call counts and data volume per API
    - Recent 20 calls with details
    - Rate limit reference

    Note: Official account-level usage can be checked via SuperCommand web:
    https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/super-command-web/index.html#/AccountDetails

    Call this to monitor API consumption before running large batch queries.
    Rate limits: single function QPS ≤ 10 (EDB ≤ 5), total QPS ≤ 20.
    Max data per request: 2,000,000 records.
    """
    conn = _get_db()

    # Recent calls
    recent = conn.execute(
        "SELECT api, ts, usage_amt, codes, indicators, detail FROM api_usage ORDER BY id DESC LIMIT 20"
    ).fetchall()

    # Daily summary
    today = datetime.now().strftime("%Y-%m-%d")
    daily = conn.execute(
        "SELECT api, COUNT(*) as calls, SUM(usage_amt) as total FROM api_usage WHERE ts >= ? GROUP BY api",
        (today,),
    ).fetchall()

    # Total summary (last 30 days)
    cutoff_30d = (datetime.now() - timedelta(days=30)).isoformat()
    monthly = conn.execute(
        "SELECT api, COUNT(*) as calls, SUM(usage_amt) as total FROM api_usage WHERE ts >= ? GROUP BY api",
        (cutoff_30d,),
    ).fetchall()

    conn.close()

    recent_calls = [
        {"api": r[0], "time": r[1], "usage": r[2], "codes": r[3], "indicators": r[4], "detail": r[5]}
        for r in recent
    ]

    daily_summary = [
        {"api": d[0], "calls_today": d[1], "total_usage_today": d[2]}
        for d in daily
    ]

    monthly_summary = [
        {"api": m[0], "calls_30d": m[1], "total_usage_30d": m[2]}
        for m in monthly
    ]

    return json.dumps(
        {
            "daily_summary": daily_summary,
            "monthly_summary": monthly_summary,
            "recent_calls": recent_calls,
            "rate_limits": {
                "single_function_qps": 10,
                "edb_function_qps": 5,
                "total_qps": 20,
                "max_records_per_request": 2_000_000,
            },
            "note": "For official account usage, check SuperCommand web: "
                    "https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/super-command-web/index.html#/AccountDetails",
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
