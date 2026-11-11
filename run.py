"""Launch the A-share adapter against a TradingAgents v0.7.1 checkout."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-dir', required=True, type=Path, help='Path to the TradingAgents checkout')
    parser.add_argument('--ticker', default='600519.SS')
    parser.add_argument('--date', default='2026-11-06')
    parser.add_argument('--check-data', action='store_true', help='Fetch and verify data only; no LLM calls')
    parser.add_argument('--run', action='store_true', help='Start the multi-agent LLM workflow after verifying data')
    args = parser.parse_args()
    project = args.project_dir.resolve()
    if not (project / 'tradingagents' / 'default_config.py').is_file():
        parser.error('TradingAgents checkout not found')
    if not args.check_data and not args.run:
        parser.error('Choose --check-data or --run')

    os.chdir(project)  # Let the existing package load the local .env credentials.
    sys.path.insert(0, str(project))
    import tradingagents
    if tradingagents.__version__ != '0.7.1':
        parser.error(f'This adapter targets TradingAgents 0.7.1; found {tradingagents.__version__}')

    import pandas as pd
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.dataflows.config import set_config
    import tencent_cn as vendor

    home = Path(__file__).resolve().parent
    # Isolate A-share results/memory from previous NVDA/Yahoo runs.
    DEFAULT_CONFIG.update({
        'data_cache_dir': str(home / 'cache'), 'results_dir': str(home / 'results'),
        'memory_log_path': str(home / 'results' / 'trading_memory.md'),
        'output_language': 'Chinese', 'checkpoint_enabled': False,
    })
    DEFAULT_CONFIG['data_vendors'].update({'core_stock_apis': 'tencent_cn', 'technical_indicators': 'tencent_cn'})
    DEFAULT_CONFIG.setdefault('tool_vendors', {}).update({'get_stock_data': 'tencent_cn', 'get_indicators': 'tencent_cn'})
    set_config(DEFAULT_CONFIG)
    try:
        vendor.tx_symbol(args.ticker)
        cutoff = vendor.cutoff_date(args.date)
    except ValueError as exc:
        parser.error(str(exc))
    args.ticker = args.ticker.upper()
    vendor.install_adapter(args.date)

    # Verify all three data paths before any LLM is constructed/called.
    frame = vendor.load_ohlcv(args.ticker, args.date)
    from tradingagents.agents import tools, context
    tool_common = {'symbol': args.ticker, 'trade_date': args.date}
    start = max(frame['Date'].min(), cutoff - pd.Timedelta(days=90))
    prices = tools.get_stock_data.func(**tool_common, start_date=f'{start:%Y-%m-%d}', end_date=args.date)
    indicators = tools.get_indicators.func(**tool_common, indicator='rsi,macd', curr_date=args.date, look_back_days=10)
    snapshot = tools.get_verified_market_snapshot.func(**tool_common, curr_date=args.date, look_back_days=5)
    for output in (prices, indicators, snapshot):
        if 'Source: Tencent Finance' not in output:
            raise RuntimeError(f'Data verification failed: {output[:200]}')
    summary = {
        'source': 'Tencent Finance', 'ticker': args.ticker, 'analysis_date': args.date,
        'identity': context.resolve_instrument_identity(args.ticker),
        'rows': len(frame), 'first_date': str(frame['Date'].iloc[0].date()),
        'last_date': str(frame['Date'].iloc[-1].date()),
        'last_close': float(frame['Close'].iloc[-1]), 'volume_unit': 'shares', 'adjustment': 'none',
        'verified_paths': ['get_stock_data', 'get_indicators (rsi,macd)', 'get_verified_market_snapshot'],
        'llm_called_by_data_check': False,
        'scope': 'market analyst plus downstream debate/trader/risk agents; A-share fundamentals/news not integrated',
    }
    report_dir = home / 'checks'
    report_dir.mkdir(parents=True, exist_ok=True)
    tag = f'{args.ticker}-{args.date}'
    (report_dir / f'{tag}.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (report_dir / f'{tag}-snapshot.md').write_text(snapshot, encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    if not args.run:
        return

    # Only the market analyst is enabled until historical A-share fundamentals
    # and news have an independently verified, point-in-time data adapter.
    from cli.main import app
    app(args=['--ticker', args.ticker, '--date', args.date, '--analysts', 'market', '--save', '--no-show'])


if __name__ == '__main__':
    main()
