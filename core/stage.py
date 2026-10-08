# -*- coding: utf-8 -*-
"""阶段契约与注册表。

每个阶段（曝光/剪辑/补帧/打码/压缩）都实现 Stage 子类并 @register，
WebUI 从 schema() 自动生成表单，从 available() 得到"缺什么"的提示。
"""
import threading

REGISTRY = {}
_LOCK = threading.Lock()


# ---- 表单字段小工具（阶段用它们描述参数）----------------------------------
def sel(key, label, options, default=None, help=""):
    """下拉。options 形如 [("value","显示名"), ...]"""
    return {"key": key, "label": label, "type": "select",
            "options": [list(o) for o in options],
            "default": default if default is not None else options[0][0], "help": help}


def num(key, label, default, min=None, max=None, step=None, help=""):
    f = {"key": key, "label": label, "type": "number", "default": default, "help": help}
    if min is not None:
        f["min"] = min
    if max is not None:
        f["max"] = max
    if step is not None:
        f["step"] = step
    return f


def rng(key, label, default, min, max, step, help=""):
    return {"key": key, "label": label, "type": "range", "default": default,
            "min": min, "max": max, "step": step, "help": help}


def chk(key, label, default=False, help=""):
    return {"key": key, "label": label, "type": "checkbox", "default": default, "help": help}


def txt(key, label, default="", help="", multiline=False):
    return {"key": key, "label": label, "type": "textarea" if multiline else "text",
            "default": default, "help": help}


def path(key, label, default="", kind="file", help="", ext=""):
    """kind="file" 时可用 ext 限定扩展名（如 ".onnx,.pt"），

    否则文件选择器只会列视频，模型/报告这类文件根本选不到。
    """
    f = {"key": key, "label": label, "type": "path", "kind": kind,
         "default": default, "help": help}
    if ext:
        f["ext"] = ext
    return f


def show_if(fields, field, value=True):
    """给一组字段加显示条件：只有 field == value 时前端才显示。

    用来把"高级选项"收起来 —— 比如剪辑默认无损，编码/质量这类参数
    不该占着位置，只在你主动打开"允许重编码"时才出现。
    """
    for f in fields:
        f["show_if"] = {"field": field, "equals": value}
    return fields


class Stage(object):
    key = ""
    name = ""
    icon = "●"
    order = 50
    desc = ""
    accepts = "video"
    produces = "video"
    # 该阶段是否吃"时间区间"。true 时预览里会显示入/出点手柄；
    # false 时只留一个定位用的播放头，避免把剪辑的操作露给别的阶段。
    uses_range = False
    # 该阶段是否支持"画面上框选区域"（打码用：模糊区 / 忽略区）
    uses_zones = False

    def available(self):
        """返回 {"ok": bool, "detail": str}；ok=False 时前端会显著提示。"""
        return {"ok": True, "detail": ""}

    def schema(self):
        return []

    def defaults(self):
        d = {}
        for f in self.schema():
            d[f["key"]] = f.get("default")
        return d

    def describe(self):
        meta = dict(self.meta() or {})
        f = dict(self.defaults())
        f.update(meta)
        d = {"key": self.key, "name": self.name, "icon": self.icon,
             "order": self.order, "desc": self.desc,
             "accepts": self.accepts, "produces": self.produces,
             "fields": self.schema(), "defaults": f,
             "meta": meta,
             "uses_range": bool(getattr(self, "uses_range", False)),
             "uses_zones": bool(getattr(self, "uses_zones", False)),
             "quick": type(self).quick is not Stage.quick,
             "analyze": type(self).analyze is not Stage.analyze,
             "available": self.available()}
        # meta 的键**同时**抬到描述符顶层：前端用 s.native_ui 这类顶层字段
        # 驱动按钮/链接。以前只并进 defaults，导致"只实现 meta() 的阶段"
        # 前端拿不到（打码阶段踩过，一度要靠覆写 describe() 绕过去）。
        for k, v in meta.items():
            d.setdefault(k, v)
        return d

    def meta(self):
        """可选：返回额外信息（如探测到的外部程序版本）。"""
        return {}

    def analyze(self, inputs, opts):
        """可选：返回"给前端画图用"的分析数据（如曝光曲线）。

        返回 None 表示本阶段不提供图表；实现后前端会自动出现对应面板。
        """
        return None

    def quick(self, inputs, a, b):
        """可选：预览里选好区间后按 Enter 的"快捷动作"。

        返回一份要合并进默认值的 opts（前端会直接执行，产物进右侧列表），
        或 None 表示本阶段不支持快捷动作。
        """
        return None

    def run(self, job, inputs, opts):
        """执行。返回输出文件路径，或路径列表。必须定期 job.check()。"""
        raise NotImplementedError


def register(cls):
    inst = cls()
    with _LOCK:
        REGISTRY[inst.key] = inst
    return cls


def get(key):
    s = REGISTRY.get(key)
    if s is None:
        raise KeyError("未知阶段: %s" % key)
    return s


def all_stages():
    return sorted(REGISTRY.values(), key=lambda s: s.order)
