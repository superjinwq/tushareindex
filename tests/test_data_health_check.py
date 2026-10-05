"""data_health_check 纯函数：期望报告期、交易日滞后、行数对比、导入期不碰根日志器。"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_health_check as dhc  # noqa: E402

D = dt.date


def test_expected_period_follows_disclosure_deadlines() -> None:
    assert dhc.expectedPeriod(D(2026, 10, 4)) == D(2026, 6, 30)
    assert dhc.expectedPeriod(D(2026, 11, 1)) == D(2026, 9, 30)
    assert dhc.expectedPeriod(D(2026, 8, 31)) == D(2026, 3, 31)
    assert dhc.expectedPeriod(D(2026, 9, 1)) == D(2026, 6, 30)
    assert dhc.expectedPeriod(D(2026, 5, 1)) == D(2026, 3, 31)
    assert dhc.expectedPeriod(D(2026, 2, 1)) == D(2025, 9, 30)


def test_lag_counts_open_days_only() -> None:
    days = [D(2026, 9, 28), D(2026, 9, 29), D(2026, 9, 30), D(2026, 10, 8)]
    assert dhc.lagTradeDays(D(2026, 9, 30), D(2026, 9, 30), days) == 0
    assert dhc.lagTradeDays(D(2026, 9, 29), D(2026, 9, 30), days) == 1
    assert dhc.lagTradeDays(D(2026, 9, 28), D(2026, 10, 8), days) == 3
    assert dhc.lagTradeDays(None, D(2026, 9, 30), days) is None


def test_lag_status_respects_allowance() -> None:
    assert dhc.lagStatus(0) == 'OK'
    assert dhc.lagStatus(1) == 'WARN'
    assert dhc.lagStatus(2) == 'FAIL'
    assert dhc.lagStatus(1, allowLag=1) == 'OK'
    assert dhc.lagStatus(None) == 'FAIL'


def test_compare_counts_flags_low_latest_per_group() -> None:
    counts = {
        D(2026, 9, 28): {'SH': 100, 'BJ': 50},
        D(2026, 9, 29): {'SH': 100, 'BJ': 50},
        D(2026, 9, 30): {'SH': 99, 'BJ': 30},
    }
    status, detail = dhc.compareCounts(counts, D(2026, 9, 30))
    assert detail['SH']['status'] == 'OK'
    assert detail['BJ']['status'] == 'FAIL'
    assert status == 'FAIL'


def test_worst_and_ratio_status() -> None:
    assert dhc.worst('OK', 'WARN', 'OK') == 'WARN'
    assert dhc.worst('INFO', 'ERROR') == 'ERROR'
    assert dhc.ratioStatus(None) == 'INFO'
    assert dhc.ratioStatus(0.95) == 'OK'
    assert dhc.ratioStatus(0.8) == 'WARN'
    assert dhc.ratioStatus(0.5) == 'FAIL'


def test_import_does_not_touch_root_logger() -> None:
    root = logging.getLogger()
    assert not any(getattr(h, 'baseFilename', '').endswith('data_health_check.log') for h in root.handlers)
    assert dhc.logger.propagate is True or dhc.logger.handlers == []


def test_build_context_holiday_and_intraday() -> None:
    days = [D(2026, 9, 28), D(2026, 9, 29), D(2026, 9, 30)]

    class FakeDb:
        def rows(self, sql, args=None):
            return [(d,) for d in reversed([x for x in days if x <= args[0]])]

    ctx = dhc.buildContext(FakeDb(), dt.datetime(2026, 10, 4, 21, 0))
    assert ctx['expectedDaily'] == D(2026, 9, 30) and ctx['expectedIntraday'] == D(2026, 9, 30)
    ctx = dhc.buildContext(FakeDb(), dt.datetime(2026, 9, 30, 10, 0))
    assert ctx['expectedDaily'] == D(2026, 9, 29) and ctx['expectedIntraday'] == D(2026, 9, 30)
    ctx = dhc.buildContext(FakeDb(), dt.datetime(2026, 9, 30, 19, 0))
    assert ctx['expectedDaily'] == D(2026, 9, 30)


def test_plain_reason_translates_custom_and_coverage() -> None:
    sw = dict(table='index_member_all_sw', status='WARN', latestInDateAmbiguousL3=['000406.SZ'],
              listedWithoutCurrentSw={'BJ': 6})
    assert '000406.SZ' in dhc.plainReason(sw) and '北交所6 只股票没有申万行业' in dhc.plainReason(sw)
    rt = dict(table='rt_k', status='WARN', coverage={'BJ': {'ratio': 0.9775, 'status': 'WARN'},
                                                     'SH': {'ratio': 1.0, 'status': 'OK'}})
    assert dhc.plainReason(rt) == '北交所覆盖率只有 97.8%'
    assert dhc.plainReason(dict(table='x', status='ERROR', error='Boom')).endswith('Boom')


def test_exit_code_only_nonzero_on_fail(monkeypatch) -> None:
    def fakeRun(overall):
        return lambda only, n: dict(overall=overall, counts={'OK': 1, 'WARN': 0, 'FAIL': 0, 'ERROR': 0},
                                    tables=1, queries=0, elapsedSec=0.1, generatedAt='2026-10-04 22:00:00',
                                    results=[])
    monkeypatch.setattr(dhc, 'setupLogger', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['data_health_check.py', '--no-write'])
    for overall, code in (('OK', 0), ('WARN', 0), ('FAIL', 1), ('ERROR', 1)):
        monkeypatch.setattr(dhc, 'runAll', fakeRun(overall))
        assert dhc.main() == code

# 代码更新时间：2026-10-04 21:22 新建：data_health_check 纯函数与导入期日志回归
# 代码更新时间：2026-10-04 22:37 摘要翻译与退出码回归
