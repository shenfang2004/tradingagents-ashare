"""Offline behavioral checks; generated fixture bars are never experiment data."""
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

if project_dir := os.environ.get('TRADINGAGENTS_PROJECT_DIR'):
    sys.path.insert(0, str(Path(project_dir).resolve()))
import pandas as pd
import tencent_cn as vendor
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorUnavailableError


class AdapterChecks(unittest.TestCase):
    def setUp(self):
        self.work_root = Path(__file__).resolve().parent / 'test-work'
        self.directory = self.work_root / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        set_config({'data_cache_dir': str(self.directory),
                    'data_vendors': {'core_stock_apis': 'tencent_cn', 'technical_indicators': 'tencent_cn'},
                    'tool_vendors': {'get_stock_data': 'tencent_cn', 'get_indicators': 'tencent_cn'}})
        dates = pd.bdate_range(end='2026-11-06', periods=260)
        self.rows = [[str(day.date()), 100 + i, 101 + i, 102 + i, 99 + i, 10 + i]
                     for i, day in enumerate(dates)]
        self.payload = {'schema_version': 1, 'source': 'Tencent Finance',
                        'symbol': '600519.SS', 'analysis_date': '2026-11-06',
                        'adjustment': 'none', 'rows': self.rows,
                        'company_name_current': 'Fixture company'}

    def tearDown(self):
        if not self.directory.resolve().is_relative_to(self.work_root.resolve()):
            raise AssertionError('Unexpected test directory')
        shutil.rmtree(self.directory)

    def test_all_public_tools_reuse_cache_and_never_touch_yahoo(self):
        vendor.install_adapter('2026-11-06')
        from tradingagents.agents import tools, context
        with (patch.object(vendor, '_download', return_value=self.payload) as fetch,
              patch('yfinance.Ticker', side_effect=AssertionError('Yahoo must not be called'))):
            common = {'symbol': '600519.SS', 'trade_date': '2026-11-06'}
            text = tools.get_stock_data.func(**common, start_date='2026-10-01', end_date='2026-12-31')
            indicators = tools.get_indicators.func(**common, indicator='rsi,macd', curr_date='2026-12-31')
            snapshot = tools.get_verified_market_snapshot.func(**common, curr_date='2026-12-31')
            identity = context.resolve_instrument_identity('600519.SS')
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(identity['company_name'], 'Fixture company')
            for result in (text, indicators, snapshot):
                self.assertIn('Source: Tencent Finance', result)
                self.assertNotIn('2026-12-31', result)
            self.assertIn('| Close | 360.000000 |', snapshot)
            self.assertIn('| Volume | 26900.000000 |', snapshot)
            self.assertIn('| rsi | 100.000000 |', snapshot)
            self.assertIn('| close_200_sma | 260.500000 |', snapshot)

    def test_later_bars_cannot_change_historical_prices_or_indicators(self):
        future = ['2026-11-09', 99999, 99999, 99999, 99999, 1]
        before = vendor.parse_rows(self.rows, '600519.SS', pd.Timestamp('2026-11-06'))
        after = vendor.parse_rows(self.rows + [future], '600519.SS', pd.Timestamp('2026-11-06'))
        pd.testing.assert_frame_equal(before, after)

    def test_sma_requires_the_full_number_of_trading_bars(self):
        self.payload['rows'] = self.rows[-49:]
        with patch.object(vendor, '_download', return_value=self.payload):
            report = vendor.get_indicators('600519.SS', 'close_50_sma', '2026-11-06')
            snapshot = vendor.build_verified_market_snapshot('600519.SS', '2026-11-06')
            self.assertIn('2026-11-06,N/A', report)
            self.assertIn('| close_50_sma | N/A |', snapshot)

    def test_network_failure_triggers_cooldown_instead_of_repeated_tool_requests(self):
        with patch.object(vendor, '_download', side_effect=VendorUnavailableError('429')) as fetch:
            for _ in range(3):
                with self.assertRaises(VendorUnavailableError):
                    vendor.load_ohlcv('600519.SS', '2026-11-06')
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(list(self.directory.rglob('*.json')), [])

    def test_corrupt_cache_is_not_served(self):
        cache = self.directory / 'tencent_cn'
        cache.mkdir()
        (cache / 'sh600519-2026-11-06-raw-v1.json').write_text('{broken', encoding='utf-8')
        with patch.object(vendor, '_download', return_value=self.payload) as fetch:
            frame = vendor.load_ohlcv('600519.SS', '2026-11-06')
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(frame['Close'].iloc[-1], 360)

    def test_schema_and_nonfinite_prices_are_rejected(self):
        for rows in ([['2026-11-06', 1]], [['2026-11-06', 10, 'NaN', 11, 9, 1]],
                     [['2026-11-06', 10, 12, 11, 9, 1]]):
            with self.subTest(rows=rows), self.assertRaises(VendorUnavailableError):
                vendor.parse_rows(rows, '600519.SS', pd.Timestamp('2026-11-06'))

    def test_only_explicit_exchange_symbols_and_settled_historical_dates(self):
        self.assertEqual(vendor.tx_symbol('600519.SS'), 'sh600519')
        self.assertEqual(vendor.tx_symbol('000001.SZ'), 'sz000001')
        for ticker in ('NVDA', '600519', '../600519.SS'):
            with self.assertRaises(ValueError):
                vendor.tx_symbol(ticker)
        with self.assertRaises(ValueError):
            vendor.cutoff_date(str(pd.Timestamp.now(tz='Asia/Shanghai').date()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
