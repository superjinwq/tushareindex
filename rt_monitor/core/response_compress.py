"""Flask 响应 gzip 压缩（大 JSON API 等）。"""
from __future__ import annotations

import gzip

from flask import Request, Response

MIN_GZIP_BYTES = 1024
GZIP_LEVEL = 6


def maybeGzipFlaskResponse(resp: Response, req: Request | None) -> Response:
    """对 JSON / 文本响应在客户端支持 gzip 时压缩（跳过已编码与小包）。"""
    if resp.status_code < 200 or resp.status_code >= 300:
        return resp
    if resp.headers.get('Content-Encoding'):
        return resp
    if req is None:
        return resp
    acceptEnc = (req.headers.get('Accept-Encoding') or '').lower()
    if 'gzip' not in acceptEnc:
        return resp
    mime = (resp.mimetype or '').lower()
    if not (mime.startswith('application/json') or mime.startswith('text/')):
        return resp
    if resp.direct_passthrough:
        return resp
    raw = resp.get_data()
    if not raw or len(raw) < MIN_GZIP_BYTES:
        return resp
    compressed = gzip.compress(raw, compresslevel=GZIP_LEVEL)
    if len(compressed) >= len(raw):
        return resp
    resp.set_data(compressed)
    resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Content-Length'] = str(len(compressed))
    vary = resp.headers.get('Vary')
    resp.headers['Vary'] = 'Accept-Encoding' if not vary else f'{vary}, Accept-Encoding'
    return resp

# 代码更新时间：2026-07-09 09:41
