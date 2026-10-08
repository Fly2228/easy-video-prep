# -*- coding: utf-8 -*-
"""工作区状态 + 阶段串联（工作流）执行器。"""
import os
import threading

from . import job as jobmod
from .stage import get as get_stage

STATE = {
    "root": "",        # 素材浏览根目录
    "outdir": "",      # 输出目录
    "current": "",     # 当前"手上"的视频（上一阶段产物）
    "history": [],     # 产物链，最新在前
}
_LOCK = threading.Lock()


def update(**kw):
    with _LOCK:
        for k, v in kw.items():
            if v is not None:
                STATE[k] = v
    return snapshot()


def push_output(path):
    with _LOCK:
        if path:
            STATE["current"] = path
            h = [x for x in STATE["history"] if x != path]
            h.insert(0, path)
            STATE["history"] = h[:40]
    return path


def snapshot():
    with _LOCK:
        s = dict(STATE)
    s["history"] = list(s.get("history") or [])
    return s


class _Proxy(object):
    """把子阶段的 0..1 进度映射到整体进度的某一段，并把日志加个前缀。"""

    def __init__(self, parent, base, span, idx, total, name):
        self._p = parent
        self._base = base
        self._span = span
        self._name = name
        self.idx = idx
        self.total = total

    @property
    def id(self):
        return self._p.id

    def log(self, msg):
        self._p.log(msg)

    def set_progress(self, p, step=None):
        self._p.set_progress(self._base + self._span * max(0.0, min(1.0, float(p))),
                             step="[%d/%d %s] %s" % (self.idx, self.total, self._name, step or ""))

    def check(self):
        return self._p.check()

    def cancelled(self):
        return self._p.cancelled()

    def add_output(self, p):
        self._p.add_output(p)

    def __setattr__(self, k, v):
        if k.startswith("_"):
            object.__setattr__(self, k, v)
        else:
            setattr(self._p, k, v)

    def __getattr__(self, k):
        return getattr(self._p, k)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [x for x in v if x]
    return [v]


def run_flow(job, inputs, steps):
    """按顺序执行 steps=[{"stage":key,"opts":{...}}, ...]，
    每一步的第一个产物作为下一步的输入。"""
    cur = _as_list(inputs)
    if not cur:
        raise ValueError("没有输入文件")
    total = len(steps)
    if total == 0:
        raise ValueError("工作流是空的")
    for i, st in enumerate(steps):
        job.check()
        key = st.get("stage")
        stage = get_stage(key)
        opts = dict(stage.defaults())
        opts.update(st.get("opts") or {})
        base = float(i) / total
        span = 1.0 / total
        job.log("── 阶段 %d/%d：%s ──" % (i + 1, total, stage.name))
        proxy = _Proxy(job, base, span, i + 1, total, stage.name)
        out = stage.run(proxy, cur, opts)
        outs = _as_list(out)
        if not outs:
            raise RuntimeError("阶段「%s」没有产出文件" % stage.name)
        for p in outs:
            job.add_output(p)
        cur = [outs[0]]
        push_output(outs[0])
        job.log("   → %s" % os.path.basename(outs[0]))
    return cur
