# -*- coding: utf-8 -*-
"""各阶段模块；导入即向 core.stage.REGISTRY 注册。"""
import importlib
import os
import traceback

_PKG = os.path.dirname(os.path.abspath(__file__))
_FAILED = {}

for _f in sorted(os.listdir(_PKG)):
    if not _f.endswith(".py") or _f.startswith("_"):
        continue
    _mod = _f[:-3]
    try:
        importlib.import_module("%s.%s" % (__name__, _mod))
    except Exception:                       # noqa: BLE001
        _FAILED[_mod] = traceback.format_exc()
        print("[easy-video-prep] 阶段 %s 加载失败:\n%s" % (_mod, _FAILED[_mod]), flush=True)

LOAD_ERRORS = _FAILED
