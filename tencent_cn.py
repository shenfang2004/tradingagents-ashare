"""Read-only Tencent daily bars for the TradingAgents 0.7.1 A-share launcher.

No Yahoo calls, LLM calls, API credentials, or order placement. Historical
unadjusted bars only. The API volume is in lots; the normalized volume is shares.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from stockstats import wrap

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

ENDPOINT = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
INDICATORS = ('close_10_ema', 'close_50_sma', 'close_200_sma', 'rsi',
              'boll', 'boll_ub', 'boll_lb', 'macd', 'macds', 'macdh', 'atr', 'vwma')
SMA_WINDOWS = {'close_50_sma': 50, 'close_200_sma': 200}
_LOCK = threading.RLock()
_FAILURES: dict[tuple[str, str], float] = {}


def tx_symbol(symbol: str) -> str:
    match = re.fullmatch(r'(\d{6})\.(SS|SZ)', symbol.upper())
    if not match:
        raise ValueError('Use an exchange-qualified A-share symbol, e.g. 600519.SS or 000001.SZ')
    return ('sh' if match[2] == 'SS' else 'sz') + match[1]


def cutoff_date(value: str) -> pd.Timestamp:
    dt = pd.Timestamp(datetime.strptime(value, '%Y-%m-%d').date())
    if dt.strftime('%Y-%m-%d') != value:
        raise ValueError('The analysis date must use YYYY-MM-DD')
    # Never label an intraday quote as the final daily closing price.
    if dt.date() >= pd.Timestamp.now(tz='Asia/Shanghai').date():
        raise ValueError('This historical daily-bar adapter requires a date before today in Asia/Shanghai')
    return dt


def parse_rows(rows: list, symbol: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    if not rows:
        raise NoMarketDataError(symbol, detail='Tencent returned no unadjusted daily bars')
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) < 6 for row in rows):
        raise VendorUnavailableError('Tencent daily-bar response has an unexpected schema')
    # Tencent order is date, OPEN, CLOSE, HIGH, LOW, volume in lots.
    frame = pd.DataFrame([r[:6] for r in rows], columns=['Date', 'Open', 'Close', 'High', 'Low', 'Volume'])
    frame['Date'] = pd.to_datetime(frame['Date'], format='%Y-%m-%d', errors='coerce')
    if frame['Date'].isna().any():
        raise VendorUnavailableError('Tencent returned an invalid trading date')
    frame = frame[frame['Date'] <= cutoff].copy()
    for col in ('Open', 'Close', 'High', 'Low', 'Volume'):
        frame[col] = pd.to_numeric(frame[col], errors='coerce')
    if frame.empty:
        raise NoMarketDataError(symbol, detail='No bars on or before the analysis date')
    numeric = frame[['Open', 'Close', 'High', 'Low', 'Volume']]
    if not np.isfinite(numeric.to_numpy()).all():
        raise VendorUnavailableError('Tencent returned missing or non-finite OHLCV values')
    invalid = ((frame[['Open', 'Close', 'High', 'Low']] <= 0).any(axis=1)
               | (frame['Volume'] < 0)
               | (frame['High'] < frame[['Open', 'Close', 'Low']].max(axis=1))
               | (frame['Low'] > frame[['Open', 'Close', 'High']].min(axis=1)))
    if invalid.any():
        raise VendorUnavailableError('Tencent returned inconsistent OHLCV values')
    frame['Volume'] *= 100.0
    frame = frame.drop_duplicates('Date', keep='last').sort_values('Date').reset_index(drop=True)
    if (cutoff - frame['Date'].iloc[-1]).days > 10:
        raise NoMarketDataError(symbol, detail='Latest Tencent bar is more than 10 calendar days stale')
    return frame[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]


def _download(symbol: str, as_of: str) -> dict:
    cutoff = cutoff_date(as_of)
    start = (cutoff - pd.DateOffset(years=2)).strftime('%Y-%m-%d')
    name = tx_symbol(symbol)
    try:
        response = requests.get(ENDPOINT, params={'param': f'{name},day,{start},{as_of},640,'}, timeout=(5, 15))
        response.raise_for_status()
        response.encoding = 'utf-8'
        payload = response.json()
        if payload.get('code') != 0:
            raise ValueError('provider returned a nonzero status')
        block = payload['data'][name]
        rows = block.get('day', [])
        parse_rows(rows, symbol, cutoff)  # Validate before ever persisting a file.
        quote = block.get('qt', {}).get(name, [])
        return {
            'schema_version': 1, 'source': 'Tencent Finance', 'endpoint': ENDPOINT,
            'symbol': symbol.upper(), 'analysis_date': as_of, 'requested_start': start,
            'retrieved_at_utc': datetime.now(timezone.utc).isoformat(),
            'adjustment': 'none', 'raw_volume_unit': 'lots_of_100_shares',
            # Only the name is read from qt; no live financial/price fields leak.
            'company_name_current': quote[1] if len(quote) > 1 else None,
            'rows': [r for r in rows if str(r[0]) <= as_of],
        }
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise VendorUnavailableError(f'Tencent daily bars unavailable ({type(exc).__name__}); no Yahoo fallback') from exc


def dataset(symbol: str, as_of: str) -> dict:
    cutoff = cutoff_date(as_of)
    name = tx_symbol(symbol)
    directory = Path(get_config()['data_cache_dir']) / 'tencent_cn'
    path = directory / f'{name}-{as_of}-raw-v1.json'
    key = (str(path.resolve()), as_of)
    with _LOCK:
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if (saved['schema_version'] == 1 and saved['symbol'] == symbol.upper()
                        and saved['analysis_date'] == as_of and saved['adjustment'] == 'none'):
                    parse_rows(saved['rows'], symbol, cutoff)
                    return saved
            except (ValueError, KeyError, TypeError, NoMarketDataError, VendorUnavailableError):
                pass  # A corrupt cache is never served as market data.
        if time.monotonic() - _FAILURES.get(key, float('-inf')) < 60:
            raise VendorUnavailableError('Tencent request failed recently; 60-second cooldown, no repeated tool retries')
        try:
            saved = _download(symbol, as_of)
        except (VendorUnavailableError, NoMarketDataError):
            _FAILURES[key] = time.monotonic()
            raise
        directory.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        return saved


def load_ohlcv(symbol: str, as_of: str) -> pd.DataFrame:
    saved = dataset(symbol, as_of)
    return parse_rows(saved['rows'], symbol, cutoff_date(as_of))


def header(symbol: str, as_of: str) -> str:
    return (f'Source: Tencent Finance; symbol: {symbol}; analysis cutoff: {as_of}.\n'
            'Prices: CNY, UNADJUSTED; volume: shares. No rows after the cutoff.\n'
            'Corporate actions can cause price discontinuities; this is not a total-return backtest.\n')


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    start = pd.Timestamp(datetime.strptime(start_date, '%Y-%m-%d').date())
    cutoff = cutoff_date(end_date)
    if start > cutoff:
        raise ValueError('start_date must be on or before end_date')
    if start < cutoff - pd.DateOffset(years=2):
        raise ValueError('This adapter supports a two-year history per analysis cutoff')
    frame = load_ohlcv(symbol, end_date)
    frame = frame[frame['Date'] >= start]
    if frame.empty:
        raise NoMarketDataError(symbol, detail='No bars within the requested date range')
    return header(symbol, end_date) + frame.to_csv(index=False, date_format='%Y-%m-%d')


def indicator_series(stock: pd.DataFrame, indicator: str) -> pd.Series:
    window = SMA_WINDOWS.get(indicator)
    if window is not None:
        return stock['close'].rolling(window=window, min_periods=window).mean()
    return stock[indicator]


def get_indicators(symbol: str, indicator: str, as_of_date: str, look_back_days: int = 30) -> str:
    if indicator not in INDICATORS:
        raise ValueError(f'Unsupported indicator: {indicator}. Choose from {INDICATORS}')
    raw = load_ohlcv(symbol, as_of_date)
    values = wrap(raw.copy())
    start = cutoff_date(as_of_date) - pd.Timedelta(days=max(1, min(int(look_back_days), 365)))
    table = pd.DataFrame({'Date': raw['Date'], indicator: indicator_series(values, indicator)})
    table = table[table['Date'] >= start]
    return header(symbol, as_of_date) + table.to_csv(index=False, date_format='%Y-%m-%d', na_rep='N/A')


def build_verified_market_snapshot(symbol: str, as_of_date: str, look_back_days: int = 30) -> str:
    frame = load_ohlcv(symbol, as_of_date)
    values = wrap(frame.copy())
    last = frame.iloc[-1]
    lines = [f'## Verified market snapshot for {symbol}', header(symbol, as_of_date),
             f'Latest trading date: {last["Date"]:%Y-%m-%d}',
             '| Field | Value |', '|---|---:|']
    lines += [f'| {key} | {last[key]:.6f} |' for key in ('Open', 'High', 'Low', 'Close', 'Volume')]
    lines += ['', '| Indicator | Value |', '|---|---:|']
    for indicator in INDICATORS:
        try:
            number = indicator_series(values, indicator).iloc[-1]
            text = f'{number:.6f}' if pd.notna(number) else 'N/A'
        except (KeyError, ValueError, IndexError):
            text = 'N/A'
        lines.append(f'| {indicator} | {text} |')
    lines += ['', 'Recent closes:', frame[['Date', 'Close']].tail(max(1, min(int(look_back_days), 30))).to_csv(index=False, date_format='%Y-%m-%d'),
              'Use these computed values as the source of truth. Do not invent missing prices or claim validated trading performance.']
    return '\n'.join(lines)


def install_adapter(as_of: str) -> None:
    """Process-local registry/compatibility hooks; no upstream files are edited.

    v0.7.1 routes price/indicators via a registry, but binds the snapshot and
    identity directly to Yahoo. Override those two bindings for this launcher.
    Settlement also binds Yahoo directly; use the same Tencent price loader.
    """
    from tradingagents.dataflows import router
    from tradingagents.agents import context, tools
    from tradingagents.memory import settlement

    router.VENDOR_METHODS['get_stock_data']['tencent_cn'] = get_stock_data
    router.VENDOR_METHODS['get_indicators']['tencent_cn'] = get_indicators
    tools.build_verified_market_snapshot = build_verified_market_snapshot

    def identity(ticker: str) -> dict:
        saved = dataset(ticker, as_of)
        return {'company_name': saved.get('company_name_current') or ticker,
                'exchange': 'Shanghai' if ticker.upper().endswith('.SS') else 'Shenzhen',
                'quote_type': 'EQUITY'}

    context._identity = identity

    def closes(ticker: str, start_date: str, end_date: str) -> pd.Series:
        last_settled = pd.Timestamp.now(tz='Asia/Shanghai').date() - pd.Timedelta(days=1)
        settled_end = min(end_date, last_settled.isoformat())
        if settled_end < start_date:
            return pd.Series(dtype='float64', index=pd.DatetimeIndex([]))
        frame = load_ohlcv(ticker, settled_end)
        return frame.set_index('Date')['Close'].loc[start_date:settled_end]

    settlement.get_closes = closes
