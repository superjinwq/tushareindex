#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per C 锚点：页面 JSON 覆盖、空值清锚点、HTTP 契约。"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rt_monitor.blueprints import opt_t_board_bp as boardBp  # noqa: E402
from rt_monitor.core import config as cfg  # noqa: E402
from rt_monitor.services import opt_iv_windows as ivWindows  # noqa: E402


@pytest.fixture
def percTmp(tmp_path, monkeypatch):
    backup = dict(cfg.OPT_IV_PERC_ANCHORS)
    monkeypatch.setattr(cfg, 'OPT_IV_PERC_ANCHORS_PATH', str(tmp_path / 'opt_iv_perc_anchors.json'))
    monkeypatch.delenv('OPT_IV_PERC_ANCHORS', raising=False)
    ivWindows.applyPercAnchorLayers()
    yield tmp_path
    cfg.OPT_IV_PERC_ANCHORS.clear()
    cfg.OPT_IV_PERC_ANCHORS.update(backup)


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(boardBp.optTBoardBp)
    return app.test_client()


def test_save_overrides_default_and_empty_clears(percTmp):
    items, err = ivWindows.savePercAnchors({'510050.SH': '2024-10-01'})
    assert err is None
    assert ivWindows.anchorFor('510050.SH') == date(2024, 10, 1)
    byCode = {x['ts_code']: x['date'] for x in items}
    assert byCode['510050.SH'] == '2024-10-01'
    # 未改动的标的仍走默认
    assert ivWindows.anchorFor('588000.SH') == date(2024, 9, 24)

    items, err = ivWindows.savePercAnchors({'510050.SH': ''})
    assert err is None
    assert ivWindows.anchorFor('510050.SH') is None
    byCode = {x['ts_code']: x['date'] for x in items}
    assert byCode['510050.SH'] == ''


def test_file_wins_over_env(percTmp, monkeypatch):
    monkeypatch.setenv('OPT_IV_PERC_ANCHORS', '588000.SH=2020-01-01')
    ivWindows.applyPercAnchorLayers()
    assert ivWindows.anchorFor('588000.SH') == date(2020, 1, 1)
    _, err = ivWindows.savePercAnchors({'588000.SH': '2024-09-24'})
    assert err is None
    assert ivWindows.anchorFor('588000.SH') == date(2024, 9, 24)
    raw = json.loads((percTmp / 'opt_iv_perc_anchors.json').read_text(encoding='utf-8'))
    assert raw['anchors']['588000.SH'] == '2024-09-24'


def test_six_digit_code_maps_and_invalid_rejected(percTmp):
    _, err = ivWindows.savePercAnchors({'510500': '2024-10-01'})
    assert err is None
    assert ivWindows.anchorFor('510500.SH') == date(2024, 10, 1)

    items, err = ivWindows.savePercAnchors({'588000.SH': '2024/09/24'})
    assert items is None and err
    assert 'YYYY-MM-DD' in err

    items, err = ivWindows.savePercAnchors({'999999.SH': '2024-09-24'})
    assert items is None and '未知标的' in err


def test_save_clears_window_cache(percTmp, monkeypatch):
    flags: list[str] = []
    ivWindows.cachedPerWindows(
        ivWindows.cacheKey('588000.SH', 'current_month', 'C', date(2026, 9, 21)),
        ivWindows.emptyWindows,
    )
    assert ivWindows.cacheSize() > 0

    def fakeT() -> None:
        flags.append('t')

    def fakeIv() -> None:
        flags.append('iv')

    monkeypatch.setattr(
        'rt_monitor.services.opt_t_board.clearOptTBoardCache', fakeT
    )
    monkeypatch.setattr(
        'rt_monitor.services.opt_iv_board.clearOptIvBoardCache', fakeIv
    )
    _, err = ivWindows.savePercAnchors({'588000.SH': '2024-09-24'})
    assert err is None
    assert ivWindows.cacheSize() == 0
    assert flags == ['t', 'iv']


def test_get_catalog_route(percTmp, client):
    resp = client.get('/api/opt-iv-perc-anchors')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    codes = [x['ts_code'] for x in body['items']]
    assert '588000.SH' in codes
    assert '510050.SH' in codes
    assert len(body['items']) >= 8
    assert 'no-store' in resp.headers.get('Cache-Control', '')


def test_post_route_writes_and_rejects_bad_payload(percTmp, client):
    bad = client.post('/api/opt-iv-perc-anchors', json={'nope': 1})
    assert bad.status_code == 400
    assert bad.get_json()['ok'] is False

    ok = client.post(
        '/api/opt-iv-perc-anchors',
        json={'anchors': {'510050.SH': '2024-10-01'}},
    )
    assert ok.status_code == 200
    body = ok.get_json()
    assert body['ok'] is True
    byCode = {x['ts_code']: x['date'] for x in body['items']}
    assert byCode['510050.SH'] == '2024-10-01'
    assert ivWindows.anchorFor('510050.SH') == date(2024, 10, 1)


# 代码更新时间：2026-09-21 06:31
