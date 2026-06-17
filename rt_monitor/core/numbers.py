"""数值序列化小工具。"""
from __future__ import annotations


def toFloatOrNone(v):
    """将值转为 float，失败返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

# 代码更新时间：2026-06-17 22:58
