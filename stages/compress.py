# -*- coding: utf-8 -*-
"""压缩阶段 —— 重编码到「目标大小」或「目标画质（CRF/CQ）」。

移植自一个 tkinter 版视频压缩脚本的纯函数核心（界面代码全部丢弃）：
  find_tool（改用 core.tool 定位）
  parse_bitrate / crf_ratio / compute_scale / resolve_codec_key
  compute_target_bps / detect_encoders / resolve_encoder / estimate_size
  build_commands / build_preview_cmd / parse_out_time
  normalize_codec / human_size（改用 tool.human_size）/ human_duration（改用 tool.human_dur）
  probe（改用 tool.probe，并从中派生压缩需要的额外字段）

设计要点：
  * 硬件编码器优先（NVENC/AMF/QSV 实测探测），Pascal 等卡上 HEVC 10bit 会先做一次
    128x128 试编码确认，失败自动回退 libx265，不会静默产出坏文件。
  * 旋转元数据（-90/90）只影响显示方向：缩放按「显示尺寸」计算，交回 ffmpeg 的
    autorotate 处理，不当错误。
  * HDR：HLG/PQ/DV 源可选「保留 HDR(10bit HEVC)」或「zscale+tonemap 转 SDR」。
  * 目标大小模式在 CPU 编码时走两遍编码，结束后清理 passlog 临时文件。
"""
import os
import re
import shlex
import shutil
import tempfile
import threading
from collections import deque

from core.stage import Stage, register, sel, num, rng, chk, txt, path
from core import tool, job as jobmod

AUDIO_BITRATE = "128k"
PREVIEW_SECS = 12
NULL_DEV = "NUL" if os.name == "nt" else "/dev/null"
MP4_SAFE_AUDIO = ("aac", "mp3", "ac3", "eac3", "alac")

_ENCODERS = None          # detect_encoders() 缓存
_FILTER_TEXT = None       # ffmpeg -filters 缓存
_NVENC_10BIT_OK = None    # NVENC 10bit 试编码缓存


# ================================================================ 纯函数核心
def parse_bitrate(s):
    """'128k' / '2.5M' -> bps。"""
    s = str(s).strip().lower()
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1000000, s[:-1]
    elif s.endswith("g"):
        mult, s = 1000000000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def normalize_codec(codec):
    c = (codec or "").lower()
    return "h265" if c in ("h265", "hevc", "hev1", "hvc1") else "h264"


def resolve_codec_key(codec_choice, source_codec):
    """codec_choice: h264 / hevc / same。"""
    if codec_choice in ("same", "", None):
        return normalize_codec(source_codec)
    return "h265" if codec_choice == "hevc" else "h264"


def crf_ratio(crf, codec_key):
    """把 CRF 粗略映射成「相对源高码率的体积系数」，仅用于大小估算。"""
    try:
        crf = float(crf)
    except (TypeError, ValueError):
        crf = 23.0
    ratio = 10 ** ((18.0 - crf) / 12.5)
    if codec_key == "h264":
        ratio *= 1.4
    return min(ratio, 1.5)


def compute_scale(src_w, src_h, target_h):
    """按目标高度等比缩放（不放大、强制偶数）；不需要缩放返回 None。"""
    if not target_h or not src_h or not src_w or src_h <= int(target_h):
        return None
    ratio = float(target_h) / float(src_h)
    w = max(int(round(src_w * ratio / 2.0)) * 2, 2)
    h = max(int(round(src_h * ratio / 2.0)) * 2, 2)
    return (w, h)


def _display_dims(info):
    """考虑旋转元数据后的显示宽高（90/270 度时宽高互换）。"""
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    if abs(int(info.get("rotation") or 0)) % 180 == 90:
        return h, w
    return w, h


def compute_target_bps(target_mb, info):
    """按目标体积反算视频码率（扣掉音频，至少 100kbps）。"""
    dur = float(info.get("duration") or 0)
    if dur <= 0:
        raise RuntimeError("探测不到视频时长，无法按目标大小反算码率；请改用「按质量」模式")
    total_bytes = float(target_mb) * 1024 * 1024
    audio_bps = parse_bitrate(AUDIO_BITRATE) if info.get("has_audio") else 0
    audio_bytes = audio_bps * dur / 8.0
    video_bytes = max(total_bytes - audio_bytes, total_bytes * 0.1)
    return max(video_bytes * 8.0 / dur, 100000.0)


def detect_encoders(refresh=False):
    """实测 ffmpeg 自带的硬件/CPU 编码器。返回 {h264:[], h265:[], cpu:[]}。"""
    global _ENCODERS
    if _ENCODERS is not None and not refresh:
        return _ENCODERS
    result = {"h264": [], "h265": [], "cpu": []}
    rc, out, _ = tool.run([tool.FFMPEG, "-hide_banner", "-encoders"])
    txt = out if (rc == 0 and out) else ""
    if txt:
        for kind, names in (("h264", ["h264_nvenc", "h264_amf", "h264_qsv"]),
                            ("h265", ["hevc_nvenc", "hevc_amf", "hevc_qsv"]),
                            ("cpu", ["libx264", "libx265"])):
            for n in names:
                if re.search(r"(?m)^\s*\S+\s+%s\b" % re.escape(n), txt) or \
                        re.search(r"\b%s\b" % re.escape(n), txt):
                    result[kind].append(n)
    _ENCODERS = result
    return result


def resolve_encoder(codec_key, hw_choice, available_hw, log=None):
    """挑编码器。返回 (lib, is_hw, kind)；kind ∈ nvenc/amf/qsv/cpu。

    移植说明：原先返回的是写死的 flags 列表，这里只回 kind，
    flags 由 _preset_flags/_rate_flags 按「预设 + 码控」统一生成。
    """
    lib = "libx264" if codec_key == "h264" else "libx265"
    hw = list((available_hw or {}).get(codec_key) or [])
    if hw_choice == "x264":
        hw = []
    elif hw_choice == "nvenc":
        hw = [n for n in hw if "nvenc" in n]
        if not hw and log:
            log("未检测到 NVIDIA NVENC，回退 CPU 软件编码（%s）" % lib)
    if hw:
        name = hw[0]
        kind = "nvenc" if "nvenc" in name else ("amf" if "amf" in name else "qsv")
        return name, True, kind
    return lib, False, "cpu"


def estimate_size(info, plan):
    """粗略预估输出体积（字节）。target 模式直接返回目标值。"""
    dur = float(info.get("duration") or 0)
    if dur <= 0:
        return 0
    audio_bps = parse_bitrate(AUDIO_BITRATE) if (
        info.get("has_audio") and plan.get("audio_mode") != "none") else 0
    if plan.get("mode") == "target":
        return float(plan.get("target_mb") or 0) * 1024 * 1024
    src_bps = float(info.get("video_bitrate") or 0)
    scale_ratio = 1.0
    if plan.get("scale"):
        sw, sh = _display_dims(info)
        if sw and sh:
            scale_ratio = (plan["scale"][0] * plan["scale"][1]) / float(sw * sh)
    bps = src_bps * crf_ratio(plan.get("crf"), plan.get("codec_key")) * scale_ratio
    return (bps + audio_bps) * dur / 8.0


def parse_out_time(t):
    """'00:01:23.456789' -> 秒。"""
    try:
        parts = str(t).strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (ValueError, TypeError):
        pass
    return 0.0


# ---------------------------------------------------------------- 编码参数
def _preset_flags(kind, preset):
    p = preset if preset in ("fast", "medium", "slow") else "medium"
    if kind == "nvenc":
        return ["-preset", {"fast": "p3", "medium": "p5", "slow": "p7"}[p], "-tune", "hq"]
    if kind == "amf":
        return ["-quality", {"fast": "speed", "medium": "balanced", "slow": "quality"}[p]]
    if kind == "qsv":
        return ["-preset", {"fast": "veryfast", "medium": "medium", "slow": "slow"}[p]]
    return ["-preset", p]                      # libx264 / libx265


def _rate_flags(plan, info):
    """码控参数：质量模式用 CRF/CQ，目标大小模式用 ABR + VBV。"""
    if plan["mode"] == "target":
        b = int(plan["bps"])
        return ["-b:v", str(b), "-maxrate", str(int(b * 1.5)), "-bufsize", str(int(b * 2.0))]
    crf = plan["crf"]
    kind = plan["encoder_kind"]
    if kind == "nvenc":
        return ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    if kind == "qsv":
        return ["-global_quality", str(crf)]
    if kind == "amf":
        return ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf)]
    return ["-crf", str(crf)]                  # libx264 / libx265


def _pix_flags(plan, info):
    """位深 / profile / 色彩标记 / 容器 tag。hvc1 只给 HEVC，给 H.264 会直接被 muxer 拒绝。"""
    hevc = plan["codec_key"] == "h265"
    tag = ["-tag:v", "hvc1"] if hevc else []
    if plan["keep_hdr"]:
        trc = (info.get("transfer") or "").strip()
        if trc not in ("arib-std-b67", "smpte2084"):
            trc = "smpte2084"                  # DV / 未标注的 HDR 按 PQ 处理
        pix = "p010le" if plan["encoder_kind"] in ("nvenc", "qsv", "amf") else "yuv420p10le"
        return (["-pix_fmt", pix, "-profile:v", "main10",
                 "-color_primaries", "bt2020", "-color_trc", trc,
                 "-colorspace", "bt2020nc", "-color_range", "tv"] + tag)
    if plan["sdr"]:
        pix = "yuv420p10le" if (plan["ten_bit"] and plan["encoder_kind"] == "cpu") else (
            "p010le" if plan["ten_bit"] else "yuv420p")
        return (["-pix_fmt", pix] +
                (["-profile:v", "main10"] if plan["ten_bit"] else []) +
                ["-color_primaries", "bt709", "-color_trc", "bt709",
                 "-colorspace", "bt709"] + tag)
    if plan["ten_bit"]:
        pix = "yuv420p10le" if plan["encoder_kind"] == "cpu" else "p010le"
        return ["-pix_fmt", pix, "-profile:v", "main10"] + tag
    return ["-pix_fmt", "yuv420p"]


def _has_filter(name):
    global _FILTER_TEXT
    if _FILTER_TEXT is None:
        rc, out, _ = tool.run([tool.FFMPEG, "-hide_banner", "-filters"])
        _FILTER_TEXT = out if rc == 0 else ""
    return bool(re.search(r"(?m)^\s*\S+\s+%s\b" % re.escape(name), _FILTER_TEXT))


def _filter_chain(plan, info):
    flt = []
    if plan["sdr"]:
        trc = (info.get("transfer") or "").strip() or "arib-std-b67"
        if _has_filter("zscale") and _has_filter("tonemap"):
            flt += ["zscale=t=linear:npl=100:tin=%s" % trc, "format=gbrpf32le",
                    "zscale=p=bt709", "tonemap=hable:desat=0",
                    "zscale=t=bt709:m=bt709:r=tv", "format=yuv420p"]
        elif _has_filter("colorspace"):
            flt += ["colorspace=space=bt709:primaries=bt709:trc=bt709:range=tv:"
                    "ispace=bt2020nc:iprimaries=bt2020:itrc=%s:fast=1" % trc]
        else:
            plan["sdr_note"] = "该 ffmpeg 没有 zscale/colorspace，HDR→SDR 只做了普通重编码"
    if plan["scale"]:
        flt.append("scale=%d:%d:flags=lanczos" % (plan["scale"][0], plan["scale"][1]))
    return flt


def _audio_flags(plan, info, log=None):
    if not info.get("has_audio") or plan["audio_mode"] == "none":
        return ["-an"], ["-map", "0:v:0"]
    maps = ["-map", "0:v:0", "-map", "0:a:0"]
    if plan["audio_mode"] == "copy":
        ac = (info.get("audio_codec") or "").lower()
        if ac in MP4_SAFE_AUDIO:
            return ["-c:a", "copy"], maps
        if log:
            log("音频 %s 不适合直接放进 MP4，改为 AAC %s" % (ac or "未知", AUDIO_BITRATE))
    return ["-c:a", "aac", "-b:a", AUDIO_BITRATE], maps


def build_commands(src, dst, plan, info, log=None):
    """构造 ffmpeg 命令列表（目标大小 + CPU 时是两遍）。plan 会被补上 passlog/extra 等。"""
    a_flags, map_flags = _audio_flags(plan, info, log)
    flt = _filter_chain(plan, info)
    vf = ["-vf", ",".join(flt)] if flt else []
    base_out = (["-c:v", plan["enc_lib"]] + _preset_flags(plan["encoder_kind"], plan["preset"]) +
                _pix_flags(plan, info) + _rate_flags(plan, info))
    extra = shlex.split(plan.get("extra") or "")
    container = ["-movflags", "+faststart"] if dst.lower().endswith((".mp4", ".mov", ".m4v")) else []
    head = [tool.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats"]

    if plan["two_pass"]:
        passlog = os.path.join(os.path.dirname(dst),
                               "." + os.path.basename(dst) + ".2pass")
        plan["passlog"] = passlog
        pass_flags = lambda n: ["-pass", str(n), "-passlogfile", passlog]  # noqa: E731
        c1 = (head + ["-i", src, "-map", "0:v:0"] + vf +
              ["-c:v", plan["enc_lib"]] + _preset_flags(plan["encoder_kind"], plan["preset"]) +
              _rate_flags(plan, info) + pass_flags(1) + ["-an", "-f", "null", NULL_DEV])
        c2 = (head + ["-i", src] + map_flags + vf + base_out +
              pass_flags(2) + a_flags + extra + container + [dst])
        return [c1, c2]
    cmd = head + ["-i", src] + map_flags + vf + base_out + a_flags + extra + container + [dst]
    return [cmd]


def build_preview_cmd(src, tmp_out, plan, info):
    """精确预估用：单遍、只编码前 PREVIEW_SECS 秒。两遍方案返回 (None, 0)。"""
    cmds = build_commands(src, tmp_out, plan, info)
    if len(cmds) != 1:
        return None, 0.0
    cmd = list(cmds[0])
    try:
        i = cmd.index("-i")
    except ValueError:
        return None, 0.0
    preview = PREVIEW_SECS
    if float(info.get("duration") or 0) > 0:
        preview = min(PREVIEW_SECS, float(info["duration"]))
    return cmd[:i + 2] + ["-t", "%.3f" % preview] + cmd[i + 2:], preview


# ---------------------------------------------------------------- 视频信息
def _video_info(path):
    """用项目的 tool.probe()，再派生压缩需要的字段（码率、显示尺寸、HDR 描述）。"""
    info = tool.probe(path)
    if not info:
        raise RuntimeError("读不出视频信息（文件损坏或不是视频）：%s" % os.path.basename(path))
    if not info.get("width") or not info.get("height"):
        raise RuntimeError("该文件没有视频流：%s" % os.path.basename(path))
    d = dict(info)
    dur = float(d.get("duration") or 0)
    size = float(d.get("size") or 0)
    d["video_bitrate"] = (size * 8.0 / dur) if dur > 0 else 0.0
    if not d["video_bitrate"] and dur > 0:      # 兜底，避免估算除零
        d["video_bitrate"] = 1.0
    dw, dh = _display_dims(d)
    d["disp_w"], d["disp_h"] = dw, dh
    return d


def _hdr_label(info):
    if info.get("dovi") is not None:
        return "Dolby Vision (profile %s)" % info.get("dovi")
    trc = (info.get("transfer") or "").strip()
    if trc == "arib-std-b67":
        return "HLG (bt2020)"
    if trc == "smpte2084":
        return "HDR10/PQ (bt2020)"
    if info.get("hdr"):
        return "HDR (primaries=%s trc=%s)" % (info.get("primaries") or "?", trc or "?")
    return ""


def _cmd_line(cmd):
    return " ".join(('"%s"' % a if " " in a else a) for a in cmd)


# ---------------------------------------------------------------- 执行
def _drain(stream, sink):
    try:
        for line in stream:
            sink.append(line.rstrip())
    except Exception:                       # noqa: BLE001
        pass


def _run_cmd(job, cmd, duration, base, span, step):
    """跑一条 ffmpeg，用 out_time_us= 换算进度；取消时先杀掉进程再抛出。"""
    job.log("  $ " + _cmd_line(cmd))
    proc = tool.popen(cmd)
    err = deque(maxlen=40)
    th = threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)
    th.start()
    try:
        for line in proc.stdout:
            job.check()
            line = line.strip()
            if line.startswith("out_time_us="):
                raw = line.split("=", 1)[1]
                try:
                    secs = int(raw) / 1000000.0
                except ValueError:
                    continue
            elif line.startswith("out_time="):
                secs = parse_out_time(line.split("=", 1)[1])
            elif line == "progress=end":
                break
            else:
                continue
            frac = min(secs / duration, 1.0) if duration > 0 else 0.0
            job.set_progress(base + span * max(0.0, frac), step=step)
        rc = proc.wait()
    finally:
        if proc.poll() is None:             # 取消/异常：别留孤儿 ffmpeg
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:               # noqa: BLE001
                pass
    if rc != 0:
        tail = "\n".join(list(err)[-6:]).strip()
        raise RuntimeError("ffmpeg 编码失败（退出码 %s）。%s" % (rc, tail or "详细错误见上方日志"))
    return list(err)


def _cleanup_2pass(passlog):
    if not passlog:
        return
    d, stem = os.path.dirname(passlog), os.path.basename(passlog)
    try:
        for n in os.listdir(d or "."):
            if n.startswith(stem):
                try:
                    os.remove(os.path.join(d or ".", n))
                except OSError:
                    pass
    except OSError:
        pass


# ================================================================ 阶段
@register
class CompressStage(Stage):
    key = "compress"
    name = "压缩"
    icon = "🗜"
    order = 50
    desc = "重编码到目标大小或目标画质；自动探测 NVENC/AMF/QSV，支持 HDR 保留与 HLG→SDR"
    accepts = "video"
    produces = "video"

    # ------------------------------------------------------------ 表单
    def schema(self):
        return [
            sel("mode", "模式", [("target", "按目标大小 (MB)"), ("crf", "按质量 (CRF/CQ)")],
                "target", help="目标大小=反算码率（CPU 编码走两遍，命中更准）；"
                               "按质量=软编用 CRF、NVENC 用 CQ"),
            num("target_mb", "目标大小 (MB)", 100, min=1, max=100000, step=1,
                help="仅「按目标大小」模式生效"),
            rng("crf", "CRF / CQ（越小越清晰）", 23, 0, 51, 1,
                help="仅「按质量」模式生效；HEVC 通常比 H.264 大 2~3 才等价"),
            sel("height", "目标高度", [("orig", "原始分辨率"), ("1080", "1080p"),
                                       ("720", "720p"), ("480", "480p")], "orig",
                help="等比缩放（不放大、不裁切）；调小比拉高 CRF 更保清晰度"),
            sel("codec", "编码格式", [("h264", "H.264（兼容性最好）"),
                                      ("hevc", "H.265 / HEVC（同画质更小）"),
                                      ("same", "保持原编码")], "h264"),
            sel("encoder", "编码器", [("auto", "自动（探测到硬件就用硬件）"),
                                      ("nvenc", "强制 NVIDIA NVENC"),
                                      ("x264", "CPU 软件编码（最稳）")], "auto"),
            sel("preset", "预设", [("fast", "快（体积略大）"), ("medium", "中（推荐）"),
                                   ("slow", "慢（同画质更小）")], "medium"),
            sel("hdr", "HDR 处理", [("auto", "自动（HDR 源保留 10bit HDR）"),
                                    ("keep", "保留 HDR（10bit HEVC）"),
                                    ("sdr", "转 SDR（BT.709）")], "auto",
                help="源是 HLG/PQ/DV 时有效；保留 HDR 必须用 HEVC 10bit"),
            sel("audio", "音频", [("copy", "直接复制（源是 AAC 时）"),
                                  ("aac", "重编码 AAC 128k"),
                                  ("none", "去掉音轨")], "copy"),
            chk("two_pass", "目标大小模式用两遍编码（仅 CPU 编码）", True,
                help="第一遍只分析，命中目标体积更准；结束会清理 passlog 临时文件"),
            chk("preview_estimate", "先做精确预估（预编码前 12 秒，仅写日志）", False),
            path("outdir", "输出目录", "", kind="dir", help="留空 = 与源文件同目录"),
            txt("extra", "额外 ffmpeg 参数（高级，可留空）", "",
                help='追加在输出参数里，例如 -g 48 或 -x265-params log-level=error'),
        ]

    # ------------------------------------------------------------ 环境检查
    def available(self):
        ff, fp = tool.FFMPEG, tool.FFPROBE
        if not ff or not (os.path.isfile(ff) or shutil.which(ff)):
            return {"ok": False, "detail": "找不到 ffmpeg：请安装并加入 PATH，或放到 C:\\ffmpeg\\bin"}
        if not fp or not (os.path.isfile(fp) or shutil.which(fp)):
            return {"ok": False, "detail": "找不到 ffprobe（通常和 ffmpeg 同目录）"}
        enc = detect_encoders()
        if not enc["cpu"] and not enc["h264"] and not enc["h265"]:
            return {"ok": False, "detail": "该 ffmpeg 不带任何 H.264/H.265 编码器，无法压缩"}
        hw = []
        for k, lbl in (("h264", "H.264"), ("h265", "HEVC")):
            for n in enc[k]:
                hw.append("%s(%s)" % (n, lbl))
        detail = "ffmpeg: %s | CPU: %s | 硬件: %s" % (
            ff, "/".join(enc["cpu"]) or "无", ", ".join(hw) or "无（将用 CPU）")
        if not _has_filter("zscale"):
            detail += " | 无 zscale：HDR→SDR 只能做近似转换"
        return {"ok": True, "detail": detail}

    def meta(self):
        enc = detect_encoders()
        return {"ffmpeg": tool.FFMPEG, "encoders": enc,
                "nvenc_10bit": bool(_NVENC_10BIT_OK) if _NVENC_10BIT_OK is not None else None}

    # ------------------------------------------------------------ 方案
    def _plan(self, job, info, opts):
        mode = opts.get("mode") or "target"
        if mode not in ("target", "crf"):
            mode = "target"
        try:
            crf = int(float(opts.get("crf")))
        except (TypeError, ValueError):
            crf = 23
        crf = max(0, min(51, crf))
        try:
            target_mb = float(opts.get("target_mb"))
        except (TypeError, ValueError):
            raise RuntimeError("目标大小必须是数字（MB）")
        if target_mb <= 0:
            raise RuntimeError("目标大小必须大于 0 MB")

        codec_key = resolve_codec_key(opts.get("codec"), info.get("codec"))
        src_hdr = bool(info.get("hdr"))
        hdr_opt = opts.get("hdr") or "auto"
        keep_hdr = hdr_opt == "keep" or (hdr_opt == "auto" and src_hdr)
        if keep_hdr and codec_key != "h265":     # codec_key 内部统一用 h264 / h265
            if hdr_opt == "keep":
                raise RuntimeError("「保留 HDR」需要 HEVC 10bit 编码：请把编码格式改成 "
                                   "H.265/HEVC，或把 HDR 处理改成「转 SDR」")
            codec_key = "h265"
            job.log("源是 %s：自动改用 HEVC 10bit 以保留 HDR" % (_hdr_label(info) or "HDR"))
        sdr = src_hdr and not keep_hdr
        if sdr:
            job.log("HDR→SDR：使用 zscale+tonemap（Hable）转到 BT.709；HDR 高光会被压到 SDR 范围内")

        if keep_hdr and info.get("dovi") is not None:
            job.log("注意：杜比视界动态元数据(RPU)无法在重编码中保留，"
                    "输出为 HDR10 基础层（PQ/10bit）")

        target_h = opts.get("height") or "orig"
        th = None if str(target_h) in ("orig", "", "None") else int(float(target_h))
        scale = compute_scale(info.get("disp_w"), info.get("disp_h"), th)

        available_hw = detect_encoders()
        enc_lib, is_hw, kind = resolve_encoder(codec_key, opts.get("encoder"),
                                               available_hw, log=job.log)
        ten_bit = keep_hdr or (codec_key == "h265" and not sdr and
                               int(info.get("depth") or 8) > 8)
        if ten_bit and kind == "nvenc":
            global _NVENC_10BIT_OK
            if _NVENC_10BIT_OK is None:
                job.log("检查 NVENC 的 HEVC 10bit 能力（Pascal 等老卡可能不支持）…")
                _NVENC_10BIT_OK = _nvenc_10bit_ok()
            if not _NVENC_10BIT_OK:
                job.log("该显卡的 NVENC 不支持 HEVC 10bit（Pascal 常见），回退 libx265 软编")
                enc_lib, is_hw, kind = "libx265", False, "cpu"
        if kind != "cpu" and ten_bit and codec_key == "h264":
            ten_bit = False

        two_pass = bool(opts.get("two_pass")) and mode == "target" and kind == "cpu"
        plan = {"mode": mode, "codec_key": codec_key, "crf": crf, "target_mb": target_mb,
                "scale": scale, "enc_lib": enc_lib, "is_hw": is_hw, "encoder_kind": kind,
                "preset": opts.get("preset") or "medium", "ten_bit": ten_bit,
                "keep_hdr": keep_hdr, "sdr": sdr, "audio_mode": opts.get("audio") or "copy",
                "two_pass": two_pass, "bps": None, "passlog": None,
                "extra": opts.get("extra") or ""}
        if mode == "target":
            plan["bps"] = int(compute_target_bps(target_mb, info))
        return plan

    # ------------------------------------------------------------ 执行
    def run(self, job, inputs, opts):
        inputs = [p for p in (inputs or []) if p]
        if not inputs:
            raise RuntimeError("没有输入文件")
        src = inputs[0]
        if not os.path.isfile(src):
            raise RuntimeError("输入文件不存在：%s" % src)
        job.check()
        job.set_progress(0.0, step="读取视频信息")
        info = _video_info(src)

        outdir = opts.get("outdir") or os.path.dirname(os.path.abspath(src))
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = tool.unique_out(outdir, tool.safe_name(stem + "_compressed"))

        plan = self._plan(job, info, opts)
        cmds = build_commands(src, dst, plan, info, log=job.log)

        job.log("源：%dx%d%s %.2f fps，%s，时长 %s，码率 %.2f Mbps%s%s" % (
            info["width"], info["height"],
            "（显示 %dx%d，旋转 %d°）" % (info["disp_w"], info["disp_h"], info["rotation"])
            if int(info.get("rotation") or 0) else "",
            float(info.get("fps") or 0), tool.human_size(info.get("size")),
            tool.human_dur(info.get("duration")), (info.get("video_bitrate") or 0) / 1e6,
            "，含音轨(%s)" % (info.get("audio_codec") or "?") if info.get("has_audio") else "，无音轨",
            "，源 HDR：" + _hdr_label(info) if info.get("hdr") else ""))
        job.log("方案：%s / %s%s / %s / %s / %s" % (
            "HEVC" if plan["codec_key"] == "h265" else "H.264", plan["enc_lib"],
            "（硬件）" if plan["is_hw"] else "（CPU）",
            ("%.0fx%.0f" % tuple(plan["scale"])) if plan["scale"] else "原始分辨率",
            "目标码率 %.0f kbps" % (plan["bps"] / 1000.0) if plan["mode"] == "target"
            else "CRF/CQ %d" % plan["crf"],
            "保留 HDR 10bit" if plan["keep_hdr"] else ("转 SDR" if plan["sdr"] else
                                                       ("10bit" if plan["ten_bit"] else "SDR/8bit"))))
        if plan["two_pass"]:
            job.log("目标大小 + CPU 编码：使用两遍编码（第一遍只做分析）")
        if plan.get("sdr_note"):
            job.log("警告：" + plan["sdr_note"])
        est = estimate_size(info, plan)
        if est:
            job.log("预计输出 ≈ %s（源 %s）" % (tool.human_size(est), tool.human_size(info.get("size"))))

        if opts.get("preview_estimate"):
            self._preview_estimate(job, src, plan, info)

        spans = ([(0.0, 0.45, "第一遍分析"), (0.45, 0.55, "编码")] if len(cmds) == 2
                 else [(0.0, 1.0, "编码")])
        ok = False
        try:
            for cmd, (base, span, step) in zip(cmds, spans):
                job.check()
                _run_cmd(job, cmd, float(info.get("duration") or 0), base, span, step)
            ok = True
        except jobmod.Cancelled:
            job.log("已取消，清理未完成的文件…")
            raise
        finally:
            _cleanup_2pass(plan.get("passlog"))
            if not ok and os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass

        if not os.path.exists(dst):
            raise RuntimeError("ffmpeg 没有生成输出文件：%s" % dst)
        out_size = os.path.getsize(dst)
        job.set_progress(1.0, step="完成")
        job.log("输出：%s" % dst)
        job.log("最终大小：%s（源 %s，%s），%s" % (
            tool.human_size(out_size), tool.human_size(info.get("size")),
            ("减少 %.0f%%" % ((1 - out_size / float(info["size"])) * 100)) if info.get("size")
            else "源大小未知",
            ("与预估基本一致" if est and abs(out_size - est) / max(est, 1) < 0.25
             else ("比预估偏大/偏小，实际 %s vs 预估 %s" % (tool.human_size(out_size),
                                                          tool.human_size(est))) if est else "")))
        return dst

    def _preview_estimate(self, job, src, plan, info):
        """前 12 秒真编一遍外推体积（build_preview_cmd），失败不影响正式压缩。"""
        job.check()
        tmp = os.path.join(tempfile.gettempdir(),
                           "_easyvideo_preview_%s.mp4" % os.getpid())
        try:
            cmd, preview = build_preview_cmd(src, tmp, plan, info)
            if cmd is None:
                job.log("两遍编码方案不做精确预估")
                return
            job.log("精确预估：预编码前 %.1f 秒…" % preview)
            rc, _, err = tool.run(cmd, timeout=600)
            if rc != 0 or not os.path.exists(tmp):
                job.log("精确预估失败（不影响正式压缩）：%s" % (err or "").strip()[-200:])
                return
            out_size = os.path.getsize(tmp)
            dur = float(info.get("duration") or 0)
            est = out_size * (dur / preview) if preview > 0 and dur > 0 else out_size
            job.log("精确预估结果 ≈ %s" % tool.human_size(est))
        except jobmod.Cancelled:
            raise
        except Exception as e:                  # noqa: BLE001
            job.log("精确预估异常（忽略）：%s" % e)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def _nvenc_10bit_ok():
    """一次 128x128 的 HEVC 10bit 试编码：Pascal 等不支持时会失败。"""
    rc, _, _ = tool.run([tool.FFMPEG, "-hide_banner", "-loglevel", "error",
                         "-f", "lavfi", "-i", "color=black:s=128x128:d=0.1",
                         "-c:v", "hevc_nvenc", "-pix_fmt", "p010le",
                         "-f", "null", NULL_DEV], timeout=120)
    return rc == 0
