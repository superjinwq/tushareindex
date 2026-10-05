# coding: utf-8
"""权利仓观察 HARD 门与矩阵。"""
from rt_monitor.services.opt_t_board import (
    _optRightsIsRepair,
    _optRightsParityHit,
    _optRightsPerHard,
    _optRightsSkewBlocks,
    _optRightsVolTenorOk,
    _opt_t_board_build_rights_selector,
)


def _axis(
    *,
    callPer=(9.0, 9.0, 14.0),
    putPer=(9.0, 9.0, 14.0),
    hvPer=20.0,
    ivHv=1.0,
    skew=0.0,
):
    return {
        'per_call': callPer,
        'per_put': putPer,
        'hv_per': hvPer,
        'iv_hv': ivHv,
        'skew': skew,
    }


def _row(
    strike: float,
    *,
    atm: bool = False,
    callDelta: str = '—',
    putDelta: str = '—',
    callDisc: str = '1.00',
    putDisc: str = '1.00',
    callTv: str = '0.0100',
    putTv: str = '0.0100',
    callMode: str = 'sell',
    putMode: str = 'sell',
    mny: str = '0.00',
    vol: float = 10.0,
    oi: float = 10.0,
    callPer: tuple[str, str, str] | None = None,
    putPer: tuple[str, str, str] | None = None,
) -> dict:
    # 默认给「过 HARD」的便宜 Per；需区分档位时用 callPer/putPer 覆盖
    cPer = callPer or ('8.0', '9.0', '14.0')
    pPer = putPer or ('8.5', '9.0', '14.0')
    return {
        'strike': strike,
        'strike_display': f'{strike:.2f}',
        'is_adj': False,
        'is_atm': atm,
        'moneyness_pct': mny,
        'moneyness_cls': '',
        'call_delta': callDelta,
        'put_delta': putDelta,
        'call_disc_prem_pct': callDisc,
        'put_disc_prem_pct': putDisc,
        'call_time_val': callTv,
        'put_time_val': putTv,
        'call_action_mode': callMode,
        'put_action_mode': putMode,
        'call_vol_n': vol,
        'put_vol_n': vol,
        'call_oi_n': oi,
        'put_oi_n': oi,
        'call_vol': '10',
        'put_vol': '10',
        'call_oi': '10',
        'put_oi': '10',
        'call_per1y': cPer[0],
        'call_perc': cPer[1],
        'call_pera': cPer[2],
        'put_per1y': pPer[0],
        'put_perc': pPer[1],
        'put_pera': pPer[2],
        'call_per1y_cls': 'opt-t-ivr-lo',
        'put_per1y_cls': 'opt-t-ivr-mid',
    }


def _sections(dteNear: int = 16) -> list[dict]:
    """标准网格：含实三–虚四；ATM=100。"""
    strikes = [75.0, 80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0, 125.0]
    rows = []
    for strike in strikes:
        # 虚一与平值用不同便宜 Per，确认展示抄该档而非中轴
        if strike == 100.0:
            callPer = ('8.0', '9.0', '14.0')
            putPer = ('8.5', '9.0', '14.0')
        elif strike == 105.0:
            callPer = ('7.0', '9.0', '14.0')
            putPer = ('7.5', '9.0', '14.0')
        else:
            callPer = ('8.0', '9.0', '14.0')
            putPer = ('8.5', '9.0', '14.0')
        rows.append(
            _row(
                strike,
                atm=strike == 100.0,
                callDelta='0.51' if strike == 100.0 else ('0.21' if strike == 105.0 else '0.08'),
                putDelta='-0.49' if strike == 100.0 else ('-0.21' if strike == 95.0 else '-0.08'),
                mny='0.00' if strike == 100.0 else '1.00',
                callPer=callPer,
                putPer=putPer,
            )
        )
    return [
        {
            'expiry_kind': kind,
            'selector_days_to_expiry': dte,
            'rows': rows,
        }
        for kind, dte in (
            ('near', dteNear),
            ('next', 37),
            ('quarter', 93),
            ('next_quarter', 184),
        )
    ]


def test_per_hard_two_below_10_and_max_below_15() -> None:
    assert _optRightsPerHard(9.0, 9.0, 14.9) is True
    assert _optRightsPerHard(9.0, 14.9, 9.0) is True
    assert _optRightsPerHard(9.0, 11.0, 14.0) is False
    assert _optRightsPerHard(9.0, 9.0, 15.0) is False
    assert _optRightsPerHard(9.0, 9.0, None) is False
    assert _optRightsPerHard(10.0, 9.0, 9.0) is True


def test_skew_and_repair_are_hard_vetoes() -> None:
    assert _optRightsSkewBlocks('call', -2.0) is True
    assert _optRightsSkewBlocks('call', -1.99) is False
    assert _optRightsSkewBlocks('call', None) is True
    assert _optRightsSkewBlocks('put', 2.5) is True
    assert _optRightsSkewBlocks('put', 2.49) is False
    assert _optRightsIsRepair(70.0, -5.0) is True
    assert _optRightsIsRepair(69.9, -5.0) is False
    assert _optRightsIsRepair(70.0, -4.9) is False


def test_near_month_requires_dte_above_15_and_own_skew() -> None:
    axis = _axis(skew=-2.0)
    assert _optRightsVolTenorOk('near', 15, 'call', _axis()) is False
    assert _optRightsVolTenorOk('near', 16, 'call', _axis()) is True
    assert _optRightsVolTenorOk('near', None, 'call', _axis()) is False
    assert _optRightsVolTenorOk('next', 10, 'call', _axis()) is True
    assert _optRightsVolTenorOk('quarter', 93, 'call', axis) is False
    assert _optRightsVolTenorOk('next', 37, 'put', _axis(skew=2.5)) is False
    assert _optRightsVolTenorOk('next', 37, 'call', _axis(hvPer=80.0, ivHv=-6.0)) is False
    # 中轴 Per 贵不再关期限门
    assert _optRightsVolTenorOk(
        'next', 37, 'call', _axis(callPer=(4.8, 18.8, 12.5))
    ) is True


def test_vol_matrix_lists_abs_delta_above_0_2_and_marks_atm_band() -> None:
    selector = _opt_t_board_build_rights_selector(
        _sections(16),
        {kind: _axis() for kind in ('near', 'next', 'quarter', 'next_quarter')},
    )
    assert [row['label'] for row in selector['vol']['call']['rows']] == [
        '平值', '虚一',
    ]
    assert [row['label'] for row in selector['vol']['put']['rows']] == [
        '虚一', '平值',
    ]
    callAtm = next(row for row in selector['vol']['call']['rows'] if row['label'] == '平值')
    assert [cell['status'] for cell in callAtm['cells']] == ['ready'] * 4
    assert callAtm['cells'][0]['show_per'] is True
    assert callAtm['cells'][0]['per1y'] == '8.0'
    assert callAtm['cells'][0]['perc'] == '9.0'
    assert callAtm['cells'][0]['is_wing_pass'] is False
    assert callAtm['cells'][0]['axis_note'] == ''
    assert 'atm_band' not in callAtm['cells'][0]
    callOtm1 = next(row for row in selector['vol']['call']['rows'] if row['label'] == '虚一')
    assert callOtm1['cells'][1]['status'] == 'ready'
    assert callOtm1['cells'][1]['per1y'] == '7.0'
    assert callOtm1['cells'][1]['per1y'] != callAtm['cells'][1]['per1y']
    assert all(row['label'] != '虚二' for row in selector['vol']['call']['rows'])

    putAtm = next(row for row in selector['vol']['put']['rows'] if row['label'] == '平值')
    assert putAtm['cells'][0]['status'] == 'ready'
    assert putAtm['cells'][0]['per1y'] == '8.5'


def test_vol_uses_row_per_not_mid_axis_and_marks_wing_pass() -> None:
    """中轴贵、该档便宜 → 仍列出，并标翼部过门；该档四期限都贵 → 整行不出。"""
    sections = _sections(16)
    for sec in sections:
        for row in sec['rows']:
            if row['strike'] == 105.0:
                row.update({
                    'call_per1y': '0.8',
                    'call_perc': '12.4',
                    'call_pera': '8.1',
                    'call_delta': '0.36',
                })
            if row['strike'] == 100.0:
                row.update({
                    'call_per1y': '40.0',
                    'call_perc': '50.0',
                    'call_pera': '45.0',
                })
    expensiveAxis = {
        kind: _axis(callPer=(4.8, 18.8, 12.5))
        for kind in ('near', 'next', 'quarter', 'next_quarter')
    }
    selector = _opt_t_board_build_rights_selector(sections, expensiveAxis)
    callOtm1 = next(row for row in selector['vol']['call']['rows'] if row['label'] == '虚一')
    assert callOtm1['cells'][1]['status'] == 'ready'
    assert callOtm1['cells'][1]['per1y'] == '0.8'
    assert callOtm1['cells'][1]['is_wing_pass'] is True
    assert callOtm1['cells'][1]['axis_note'] == '翼部过门、中轴未过'
    assert all(row['label'] != '平值' for row in selector['vol']['call']['rows'])


def test_vol_hides_buckets_with_no_ready_tenor() -> None:
    """四期限都未入选的档不出行；整表没有入选时只剩表头数据。"""
    sections = _sections(16)
    for sec in sections:
        for row in sec['rows']:
            row['call_delta'] = '0.08'
            row['put_delta'] = '-0.08'
    selector = _opt_t_board_build_rights_selector(
        sections,
        {kind: _axis() for kind in ('near', 'next', 'quarter', 'next_quarter')},
    )
    assert selector['vol']['call']['rows'] == []
    assert selector['vol']['put']['rows'] == []
    assert selector['columns']


def test_near_month_dte_15_is_blank_while_later_tenors_stay() -> None:
    selector = _opt_t_board_build_rights_selector(
        _sections(15),
        {kind: _axis() for kind in ('near', 'next', 'quarter', 'next_quarter')},
    )
    callAtm = next(row for row in selector['vol']['call']['rows'] if row['label'] == '平值')
    assert [cell['status'] for cell in callAtm['cells']] == ['empty', 'ready', 'ready', 'ready']


def test_parity_requires_discount_buy_mode_liquidity_and_deep_itm() -> None:
    base = _row(90.0, atm=False, mny='-12.00', callDelta='0.70', callMode='buy',
                callDisc='-2.00', callTv='-0.0100')
    assert _optRightsParityHit(base, 'call') is True
    shallow = dict(base, moneyness_pct='-9.00', call_delta='0.79')
    assert _optRightsParityHit(shallow, 'call') is False
    byDelta = dict(base, moneyness_pct='-9.00', call_delta='0.80')
    assert _optRightsParityHit(byDelta, 'call') is True
    notBuy = dict(base, call_action_mode='sell')
    assert _optRightsParityHit(notBuy, 'call') is False
    thinDisc = dict(base, call_disc_prem_pct='-1.99')
    assert _optRightsParityHit(thinDisc, 'call') is False
    flatTv = dict(base, call_time_val='0.0000')
    assert _optRightsParityHit(flatTv, 'call') is False
    dry = dict(base, call_vol_n=0, call_oi_n=0)
    assert _optRightsParityHit(dry, 'call') is False
    put = _row(110.0, mny='12.00', putDelta='-0.70', putMode='buy',
               putDisc='-2.10', putTv='-0.0200')
    assert _optRightsParityHit(put, 'put') is True
    putShallow = dict(put, moneyness_pct='9.00', put_delta='-0.79')
    assert _optRightsParityHit(putShallow, 'put') is False


def test_parity_matrix_ignores_short_dte_and_hides_vol_cells() -> None:
    sections = _sections(2)
    for sec in sections:
        for row in sec['rows']:
            if row['strike'] == 90.0:
                row.update({
                    'call_delta': '0.91',
                    'call_action_mode': 'buy',
                    'call_disc_prem_pct': '-2.59',
                    'call_time_val': '-0.0200',
                    'moneyness_pct': '-20.60',
                })
    selector = _opt_t_board_build_rights_selector(sections, {})
    callItm2 = next(row for row in selector['parity']['call']['rows'] if row['label'] == '实二')
    assert [cell['status'] for cell in callItm2['cells']] == ['ready'] * 4
    assert callItm2['cells'][0]['show_per'] is False
    assert callItm2['cells'][0]['vol'] == '10'
    assert callItm2['cells'][0]['oi'] == '10'


def test_parity_includes_deep_itm_outside_selector_buckets() -> None:
    sections = _sections(30)
    deep = _row(
        50.0,
        mny='-50.00',
        callDelta='0.95',
        callMode='buy',
        callDisc='-3.00',
        callTv='-0.0500',
    )
    for sec in sections:
        if sec['expiry_kind'] == 'next_quarter':
            sec['rows'] = list(sec['rows']) + [deep]
    selector = _opt_t_board_build_rights_selector(sections, {})
    deepRow = next(row for row in selector['parity']['call']['rows'] if row['strike_display'] == '50.00')
    assert [cell['status'] for cell in deepRow['cells']] == ['missing', 'missing', 'missing', 'ready']
    assert all(row['label'] != '实深' for row in selector['vol']['call']['rows'])
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    body = (root / 'templates' / 'rt_monitor_opt_t_board_body.html').read_text(encoding='utf-8')
    page = (root / 'templates' / 'rt_monitor_opt_t_board.html').read_text(encoding='utf-8')
    assert body.index('义务仓优选') < body.index('权利仓观察')
    assert body.index('权利仓观察') < body.index('{% for sec in opt_t_sections %}')
    assert 'data-opt-rights-kind="vol"' in body
    assert 'data-opt-rights-kind="parity"' in body
    assert '>波动型</button>' in body
    assert '>平价型</button>' in body
    assert '波动型便宜' not in body
    assert '平价型便宜' not in body
    assert '仅当前标的' not in body
    assert '该档Per两列' in body
    assert 'HARD折价β' in body
    assert '翼部过门见悬停' in body
    assert 'data-opt-rights-rule="vol"' in body
    assert 'data-opt-rights-rule="parity"' in body
    assert body.count('opt-t-rights-rule') == 2
    assert 'is-wing-pass' in body
    assert 'data-opt-rights-side="call"' in body
    assert 'data-opt-rights-side="put"' in body
    assert 'data-opt-rights-panel="{{ mode }}-{{ side }}"' in body
    assert 'is-atm-band' not in body
    assert '>ATM</span>' not in body
    assert 'data-opt-rights-kind="vol"].is-active' in page
    assert 'is-wing-pass' in page
    assert 'setRightsSelector' in page
    assert 'data-opt-rights-kind' in page
    assert "rt_monitor_opt_t_board_rights_tabs" in page
    assert 'function readRightsPref(' in page
    assert 'function writeRightsPref(' in page
    assert 'applyRightsPrefFor(' in page
    assert 'persist: false' in page
    rightsCss = page.split('.opt-t-rights {', 1)[1].split('.opt-t-rights-tabs', 1)[0]
    assert 'margin-top: 0' in rightsCss
    assert 'margin-top: 8px' not in rightsCss
    assert 'gap: 1px' in page.split('#opt-t-body-root {', 1)[1].split('.opt-t-selector {', 1)[0]

# 代码更新时间：2026-09-23 07:36 义务仓与权利仓竖缝 1px（去 8px margin）
