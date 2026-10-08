# -*- coding: utf-8 -*-
"""打码阶段（scrub）：进程内调用 vendored 的 OpenScrub 引擎。

只换掉「怎么执行」这一层：旧版是 subprocess 调 openscrub.exe，本版用 importlib 把
third_party/OpenScrub/openscrub.py 当模块加载进来（全进程只加载一次），再照引擎
main() 的真实流程调用：

    parser = eng.build_parser()
    args   = parser.parse_args(argv)        # argv 由本阶段拼，类别锁死 person,face
    args   = eng._prep_args(args, parser)   # 归一化 zones / ignore-region / mode-map
    eng.run_pipeline(args, cb)              # 检测 + 渲染；cb 是本模块的桥

旧版的全部能力都保留：参数表单、模糊/忽略区域、日志清洗、进度映射、取消、审计
报告、缓存目录提示。区别只是不再有 .exe、不再有子进程和 taskkill。

模型策略（发布到 GitHub 的硬要求：**不打包任何模型**）
  * 表单里 person_model / face_model 默认留空；
  * 留空时按引擎自己的注册表（person_models.json / face_models.json，带
    download_url + sha256）用引擎的 download_model() 按需下载到
    %LOCALAPPDATA%\\OpenScrub\\models（仓库之外，不可能被 git 收进去），
    并逐字节校验 sha256；下载过程走 job 日志，可被取消；
  * 人体模型缺失 = 整个阶段不可用（available() 返回 ok=False 并写清去哪下）；
  * 人脸模型可选：缺了就退回引擎内置 YuNet（引擎自己会下 ~230 KB 那份）。

实测的硬约束（别改）：
  * --categories 只能 person,face。混入任何文字类别都会触发 OCR 模型下载
    （本机 huggingface.co 返回 502），整个任务会卡死在第一步。
  * 引擎内部是用裸 "ffmpeg" 起子进程的，所以入口处把 tool 定位到的 ffmpeg
    目录前置进 PATH，保证它跟项目其它阶段用同一个 ffmpeg。
"""
import importlib
import importlib.util
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time

from core.stage import Stage, register, sel, num, rng, chk, txt, path
from core import tool, job as jobmod

# ---- 路径 -------------------------------------------------------------------
_THIS = os.path.dirname(os.path.abspath(__file__))      # easy-video-prep/stages
_ROOT = os.path.dirname(_THIS)                          # easy-video-prep
ENGINE_DIR = os.path.join(_ROOT, "third_party", "OpenScrub")
ENGINE_FILE = os.path.join(ENGINE_DIR, "openscrub.py")
# 仓库里可能已经放了一份本地缓存（搜索用，**不往这里写**，避免模型进 git）
MODEL_DIR = os.path.join(_ROOT, "models")


def _local_appdata():
    v = os.environ.get("LOCALAPPDATA")
    if v:
        return v
    return os.path.join(os.path.expanduser("~"), "AppData", "Local")


# 下载落点定在用户目录：模型永远不落进仓库
DOWNLOAD_DIR = os.path.join(_local_appdata(), "OpenScrub", "models")
OPENSCRUB_HOME_MODELS = os.path.join(os.path.expanduser("~"), ".openscrub", "models")
ENGINE_MODELS_DIR = os.path.join(ENGINE_DIR, "models")
JOB_CACHE_DIR = os.path.join(_local_appdata(), "OpenScrub", "openscrub_jobs")
NATIVE_UI = "https://127.0.0.1:8384/"

_MODEL_FILE = {"person": "person_models.json", "face": "face_models.json"}
_ENV_VAR = {"person": "OPENSCRUB_PERSON_MODEL", "face": "OPENSCRUB_FACE_MODEL"}
_KIND_LABEL = {"person": "人体分割模型", "face": "人脸检测模型"}

# ---- 文本清洗 ---------------------------------------------------------------
# 引擎的日志里可能混进 ANSI 颜色码与 NUL（它的某些子进程输出按 UTF-16LE 落地）。
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]"           # CSI（颜色、光标、清行）
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC
    r"|\x1b[@-Z\\-_]"                        # 其它两字节转义
)

# ---- 附加参数里必须拦掉的东西（都会触发网络下载或绕过本阶段的约定）----------
_FORBIDDEN_ARGS = {
    "--categories": "类别由本阶段锁定为 person,face；混入文字类别会触发 OCR 模型下载并卡死",
    "--custom-regex": "自定义正则属于文字类别，会触发 OCR 模型下载",
    "--mrn-regex": "身份号码正则会启用文字类别并触发 OCR 模型下载",
    "--allow-names": "名单功能依赖文字类别",
    "--extra-names": "名单功能依赖文字类别",
    "--engine": "OCR 引擎参数（本阶段不做文字识别）",
    "--audio-pii": "语音转写会下载 whisper 模型",
    "--audio-pii-model": "语音转写会下载 whisper 模型",
    "--audio-pii-apply": "语音转写会下载 whisper 模型",
    "--batch": "本阶段一次只处理一个输入，批量请交给工作流",
    "--from-report": "请用「从报告重新渲染」字段",
    "--preview": "预览不产出成片；复查请用原生界面",
    "--config": "YAML profile 会覆盖本阶段的锁定参数",
}

# ---- 依赖 -------------------------------------------------------------------
# 这些是引擎自身的运行时依赖（"引擎自身的依赖不算你的依赖"，但缺了阶段就跑不动，
# 所以 available() / run() 要如实检查）。rapidfuzz 是实测补上的：person,face-only
# 的扫描收尾也会走 reverse_pass，而无条件 import rapidfuzz —— 少了会在扫描中途炸。
# pytesseract / pillow / spacy / flask 等只服务文字类别与它自己的 WebUI，本阶段不用。
_REQUIRED_MODULES = (
    ("cv2", "opencv-python"),
    ("numpy", "numpy"),
    ("onnxruntime", "onnxruntime"),
    ("rapidfuzz", "rapidfuzz"),
    ("yaml", "PyYAML"),
)
_MODULE_HINT = {
    "onnxruntime": ("缺 onnxruntime，请 pip install onnxruntime 或 "
                    "onnxruntime-directml（没有它，yolo11n-seg 这类分割模型无法出"
                    "人体轮廓，只能退化成方框）"),
    "rapidfuzz": ("缺 rapidfuzz，请 pip install rapidfuzz（引擎扫描收尾的模糊匹配"
                  "要用它，缺了整个阶段跑不完）"),
}


def _missing_modules():
    """返回 [(模块名, 建议的包名, 异常), ...]；空列表 = 依赖齐全。"""
    miss = []
    for name, pkg in _REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except Exception as ex:                        # noqa: BLE001
            miss.append((name, pkg, ex))
    return miss


# ---- 引擎加载（只加载一次）--------------------------------------------------
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def _load_engine():
    """importlib 加载引擎源码；成功一次后全进程复用。

    不改 sys.path：直接用 spec_from_file_location 指定绝对路径。
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if not os.path.isfile(ENGINE_FILE):
            raise RuntimeError(
                "OpenScrub 引擎源码缺失：%s。请确认 third_party/OpenScrub/ 完整"
                "（openscrub.py + person/face/plate_models.json + LICENSE/NOTICE）。"
                % ENGINE_FILE)
        spec = importlib.util.spec_from_file_location("openscrub_engine", ENGINE_FILE)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法为 %s 建立 import spec" % ENGINE_FILE)
        mod = importlib.util.module_from_spec(spec)
        # 必须先登记进 sys.modules：引擎里用了 dataclasses，
        # 它在 3.8 上要按 cls.__module__ 反查模块字典。
        sys.modules["openscrub_engine"] = mod
        # 别让 CPython 在 third_party/ 下写 __pycache__：那是上游目录，要逐字节
        # 保持原样（发布时也不该多出一个 .pyc）。
        dwb = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop("openscrub_engine", None)
            raise
        finally:
            sys.dont_write_bytecode = dwb
        _ENGINE = mod
        return _ENGINE


def _ensure_ffmpeg_on_path(job):
    """引擎用裸 "ffmpeg" 起子进程；把项目定位到的那个目录前置进 PATH。"""
    ff = getattr(tool, "FFMPEG", "") or ""
    d = os.path.dirname(os.path.abspath(ff)) if os.path.isabs(ff) else ""
    if not d or not os.path.isdir(d):
        return
    cur = os.environ.get("PATH") or ""
    have = [x for x in cur.split(os.pathsep) if x]
    if d.lower() in [x.lower() for x in have]:
        return
    os.environ["PATH"] = d + os.pathsep + cur
    job.log("已把 ffmpeg 目录加入 PATH：%s（引擎内部按裸 ffmpeg 调用）" % d)


# ---- 模型定位 / 下载 --------------------------------------------------------
def _registry(kind):
    """直接读 vendored 的注册表 JSON。

    available() 每次 /api/stages 都会调用，为它去加载整个引擎（要 import cv2、
    numpy）太贵；注册表只是几 KB 静态 JSON，直接读等价于引擎的
    load_model_registry(kind)（vendored 目录不是只读安装，两者读同一个文件）。
    """
    p = os.path.join(ENGINE_DIR, _MODEL_FILE.get(kind, ""))
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [m for m in (data.get("models") or []) if isinstance(m, dict)]
    except Exception:                                  # noqa: BLE001
        return []


def _registry_entry(kind):
    """推荐条目优先，否则注册表第一条。"""
    ms = _registry(kind)
    for m in ms:
        if m.get("recommended"):
            return m
    return ms[0] if ms else None


def _model_names(kind):
    """在某目录里按什么文件名找模型：引擎的传统名 + 注册表各条目的 id.onnx。"""
    names = []
    if kind == "person":
        names.append("person_yolov8.onnx")
    for m in _registry(kind):
        mid = str(m.get("id") or "").strip()
        if mid:
            names.append(mid + ".onnx")
    out, seen = [], set()
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _model_dirs():
    dirs = (DOWNLOAD_DIR, MODEL_DIR, ENGINE_MODELS_DIR, OPENSCRUB_HOME_MODELS)
    out, seen = [], set()
    for d in dirs:
        a = os.path.abspath(d)
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _find_local_model(kind):
    """按 环境变量 > 各缓存目录 的顺序找一个已存在的模型；找不到返回 None。"""
    env = os.environ.get(_ENV_VAR.get(kind, ""))
    if env and os.path.isfile(env):
        return env
    for d in _model_dirs():
        for n in _model_names(kind):
            p = os.path.join(d, n)
            try:
                if os.path.isfile(p) and os.path.getsize(p) > 10000:
                    return p
            except OSError:
                continue
    return None


def _model_brief(kind):
    """一行版：给 available() 的 detail 用。"""
    e = _registry_entry(kind) or {}
    mid = str(e.get("id") or "?")
    url = str(e.get("download_url") or "")
    return "%s → 放到 %s%s" % (
        mid, os.path.join(DOWNLOAD_DIR, mid + ".onnx"),
        ("（下载地址 %s）" % url) if url
        else "（注册表 %s 里没有 download_url）" % _MODEL_FILE.get(kind))


def _model_help(kind):
    """多行版：运行时拿不到模型时给人的指引。"""
    e = _registry_entry(kind) or {}
    mid = str(e.get("id") or "?")
    url = str(e.get("download_url")
              or "（注册表 %s 里没有 download_url）"
              % os.path.join(ENGINE_DIR, _MODEL_FILE.get(kind, "")))
    return ("去哪里拿：%s\n"
            "    放这里  ：%s\n"
            "    或这里  ：%s\n"
            "    也可以在表单的模型字段里直接填 .onnx 的绝对路径。"
            % (url, os.path.join(DOWNLOAD_DIR, mid + ".onnx"),
               os.path.join(MODEL_DIR, mid + ".onnx")))


def _download_registry_model(job, eng, bridge, kind):
    """用引擎自己的注册表下载机制按需下载推荐模型。

    返回 (path, reason)：path 非空 = 成功；否则 reason 说明为什么没下成。
    """
    try:
        reg = eng.load_model_registry(kind) or _registry(kind)
    except Exception:                                  # noqa: BLE001
        reg = _registry(kind)
    entry = next((m for m in reg
                  if isinstance(m, dict) and m.get("recommended")), None)
    if entry is None and reg:
        entry = reg[0]
    if not entry:
        return None, "注册表 %s 里没有 %s 模型条目" % (_MODEL_FILE.get(kind), kind)
    url = str(entry.get("download_url") or "")
    if not url or url.upper() == "TODO_VERIFY":
        return None, "注册表条目 %s 还没有可用的 download_url" % entry.get("id")

    def _on_chunk(frac):
        job.check()          # 下载也能取消（每 64 KB 一次，纯布尔检查，不拖慢）

    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(30.0)   # 引擎的 download_model 没设超时，防断网卡死
    try:
        dest = eng.download_model(entry, kind, dest_dir=DOWNLOAD_DIR,
                                  cb=bridge, progress=_on_chunk)
        return dest, ""
    finally:
        socket.setdefaulttimeout(old)


def _dir_size(root, budget=3000):
    """有界统计目录占用：最多数 budget 个文件，避免卡在几万个缩略图上。"""
    total = 0
    n = 0
    if not os.path.isdir(root):
        return 0, 0, False
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
            n += 1
            if n >= budget:
                return total, n, True
    return total, n, False


# ---- 引擎回调桥 -------------------------------------------------------------
class _Bridge(object):
    """把引擎的 Callbacks 接到 job 上。

    引擎只用到 log / progress / cancelled（还读一个 wants_frames），
    不需要继承它的 Callbacks 类 —— 那样反而把两个模块更死地绑在一起。
    """

    wants_frames = False          # 引擎会直接读这个属性（除非要拿标注帧）

    def __init__(self, job):
        self.job = job
        self._n = 0               # progress 调用计数
        self._val = 0.0           # 只允许进度前进

    # log：剥掉 ANSI / NUL；引擎日志自带前导空格（缩进有意义），原样保留
    def log(self, msg):
        s = _ANSI_RE.sub("", str(msg).replace("\x00", "")).rstrip()
        if s.strip():
            self.job.log(s)

    # progress：post/scan -> 0.05~0.60，render -> 0.60~0.99
    #   每 40 次才 job.check() 一次（每帧都查会拖慢），被取消就抛 Cancelled
    def progress(self, stage, done, total):
        self._n += 1
        if self._n % 40 == 0:
            self.job.check()
        try:
            done = max(0, int(done))
            total = max(1, int(total))
        except (TypeError, ValueError):
            return
        frac = min(1.0, done / float(total))
        if stage == "render":
            val = 0.60 + 0.39 * frac
            step = "渲染 %d/%d（%d%%）" % (done, total, int(frac * 100))
        else:
            val = 0.05 + 0.55 * frac
            step = "检测扫描" if stage == "scan" else "收尾回溯 / 去重"
        if val > self._val:
            self._val = val
            self.job.set_progress(val, step=step)

    # cancelled：引擎在长循环里自己查它并抛 PipelineCancelled ——
    #   这是它设计好的取消路径，会带上它自己的清理（关句柄、删半成品）
    def cancelled(self):
        return bool(self.job.cancelled())


@register
class ScrubStage(Stage):
    key = "scrub"
    name = "打码"
    icon = "▩"
    order = 40
    desc = ("人体轮廓 / 人脸遮挡，进程内调用 vendored 的 OpenScrub 引擎（未修改其源码）；"
            "类别锁定 person,face，成片可在原生界面人工复查")
    accepts = "video"
    produces = "video"
    uses_zones = True          # 预览里可框选"模糊区 / 忽略区"

    # ---- 表单 ---------------------------------------------------------------
    def schema(self):
        return [
            sel("coverage", "遮挡范围", [
                ("tight", "tight · 人体轮廓（最好看，默认）"),
                ("box", "box · 检测框（藏住体形）"),
                ("concealed", "concealed · 滑动大框（连步态也藏）"),
            ], default="tight",
                help="决定人体这类被跟踪目标的遮盖形状；人脸不受影响"),

            sel("mode", "遮挡方式", [
                ("blur", "blur · 模糊（默认）"),
                ("box", "box · 纯黑块（不可逆）"),
                ("mosaic", "mosaic · 马赛克"),
                ("inpaint", "inpaint · 内容填充（最慢）"),
            ], default="blur",
                help="box/mosaic 不可恢复；blur 观感最好"),

            sel("face_shape", "人脸遮罩形状", [
                ("ellipse", "ellipse · 椭圆（贴合脸型，不糊到背景角）"),
                ("rect", "rect · 矩形（经典方块）"),
            ], default="ellipse", help="文字区域永远是矩形，不受此项影响"),

            rng("person_threshold", "人体阈值", 0.50, 0.05, 0.95, 0.05,
                help="调低召回更多（错糊风险↑），调高更严格"),

            rng("face_threshold", "人脸阈值", 0.60, 0.05, 0.95, 0.05,
                help="同上；漏脸比错糊更严重时调低"),

            num("pad", "模糊外扩(px)", 8, 0, 200, 2,
                help="在紧贴轮廓之外再多糊几像素，防止边缘漏出；默认 8"),

            sel("hdr_output", "HDR 输出", [
                ("sdr", "sdr · 色调映射到 SDR BT.709（默认，下游兼容性好）"),
                ("match", "match · 保持 HDR（10bit HEVC，不丢色彩）"),
            ], default="sdr",
                help="源是 HDR(HLG/PQ) 时才有区别；想零损失选 match，想省心选 sdr"),

            sel("encoder", "编码器", [
                ("auto", "auto · 有 GPU 用 NVENC/QSV，否则 x264（默认）"),
                ("nvenc", "nvenc · 强制 NVIDIA 硬件编码"),
                ("qsv", "qsv · 强制 Intel 硬件编码"),
                ("x264", "x264 · 强制 CPU 软编（最慢、最稳）"),
            ], default="auto", help="实测本机 auto 选中 h264_nvenc"),

            sel("out_quality", "成片画质", [
                ("archival", "archival · 视觉无损（最大，默认）"),
                ("balanced", "balanced · 约 1/3 体积"),
                ("share", "share · 约 1/8 体积，适合发送"),
            ], default="archival", help="打码后还要再剪辑/压缩，建议保持 archival"),

            sel("codec", "编码格式", [
                ("h264", "h264 · 到处都能播（默认）"),
                ("hevc", "hevc · H.265，更小；HDR 输出强制 10bit HEVC"),
            ], default="h264", help="hevc 部分老播放器/剪辑软件不支持"),

            num("detect_scale", "人脸检测缩放", 1.0, 0.2, 1.0, 0.1,
                help="在缩小副本上跑人脸检测以提速（0.5=半分辨率）；不影响输出画质"),

            chk("face_heads", "连转头/后脑一起糊", False,
                help="用人体检测推导头部区域并入人脸类别，更保险但更慢"),

            chk("report", "写 JSON 审计报告", True,
                help="报告可编辑（把不该糊的 enabled 改成 false），再用下面的字段重出片"),

            path("from_report", "从报告重新渲染（可选）", "", "file", ext=".json",
                 help="填上一轮的报告 JSON：跳过检测直接重渲染，用于人工复查后重出片"),

            path("person_model", "人体分割模型", "", "file", ext=".onnx",
                 help="留空 = 首次运行按 person_models.json 自动下载推荐模型"
                      "（yolo11n-seg-person.onnx，约 11 MB，sha256 校验）到用户目录；"
                      "也可填本地 .onnx 路径。缺它则整个阶段不可用"),

            path("face_model", "人脸检测模型", "", "file", ext=".onnx",
                 help="留空 = 先用本地缓存的人脸模型，没有就用引擎内置 YuNet"
                      "（零配置，引擎会自己下 ~230 KB）；也可填 .onnx 路径"),

            txt("regions", "模糊 / 忽略区域（由预览框选生成）", "", multiline=True,
                help="JSON 数组，每项 {\"x\",\"y\",\"w\",\"h\",\"mode\"}，坐标 0~1 归一化。\n"
                     "mode=\"blur\" → 只在这些框里检测 person/face 并模糊（= 选择要糊的对象）；\n"
                     "mode=\"ignore\" → 这些框绝不模糊（= 保留对象）。\n"
                     "在预览画面上直接框选 / 拖动即可生成，不需要手写。"),

            txt("extra_args", "附加参数（可选）", "",
                help="原样追加给 OpenScrub 引擎，例如 --dense-faces。"
                     "会触发下载或绕过锁定类别的参数被禁用", multiline=True),
        ]

    # ---- 依赖检查 -----------------------------------------------------------
    def available(self):
        ok = True
        bits = []

        if os.path.isfile(ENGINE_FILE):
            bits.append("引擎源码已就位（third_party/OpenScrub，原样 vendored）")
        else:
            ok = False
            bits.append("缺少引擎源码 → %s" % ENGINE_FILE)

        miss = _missing_modules()
        if miss:
            ok = False
            for name, pkg, ex in miss:
                bits.append(_MODULE_HINT.get(name)
                            or "缺 %s（%s）：%s" % (name, pkg, ex))
        else:
            bits.append("依赖已就位：cv2 / numpy / onnxruntime / rapidfuzz / yaml")

        person = _find_local_model("person")
        if person:
            bits.append("人体分割模型已就位：%s" % person)
        else:
            ok = False
            bits.append("人体分割模型未就位（缺它整个阶段不可用）：首次运行会自动按"
                        "注册表下载，或手动 %s" % _model_brief("person"))

        face = _find_local_model("face")
        if face:
            bits.append("人脸模型：%s（可选）" % face)
        else:
            bits.append("人脸模型未就位（可选）：将用引擎内置 YuNet 兜底")

        bits.append("人工复查用原生界面 %s（需自己启动 OpenScrub 的 web 版）" % NATIVE_UI)
        return {"ok": ok, "detail": "；".join(bits)}

    def meta(self):
        # Stage.describe() 会把这里的键同时抬到描述符顶层，前端用 native_ui 画按钮
        return {
            "native_ui": NATIVE_UI,
            "engine": ENGINE_FILE,
            "models": [p for p in (_find_local_model("person"),
                                   _find_local_model("face")) if p],
            "model_dirs": _model_dirs(),
            "cache_dir": JOB_CACHE_DIR,
            "locked_categories": "person,face",
        }

    # ---- 参数整理 -----------------------------------------------------------
    def _extra_args(self, opts):
        raw = str(opts.get("extra_args") or "").strip()
        if not raw:
            return []
        try:
            toks = shlex.split(raw, posix=False)
        except ValueError as ex:
            raise RuntimeError("附加参数无法解析（引号没配对？）：%s" % ex)
        clean = [t.strip().strip('"').strip("'") for t in toks]
        clean = [t for t in clean if t]
        for t in clean:
            name = t.split("=", 1)[0]
            if name in _FORBIDDEN_ARGS:
                raise RuntimeError("附加参数 %s 被禁用：%s" % (name, _FORBIDDEN_ARGS[name]))
            if name in ("-o", "--output"):
                raise RuntimeError("输出路径由本阶段决定，不能在附加参数里再指定 -o")
        return clean

    def _parse_regions(self, job, opts):
        """框选区域 -> (blur, ignore) 两组归一化框；语义与旧版完全一致。"""
        try:
            regs = json.loads(str(opts.get("regions") or "").strip() or "[]")
        except ValueError:
            regs = []
            job.log("区域 JSON 解析失败，已忽略（在预览里重新框一下）")
        blur, ign = [], []
        for r in (regs if isinstance(regs, list) else []):
            if not isinstance(r, dict):
                continue
            try:
                x = max(0.0, min(1.0, float(r["x"])))
                y = max(0.0, min(1.0, float(r["y"])))
                w = max(0.0, min(1.0 - x, float(r["w"])))
                h = max(0.0, min(1.0 - y, float(r["h"])))
            except (KeyError, TypeError, ValueError):
                continue
            if w <= 0.002 or h <= 0.002:
                continue
            box = [round(x, 4), round(y, 4), round(x + w, 4), round(y + h, 4)]
            (ign if str(r.get("mode")) == "ignore" else blur).append(box)
        return blur, ign

    def _log_report(self, job, rp):
        try:
            with open(rp, "r", encoding="utf-8", errors="replace") as fh:
                rep = json.load(fh)
        except Exception as ex:                        # noqa: BLE001
            job.log("  （审计报告读取失败：%s）" % ex)
            return
        by = {}
        for d in (rep.get("detections") or []):
            if not isinstance(d, dict) or d.get("enabled") is False:
                continue
            c = d.get("category") or "?"
            by[c] = by.get(c, 0) + 1
        prov = rep.get("provenance") or {}
        job.log("审计报告：%s" % rp)
        job.log("  实际应用：%s" % ("、".join("%s %d 处" % (k, v)
                                          for k, v in sorted(by.items())) or "无"))
        if prov.get("output_sha256"):
            job.log("  输出 SHA256：%s…" % str(prov["output_sha256"])[:16])
        job.log("  报告可直接编辑（把不该糊的检测项 enabled 改成 false），"
                "再填到「从报告重新渲染」即可跳过检测重出片。")

    # ---- 模型：不打包，留空就按注册表下载 -----------------------------------
    def _ensure_model(self, job, eng, bridge, kind, raw_opt, required):
        label = _KIND_LABEL.get(kind, kind)
        explicit = os.path.expanduser(str(raw_opt or "").strip())
        if explicit:
            if os.path.isfile(explicit):
                job.log("%s：%s（表单指定）" % (label, explicit))
                return explicit
            if required:
                raise RuntimeError(
                    "%s 表单里指定的文件不存在：%s。留空则按引擎注册表自动下载。"
                    % (label, explicit))
            job.log("（%s 表单指定的文件不存在：%s，改为自动解析）" % (label, explicit))

        env = os.environ.get(_ENV_VAR.get(kind, ""))
        if env and os.path.isfile(env):
            job.log("%s：%s（来自环境变量 %s）" % (label, env, _ENV_VAR[kind]))
            return env

        found = _find_local_model(kind)
        if found:
            job.log("%s：%s" % (label, found))
            return found

        # 本地没有 -> 用引擎自己的注册表下载机制按需下载（sha256 校验）
        job.log("%s 本地没有，按引擎注册表按需下载…" % label)
        try:
            path, why = _download_registry_model(job, eng, bridge, kind)
        except jobmod.Cancelled:
            raise
        except Exception as ex:                        # noqa: BLE001
            path, why = None, "%s: %s" % (type(ex).__name__, ex)
        if path:
            job.log("%s 下载完成（已通过 sha256 校验）：%s" % (label, path))
            return path

        if required:
            raise RuntimeError("未能获得%s（%s）。\n    %s"
                               % (label, why, _model_help(kind)))
        job.log("（%s 未就位：%s）—— 退回引擎内置 YuNet 人脸检测"
                "（引擎首次会自动下载约 230 KB 那份）。" % (label, why))
        job.log("    如需更高召回，%s" % _model_help(kind))
        return None

    # ---- 执行 ---------------------------------------------------------------
    def run(self, job, inputs, opts):
        opts = dict(opts or {})
        srcs = [x for x in (inputs or []) if x]
        if not srcs:
            raise RuntimeError("没有输入视频")
        src = os.path.abspath(srcs[0])
        if not os.path.isfile(src):
            raise RuntimeError("输入文件不存在：%s" % src)

        miss = _missing_modules()
        if miss:
            bad = [_MODULE_HINT.get(name) or "缺 %s（%s）：%s" % (name, pkg, ex)
                   for name, pkg, ex in miss]
            raise RuntimeError("打码阶段依赖不满足：" + "；".join(bad))

        eng = _load_engine()            # 引擎很重：进程内只加载一次
        _ensure_ffmpeg_on_path(job)
        bridge = _Bridge(job)

        from_report = os.path.expanduser(str(opts.get("from_report") or "").strip())
        if from_report and not os.path.isfile(from_report):
            raise RuntimeError("复查报告不存在：%s" % from_report)

        if from_report:
            # 重渲染不跑检测：不需要任何模型、也不需要联网，别在这里卡住
            job.log("从报告重新渲染：跳过模型解析（检测不参与）")
            person_model = face_model = None
        else:
            # 模型：person 必需，face 可选（缺了引擎用 YuNet 兜底）
            person_model = self._ensure_model(job, eng, bridge, "person",
                                              opts.get("person_model"), True)
            face_model = self._ensure_model(job, eng, bridge, "face",
                                            opts.get("face_model"), False)

        coverage = str(opts.get("coverage") or "tight")
        mode = str(opts.get("mode") or "blur")
        face_shape = str(opts.get("face_shape") or "ellipse")
        hdr_output = str(opts.get("hdr_output") or "sdr")
        encoder = str(opts.get("encoder") or "auto")
        out_quality = str(opts.get("out_quality") or "archival")
        codec = str(opts.get("codec") or "h264")
        face_heads = bool(opts.get("face_heads"))
        want_report = opts.get("report")
        want_report = True if want_report is None else bool(want_report)

        def _f(key, dflt):
            try:
                return float(opts.get(key))
            except (TypeError, ValueError):
                return float(dflt)

        pt = min(0.99, max(0.01, _f("person_threshold", 0.5)))
        ft = min(0.99, max(0.01, _f("face_threshold", 0.6)))
        pad = int(max(0, min(200, _f("pad", 8))))
        dscale = min(1.0, max(0.2, _f("detect_scale", 1.0)))

        # 输出目录：opts 优先，WebUI 自动跑时 opts 里没有 outdir，就落在源文件旁边
        outdir = str(opts.get("outdir") or "").strip() or os.path.dirname(src)
        stem = tool.safe_name(os.path.splitext(os.path.basename(src))[0])
        out = tool.unique_out(outdir, stem + "_scrub", ".mp4")
        report_path = tool.unique_out(outdir, stem + "_scrub_report", ".json")

        # ---- 开跑前的环境说明 ----
        info = tool.probe(src)
        dur = 0.0
        if info:
            dur = float(info["duration"] or 0.0)
            job.log("输入 %s · %dx%d · %s · %s · %.2f fps%s"
                    % (os.path.basename(src), info["width"], info["height"],
                       tool.human_dur(dur), tool.human_size(info["size"]), info["fps"],
                       " · HDR(%s)" % (info["transfer"] or "?") if info["hdr"] else ""))
            if info["rotation"]:
                job.log("  · 检测到旋转元数据 %d°：属正常情况，引擎自行解码/编码，"
                        "本阶段不做任何翻转也不报错" % info["rotation"])
            if info["hdr"]:
                if hdr_output == "match":
                    job.log("  · 源是 HDR：输出保持 HDR（10bit HEVC，色彩不丢）")
                else:
                    job.log("  · 源是 HDR：输出将色调映射为 SDR BT.709"
                            "（想零损失请把 HDR 输出改成 match）")
        else:
            job.log("（无法探测输入信息，继续执行）")

        size, nfiles, trunc = _dir_size(JOB_CACHE_DIR)
        if nfiles:
            job.log("提示：用原生界面人工复查时，OpenScrub 会在 %s 堆积缩略图缓存，"
                    "当前约 %s%s（已数 %d 个文件）；复查完可整个删除该目录释放 C 盘。"
                    % (JOB_CACHE_DIR, tool.human_size(size), " 以上" if trunc else "", nfiles))
        else:
            job.log("提示：用原生界面人工复查时会在 %s 产生缓存，注意清理 C 盘。"
                    % JOB_CACHE_DIR)

        # ---- 拼 argv（等价于旧版命令行，只是不再有 .exe）----
        argv = [src]
        if from_report:
            job.log("模式：从报告重新渲染（跳过检测）—— %s" % from_report)
            # 类别同样锁死；这条路径不跑检测，加上只是把锁写成无条件的
            argv += ["--categories", "person,face"]
            argv += ["--from-report", from_report,
                     "--encoder", encoder, "--out-quality", out_quality,
                     "--codec", codec]
        else:
            # 类别锁死 person,face：这是本阶段最重要的安全约束
            argv += ["--categories", "person,face"]
            argv += ["--person-model", person_model]
            if face_model:
                argv += ["--face-model", face_model]
            argv += ["--coverage", coverage, "--mode", mode,
                     "--face-shape", face_shape]
            argv += ["--person-threshold", "%.2f" % pt,
                     "--face-threshold", "%.2f" % ft]
            argv += ["--pad", str(pad)]
            argv += ["--hdr-output", hdr_output, "--encoder", encoder,
                     "--out-quality", out_quality, "--codec", codec]
            argv += ["--detect-scale", "%.2f" % dscale]
            if face_heads:
                argv.append("--face-heads")
            if want_report:
                argv += ["--report", report_path]

        # ---- 模糊 / 忽略区域 ----
        # 映射到引擎的两个开关：
        #   mode=blur   → --zones：只在这些框里检测，等于"选中要糊的对象"
        #   mode=ignore → --ignore-region：这些框绝不模糊
        blur, ign = self._parse_regions(job, opts)
        zones_file = None
        if blur and not from_report:
            zf = os.path.join(tempfile.gettempdir(),
                              "easy-video-prep_zones_%d.json" % os.getpid())
            try:
                with open(zf, "w", encoding="utf-8") as fh:
                    json.dump({"person": blur, "face": blur}, fh)
                argv += ["--zones", zf]
                zones_file = zf
                job.log("模糊区域 %d 个：只在这些框内检测 person/face（%s）"
                        % (len(blur), zf))
            except OSError as ex:
                job.log("写区域文件失败，已跳过：%s" % ex)
        for b in ign:
            argv += ["--ignore-region", "%.5f,%.5f,%.5f,%.5f"
                     % (b[0], b[1], b[2], b[3])]
        if ign:
            job.log("忽略区域 %d 个：这些地方绝不模糊" % len(ign))
        if ign and from_report:
            job.log("提示：从报告重渲染时区域参数不生效（那次运行没有走检测）")

        argv += self._extra_args(opts)
        argv += ["-o", out]

        parser = eng.build_parser()
        try:
            args = parser.parse_args(argv)
        except SystemExit as ex:
            raise RuntimeError("组装引擎参数失败（parse_args 退出码 %s）：%s"
                               % (ex.code, subprocess.list2cmdline(argv)))
        args = eng._prep_args(args, parser)      # 归一化 zones / ignore_region 等

        job.log("进程内调用引擎：" + subprocess.list2cmdline(argv))
        job.set_progress(0.05, step="开始检测（进程内调用引擎）")
        t0 = time.time()
        try:
            eng.run_pipeline(args, bridge)
        except eng.PipelineCancelled:
            job.log("已取消：引擎在自己的检查点退出（进程内，无需清理子进程）")
            raise jobmod.Cancelled()
        except jobmod.Cancelled:
            raise
        except RuntimeError as ex:
            raise RuntimeError("OpenScrub 引擎报错：%s" % ex)
        except Exception as ex:                  # noqa: BLE001
            raise RuntimeError("OpenScrub 引擎执行失败：%s: %s"
                               % (type(ex).__name__, ex))
        finally:
            if zones_file and os.path.isfile(zones_file):
                try:
                    os.remove(zones_file)
                except OSError:
                    pass

        el = time.time() - t0
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise RuntimeError(
                "引擎正常结束但没有产出成片：%s（用时 %.1fs）。请回看上面最后几条"
                "日志：若提示模型加载失败，检查模型路径/显卡驱动；"
                "若卡在第一步，多半是误开了文字类别（本阶段不允许）。" % (out, el))

        if want_report and not from_report and os.path.isfile(report_path):
            self._log_report(job, report_path)

        job.set_progress(1.0, step="完成")
        job.log("完成：%s（%s，用时 %.1fs）"
                % (out, tool.human_size(os.path.getsize(out)), el))
        return out
