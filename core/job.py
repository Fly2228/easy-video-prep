# -*- coding: utf-8 -*-
"""统一任务引擎：所有阶段都通过它报进度、写日志、被取消。"""
import threading
import time
import uuid


class Cancelled(Exception):
    """阶段内部调用 job.check() 时若已被取消，抛这个。"""


class Job(object):
    def __init__(self, kind, label, inputs=None, meta=None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label
        self.inputs = list(inputs or [])
        self.meta = dict(meta or {})
        self.status = "pending"          # pending|running|done|failed|cancelled
        self.progress = 0.0
        self.step = ""                   # 当前子步骤文字
        self.logs = []
        self.error = None
        self.outputs = []
        self.t0 = time.time()
        self.elapsed = 0.0
        self._cancel = False
        self._lock = threading.Lock()

    # ---- 供阶段调用 ----------------------------------------------------
    def log(self, msg):
        with self._lock:
            for line in str(msg).splitlines() or [""]:
                self.logs.append("[%7.1fs] %s" % (time.time() - self.t0, line))
            if len(self.logs) > 4000:
                del self.logs[:1000]

    def set_progress(self, p, step=None):
        with self._lock:
            self.progress = max(0.0, min(1.0, float(p)))
            if step is not None:
                self.step = str(step)

    def cancel(self):
        self._cancel = True

    def cancelled(self):
        return self._cancel

    def check(self):
        """长循环里定期调用；被取消就抛 Cancelled。"""
        if self._cancel:
            raise Cancelled()
        return False

    def add_output(self, path):
        with self._lock:
            if path and path not in self.outputs:
                self.outputs.append(path)

    # ---- 快照 ----------------------------------------------------------
    def snapshot(self, tail=200):
        with self._lock:
            return {
                "id": self.id, "kind": self.kind, "label": self.label,
                "inputs": self.inputs, "status": self.status,
                "progress": round(self.progress, 4), "step": self.step,
                "error": self.error, "outputs": list(self.outputs),
                "elapsed": round(self.elapsed or (time.time() - self.t0), 2),
                "log_len": len(self.logs), "log_tail": self.logs[-tail:],
                "meta": self.meta,
            }


REGISTRY = {}
RLOCK = threading.Lock()
ORDER = []


def create(kind, label, inputs=None, meta=None):
    j = Job(kind, label, inputs, meta)
    with RLOCK:
        REGISTRY[j.id] = j
        ORDER.append(j.id)
        # 只保留最近 200 条
        while len(ORDER) > 200:
            REGISTRY.pop(ORDER.pop(0), None)
    return j


def get(jid):
    with RLOCK:
        return REGISTRY.get(jid)


def all_jobs(limit=40):
    with RLOCK:
        ids = list(ORDER)[-limit:]
    out = []
    for i in reversed(ids):
        j = REGISTRY.get(i)
        if j:
            s = j.snapshot(tail=0)
            out.append(s)
    return out


def run(job, fn):
    """在后台线程里执行 fn(job)，自动维护 status / error / elapsed。"""
    def _w():
        job.status = "running"
        t = time.time()
        try:
            r = fn(job)
            if isinstance(r, str):
                job.add_output(r)
            elif isinstance(r, (list, tuple)):
                for p in r:
                    job.add_output(p)
            job.status = "done"
            job.set_progress(1.0)
            job.log("完成")
        except Cancelled:
            job.status = "cancelled"
            job.log("已取消")
        except Exception as e:                      # noqa: BLE001
            job.status = "failed"
            job.error = "%s: %s" % (type(e).__name__, e)
            job.log("失败 -> " + job.error)
        finally:
            job.elapsed = time.time() - t
    th = threading.Thread(target=_w, daemon=True)
    th.start()
    return job
