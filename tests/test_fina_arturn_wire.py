# coding: utf-8
from __future__ import annotations

import gzip
import json
import unittest

from rt_monitor.services.fina_arturn_queries import (
    _fina_arturn_wire_cache,
    ensureFinaArturnWireGzip,
)


def _samplePayload(endDate: str = '20260630', hint: str = '') -> dict:
    return {
        'rows': [
            {'ts_code': '600000.SH', 'sec_name': '浦发银行', 'p0': 1.25},
            {'ts_code': '000001.SZ', 'sec_name': '平安银行', 'p0': None},
        ],
        'periods': [endDate],
        'contract_periods': [endDate],
        'selected_end_date': endDate,
        'end_dates': [endDate, '20260331'],
        'data_version': f'{endDate}|sb_act_12|2',
        'cache_hit': True,
        'hint': hint,
        'last_update': '2026-09-03 08:52:00',
    }


class FinaArturnWireTest(unittest.TestCase):
    def setUp(self):
        _fina_arturn_wire_cache.clear()

    def tearDown(self):
        _fina_arturn_wire_cache.clear()

    def testEncodeReuseSameBytes(self):
        payload = _samplePayload()
        first = ensureFinaArturnWireGzip(payload)
        second = ensureFinaArturnWireGzip(payload)
        self.assertIsNotNone(first)
        self.assertIs(first, second)
        body = json.loads(gzip.decompress(first).decode('utf-8'))
        self.assertEqual(body['selected_end_date'], '20260630')
        self.assertEqual(len(body['rows']), 2)
        self.assertEqual(body['rows'][0]['sec_name'], '浦发银行')

    def testHintMissRebuilds(self):
        a = ensureFinaArturnWireGzip(_samplePayload(hint=''))
        b = ensureFinaArturnWireGzip(_samplePayload(hint='switched'))
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertIsNot(a, b)

    def testErrorPayloadSkipped(self):
        self.assertIsNone(ensureFinaArturnWireGzip({'error': 'boom', 'rows': []}))
        self.assertIsNone(ensureFinaArturnWireGzip({}))


if __name__ == '__main__':
    unittest.main()
