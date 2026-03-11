# iFinD HTTP API MCP Server

A local MCP server that wraps Tonghuashun (同花顺) iFinD HTTP API, enabling Claude to query iFinD data directly without requiring a local iFinD terminal installation.

## Prerequisites

1. **iFinD account** with HTTP API access (apply at [quantapi.10jqka.com.cn](https://quantapi.10jqka.com.cn/))
2. **refresh_token** obtained from iFinD SuperCommand:
   - Web: https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/super-command-web/index.html#/AccountDetails
   - Or via SuperCommand desktop tool: Tools → refresh_token查询
3. **Python 3.8+**

## Setup

```bash
cd financial-analysis/mcp/ifind-mcp-server
pip install -r requirements.txt
```

Set your refresh_token as an environment variable:

```bash
export IFIND_REFRESH_TOKEN="your_refresh_token_here"
```

## Configuration

The server is configured in `financial-analysis/.mcp.json` as a stdio-based MCP server:

```json
{
  "ifind": {
    "type": "stdio",
    "command": "python",
    "args": ["financial-analysis/mcp/ifind-mcp-server/server.py"],
    "env": {
      "IFIND_REFRESH_TOKEN": ""
    }
  }
}
```

## Available Tools

### Core Data Tools

| Tool | iFinD API | HTTP Endpoint | Description |
|------|-----------|---------------|-------------|
| `ifind_basic_data` | `THS_BD` | `/basic_data_service` | Cross-sectional fundamental data (financials, valuation, company info) |
| `ifind_date_sequence` | `THS_DS` | `/date_sequence` | Historical date-sequence data (time-series fundamentals) |
| `ifind_history_quotes` | `THS_HQ` | `/cmd_history_quotation` | Historical market quotes (OHLCV, technicals) |
| `ifind_realtime_quotes` | `THS_RQ` | `/real_time_quotation` | Real-time quote snapshot |
| `ifind_high_frequency` | `THS_HF` | `/high_frequency` | Intraday minute-bar data |
| `ifind_snapshot` | `THS_SS` | `/snap_shot` | Intraday snapshot / order book |
| `ifind_data_pool` | `THS_DP` | `/data_pool` | Dataset queries (index constituents, blocks, reports) |
| `ifind_edb` | `THS_EDB` | `/edb_service` | Economic database (GDP, CPI, PMI, rates, FX) |

### Fund Valuation Tools

| Tool | iFinD API | HTTP Endpoint | Description |
|------|-----------|---------------|-------------|
| `ifind_fund_valuation` | `THS_realTimeValuation` | `/fund_valuation` | Real-time fund NAV estimation (minute-level) |
| `ifind_final_fund_valuation` | — | `/final_fund_valuation` | Daily fund valuation vs actual NAV comparison |

### Date Utility Tools

| Tool | iFinD API | HTTP Endpoint | Description |
|------|-----------|---------------|-------------|
| `ifind_date_query` | `THS_DateQuery` | `/get_trade_dates` | Query trading dates for any exchange |
| `ifind_date_offset` | `THS_DateOffset` | `/get_trade_dates` | Calculate date offset by N trading/calendar days |

### Screening & Lookup Tools

| Tool | iFinD API | HTTP Endpoint | Description |
|------|-----------|---------------|-------------|
| `ifind_smart_stock_picking` | `THS_WCQuery` | `/smart_stock_picking` | Wencai AI natural-language stock screener |
| `ifind_get_thscode` | `THS_toTHSCODE` | `/get_thscode` | Convert security code/name to iFinD thscode format |

### Account & Diagnostics Tools

| Tool | iFinD API | HTTP Endpoint | Description |
|------|-----------|---------------|-------------|
| `ifind_data_volume` | — | `/get_data_volume` | Official account data quota usage |
| `ifind_error_message` | — | `/get_error_message` | Look up iFinD error code meanings |
| `ifind_usage` | — | (local) | Local API call log and usage statistics |

## Security Codes Format

| Market | Format | Example |
|--------|--------|---------|
| A-shares (Shanghai) | XXXXXX.SH | 600519.SH |
| A-shares (Shenzhen) | XXXXXX.SZ | 000858.SZ |
| Hong Kong | XXXXX.HK | 00700.HK |
| US (NASDAQ) | XXXX.O | AAPL.O |
| US (NYSE) | XXXX.N | MSFT.N |
| Index | XXXXXX.SH/SZ | 000300.SH |
| Futures | XXXXXX.CFE/SHF/DCE/ZCE | IF2403.CFE |
| Bonds | XXXXXX.SH/SZ/IB | 019672.SH |

## Rate Limits

| Limit | Value |
|-------|-------|
| Single function QPS | ≤ 10 |
| EDB function QPS | ≤ 5 |
| Total account QPS | ≤ 20 |
| Max records per request | 2,000,000 |
| Max IPs per access_token | 20 |
| access_token validity | 7 days (auto-refreshed) |

## Authentication Flow

1. Set `IFIND_REFRESH_TOKEN` environment variable (long-lived, expires with account)
2. Server automatically obtains `access_token` via `/get_access_token` endpoint
3. `access_token` is cached for 7 days and auto-refreshed when expired

## Common Indicator Codes

### Stock Price & Volume
- `ths_open_price_stock`, `ths_close_price_stock`, `ths_high_price_stock`, `ths_low_price_stock`
- `ths_vol_stock`, `ths_amt_stock`, `ths_turnover_ratio_stock`

### Valuation
- `ths_pe_ttm_stock`, `ths_pb_mrq_stock`, `ths_ps_ttm_stock`, `ths_pcf_ocf_ttm_stock`
- `ths_market_value_stock`, `ths_float_market_value_stock`

### Financials
- `ths_or_ttm_stock` (revenue), `ths_np_atoopc_stock` (net profit)
- `ths_roe_ttm_stock`, `ths_eps_stock`, `ths_bps_stock`

### Growth
- `ths_or_yoy_stock` (revenue YoY), `ths_np_yoy_stock` (net profit YoY)

For a complete indicator catalog, use iFinD SuperCommand web:
https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/super-command-web/index.html
