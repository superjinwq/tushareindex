"""期限结构闸门与各到期 1σ 带。"""
from __future__ import annotations

import math
from pathlib import Path

from flask import Flask

from rt_monitor.services.opt_t_board import (
    blendExpectedMove,
    buildSectionExpectedMove,
    buildTermStructureGate,
    markExpectedMoveRows,
    quoteMidPx,
    rememberExpectedMoveLeg,
)


def _axis(midIv: float, ivHv: float, *, muted: bool = False) -> dict:
    return {'mid_iv': midIv, 'iv_hv': ivHv, 'muted': muted}


def test_term_gate_contango_lists_only_positive_premium() -> None:
    gate = buildTermStructureGate({
        'near': _axis(0.3543, 4.9),
        'next': _axis(0.3812, -6.5),
        'quarter': _axis(0.4000, -15.1),
        'next_quarter': _axis(0.3970, -12.1),
    })
    assert gate is not None
    assert gate['state'] == '正挂'
    assert gate['cls'] == 'opt-t-term--contango'
    assert gate['slope_pp'] == (0.3543 - 0.3812) * 100.0
    assert gate['rich'] == ['当月']
    assert '溢仍为正：当月' in gate['text']
    assert '下月 38.1' in gate['text']


def test_term_gate_backwardation_and_flat_band() -> None:
    back = buildTermStructureGate({
        'near': _axis(0.20, 1.2),
        'next': _axis(0.16, 0.4),
    })
    assert back is not None
    assert back['state'] == '倒挂'
    assert back['rich'] == ['当月', '下月']
    flat = buildTermStructureGate({
        'near': _axis(0.150, 0.2),
        'next': _axis(0.152, -0.1),
    })
    assert flat is not None
    assert flat['state'] == '平坦'
    assert abs(flat['slope_pp'] + 0.2) < 1e-9


def test_term_gate_skips_muted_front_month_and_missing_leg() -> None:
    gate = buildTermStructureGate({
        'near': _axis(0.30, 5.0, muted=True),
        'next': _axis(0.20, 1.0),
    })
    assert gate is not None
    assert gate['rich'] == ['下月']
    assert '当月 HV 已淡化' in gate['title']
    assert buildTermStructureGate({'near': _axis(0.20, 1.0)}) is None


def _fullAxis(
    midIv: float,
    ivHv: float,
    hvPer: float,
    call1y: float,
    put1y: float,
) -> dict:
    return {
        'mid_iv': midIv,
        'iv_hv': ivHv,
        'hv_per': hvPer,
        'per_call': (call1y, None, None),
        'per_put': (put1y, None, None),
        'muted': False,
    }


def test_term_reads_split_quote_level_from_premium() -> None:
    """588000 这种：下月 IV 更高，但只有当月溢为正，且当月分位不高。"""
    gate = buildTermStructureGate({
        'near': _fullAxis(0.3527, 4.9, 72.9, 42.3, 44.2),
        'next': _fullAxis(0.3807, -6.5, 90.4, 49.4, 53.0),
        'quarter': _fullAxis(0.4002, -15.1, 91.8, 59.7, 60.9),
        'next_quarter': _fullAxis(0.3972, -12.1, 90.8, 57.3, 65.7),
    }, '588000')
    assert gate is not None
    tags = [row['tag'] for row in gate['reads']]
    assert tags == ['溢价不强', '在修', '在修', '在修']
    assert gate['reads'][0]['action'] == '只宜轻'
    assert '只宜轻' in gate['reads'][0]['text']
    assert gate['reads'][1]['action'] == '不卖不买'
    assert '1y 43' in gate['reads'][0]['text']
    assert 'HV 73' in gate['reads'][0]['text']
    assert '中度正挂' in gate['text']
    assert '贵的是报价' in gate['curve']
    assert '下月不因 IV 更高去卖' in gate['curve']
    assert '正挂' not in gate['curve']


def test_term_reads_cover_rich_rent_and_light_book() -> None:
    rich = buildTermStructureGate({
        'near': _fullAxis(0.20, 4.0, 20.0, 80.0, 82.0),
        'next': _fullAxis(0.16, 1.2, 18.0, 22.0, 24.0),
    })
    assert rich is not None
    assert [row['tag'] for row in rich['reads']] == ['真贵', '低位收租']
    assert [row['action'] for row in rich['reads']] == ['义务仓可卖', '可收租']
    assert '卖当月要降档' in rich['curve']
    assert '陡倒挂' in rich['text']
    light = buildTermStructureGate({
        'near': _fullAxis(0.20, 2.0, 20.0, 22.0, 24.0),
        'next': _fullAxis(0.22, 1.5, 25.0, 28.0, 30.0),
    }, '588000.SH')
    assert light is not None
    assert light['reads'][0]['tag'] == '低位轻仓'
    assert light['reads'][0]['action'] == '轻仓'
    bothPos = buildTermStructureGate({
        'near': _fullAxis(0.14, 3.3, 10.0, 22.0, 24.0),
        'next': _fullAxis(0.16, 3.2, 12.0, 28.0, 30.0),
    })
    assert bothPos is not None
    assert bothPos['state'] == '正挂'
    assert '选月看溢，远月没有事件肥' in bothPos['curve']
    assert '不按 50' in light['reads'][0]['title']
    flatPrem = buildTermStructureGate({
        'near': _fullAxis(0.37, 6.0, 75.0, 49.0, 49.0),
        'next': _fullAxis(0.39, -1.0, 88.0, 58.0, 58.0),
    })
    assert flatPrem is not None
    assert flatPrem['reads'][1]['tag'] == '中性'
    assert '溢为负' not in flatPrem['curve']
    assert '下月不因 IV 更高去卖' not in flatPrem['curve']
    split = buildTermStructureGate({
        'near': _fullAxis(0.17, 4.8, 19.0, 59.0, 48.0),
        'next': _fullAxis(0.18, 3.3, 33.0, 49.0, 49.0),
    })
    assert split is not None
    nearSides = split['reads'][0]['sides']
    assert [side['name'] for side in nearSides] == ['沽', '购']
    assert nearSides[0]['tag'] == '低位收租'
    assert nearSides[1]['tag'] == '偏贵'
    weakLong = buildTermStructureGate({
        'near': _fullAxis(0.18, 4.0, 20.0, 40.0, 40.0),
        'next': _fullAxis(0.17, -1.4, 56.0, 29.0, 29.0),
    })
    assert weakLong is not None
    assert weakLong['state'] == '倒挂'
    assert weakLong['reads'][1]['tag'] == '溢为负'
    assert weakLong['reads'][1]['action'] == '不卖不买'
    realCheap = buildTermStructureGate({
        'near': _fullAxis(0.18, 4.0, 20.0, 40.0, 40.0),
        'next': _fullAxis(0.16, -4.0, 40.0, 20.0, 22.0),
    })
    assert realCheap is not None
    assert realCheap['reads'][1]['tag'] == '分位偏低'
    assert realCheap['reads'][1]['action'] == '权利仓可看'


def test_blend_expected_move_matches_tastytrade_weights() -> None:
    move = blendExpectedMove(4.40, 3.46, 2.66)
    assert move is not None
    assert abs(move - (4.40 * 0.6 + 3.46 * 0.3 + 2.66 * 0.1)) < 1e-9
    partial = blendExpectedMove(4.40, 3.46, None)
    assert partial is not None
    assert abs(partial - (4.40 * 0.6 + 3.46 * 0.3) / 0.9) < 1e-9
    assert blendExpectedMove(None, 3.46, 2.66) is None


def test_section_expected_move_uses_strangle_then_iv_fallback() -> None:
    book = {
        2.80: {'call': 0.20, 'put': 0.05, 'call_iv': 0.16, 'put_iv': 0.17},
        2.85: {'call': 0.16, 'put': 0.07},
        2.90: {'call': 0.12, 'put': 0.10, 'call_iv': 0.15, 'put_iv': 0.15},
        2.95: {'call': 0.09, 'put': 0.14},
        3.00: {'call': 0.06, 'put': 0.18},
    }
    priced = buildSectionExpectedMove(spot=2.90, days=62, book=book)
    assert priced is not None
    assert '0.6' in priced['source']
    straddle = 0.12 + 0.10
    wing1 = 0.09 + 0.07
    wing2 = 0.06 + 0.05
    assert abs(priced['move'] - (straddle * 0.6 + wing1 * 0.3 + wing2 * 0.1)) < 1e-9
    assert abs((priced['hi'] - priced['lo']) / 2.0 - priced['move']) < 1e-9

    ivOnly = buildSectionExpectedMove(
        spot=1.713,
        days=62,
        book={1.70: {'call_iv': 0.3812, 'put_iv': 0.3804}},
    )
    assert ivOnly is not None
    want = 1.713 * ((0.3812 + 0.3804) / 2.0) * math.sqrt(62 / 365.0)
    assert abs(ivOnly['move'] - want) < 1e-9
    assert '平值中轴IV' in ivOnly['source']


def test_quote_mid_and_strike_band_edges() -> None:
    assert quoteMidPx(0.10, 0.12, 0.20) == 0.11
    assert quoteMidPx(0.12, 0.10, 0.20) == 0.20
    book: dict[float, dict[str, float]] = {}
    rememberExpectedMoveLeg(
        book,
        strike=2.9,
        isAdj=True,
        bidCall=0.1,
        askCall=0.2,
        lastCall=0.15,
        bidPut=0.1,
        askPut=0.2,
        lastPut=0.15,
        callIv=0.2,
        putIv=0.2,
    )
    assert book == {}
    rememberExpectedMoveLeg(
        book,
        strike=2.9,
        isAdj=False,
        bidCall=0.10,
        askCall=0.12,
        lastCall=0.99,
        bidPut=0.08,
        askPut=0.10,
        lastPut=0.99,
        callIv=0.15,
        putIv=0.16,
    )
    assert book[2.9]['call'] == 0.11
    assert book[2.9]['put'] == 0.09

    rows = [
        {'strike': k, 'strike_title': '标准合约'}
        for k in (2.75, 2.80, 2.85, 2.90, 2.95, 3.00, 3.10, 3.15)
    ]
    move = {'lo': 2.959 - 0.183, 'hi': 2.959 + 0.183, 'text': '1σ'}
    markExpectedMoveRows(rows, 2.959, move)
    byStrike = {row['strike']: row['em_cls'] for row in rows}
    assert byStrike[2.75] == ''
    assert byStrike[3.15] == ''
    assert 'opt-t-em-in' in byStrike[2.85]
    assert 'opt-t-em-edge' in byStrike[2.80]
    assert 'opt-t-em-edge' in byStrike[3.10]
    assert '1σ带内' in rows[1]['strike_title']
    assert '1σ带外' in rows[0]['strike_title']


def test_body_fragment_shows_term_gate_and_expected_move() -> None:
    root = Path(__file__).resolve().parents[1]
    app = Flask(__name__, template_folder=str(root / 'templates'))
    payloadSections = [{
        'spread_alt': False,
        'quote_asof': '无实时 · 2026-09-24',
        'quote_asof_kind': 'daily',
        'expiry_meta': '2026-11-25 · 剩62天 · 下月',
        'expected_move': {
            'text': '1σ ±0.269 · 1.444–1.982',
            'title': '约 68% 价格带',
        },
        'rows': [{
            'row_cls': '',
            'em_cls': 'opt-t-em-in opt-t-em-edge',
            'strike_title': '标准合约 · 1σ带内',
            'strike_display': '1.700',
            'moneyness_pct': '-0.76',
            'moneyness_cls': '',
            'call_oi': '—', 'call_oi_cls': '', 'call_oi_age_cls': '',
            'call_vol': '—', 'call_vol_cls': '', 'call_vol_age_cls': '',
            'call_close': '—', 'call_live': False, 'call_last_age_cls': '',
            'call_px_title': '', 'call_ts_code': '', 'call_prem_pct': '—',
            'call_prem_cls': '', 'call_iv': '—', 'call_per1y': '—',
            'call_per1y_cls': '', 'call_per1y_title': '', 'call_perc': '—',
            'call_perc_cls': '', 'call_perc_title': '', 'call_pera': '—',
            'call_pera_cls': '', 'call_pera_title': '', 'call_gamma': '—',
            'call_delta': '—', 'call_delta_cls': '', 'call_tv_ann': '—',
            'call_tv_ann_cls': '', 'call_time_val': '—', 'call_time_cls': '',
            'call_eff_px': '—', 'call_disc_prem_pct': '—', 'call_disc_cls': '',
            'call_spread': '—', 'put_spread': '—', 'put_eff_px': '—',
            'put_disc_prem_pct': '—', 'put_disc_cls': '', 'put_time_val': '—',
            'put_time_cls': '', 'put_tv_ann': '—', 'put_tv_ann_cls': '',
            'put_delta': '—', 'put_delta_cls': '', 'put_gamma': '—',
            'put_pera': '—', 'put_pera_cls': '', 'put_pera_title': '',
            'put_perc': '—', 'put_perc_cls': '', 'put_perc_title': '',
            'put_per1y': '—', 'put_per1y_cls': '', 'put_per1y_title': '',
            'put_iv': '—', 'put_prem_pct': '—', 'put_prem_cls': '',
            'put_close': '—', 'put_live': False, 'put_last_age_cls': '',
            'put_px_title': '', 'put_ts_code': '', 'put_vol': '—',
            'put_vol_cls': '', 'put_vol_age_cls': '', 'put_oi': '—',
            'put_oi_cls': '', 'put_oi_age_cls': '',
        }],
    }]
    with app.app_context():
        html = app.jinja_env.get_template('rt_monitor_opt_t_board_body.html').render(
            opt_t_sections=payloadSections,
            opt_t_error=None,
            opt_t_row_count=1,
            opt_t_obligation_selector=None,
            opt_t_rights_selector=None,
            opt_t_term_gate={
                'text': '期限 正挂 -2.7pp（当月 35.3 → 下月 38.1） · 溢仍为正：当月',
                'title': '当月中轴IV − 下月中轴IV',
                'cls': 'opt-t-term--contango',
            },
        )
    assert 'opt-t-term--contango' in html
    assert '溢仍为正：当月' in html
    assert '1σ ±0.269' in html
    assert 'opt-t-em-edge' in html
    assert '1σ带内' in html
