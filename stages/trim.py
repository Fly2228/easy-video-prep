# -*- coding: utf-8 -*-
"""剪切 & 合并阶段 —— 无损流拷贝切割 + 精确重编码切割 + 顺序拼接。

移植自 video-trimmer/server.py 的 run_trim()/probe()/safe_name()/unique_out()/ts_name()：
  - 切割默认走 ffmpeg 流拷贝（-c copy），不重新编码，零画质损失；
  - 需要帧级精准时勾选"精确切割"，退化为 -ss 放在 -i 之后 + 重编码；
  - 合并优先 concat demuxer + -c copy（无损），参数不一致时回退 filter_complex 重编码。

不丢质量的要点：
  * 默认 -c copy，视频/音频/旋转元数据/HDR 色彩标签原样搬过去；
  * 只有用户显式勾选精确切割、或合并的输入编码参数不一致时才重编码；
  * 重编码用 CRF 恒定质量（默认 18）；HDR 源选 HEVC 编码器时保 10bit + 色彩标签，
    选只能出 8bit 的 H.264 时自动 HDR→SDR(bt709) 色调映射，绝不贴"HLG 数据 + bt2020 标签"的半残标签；
  * 合并 fast path 会逐项比对 codec/分辨率/帧率/像素格式/音轨，不一致绝不硬 copy。

已知限制：
  * 流拷贝的 -ss 在 -i 之前 = 关键帧对齐；-c copy 不丢弃"关键帧→起点"之间的内容，
    所以每段会提前到上一个关键帧开始（多出的长度取决于 GOP，可能达数秒），结束点准确；
  * 重编码无法保留 Dolby Vision 动态元数据；HDR 只能靠色彩标签近似保留；
  * 合并的重编码回退会把音频统一成 48kHz 立体声（5.1 会被下混）；
  * 切割模式一次只处理一个源文件。
"""
import os
import re
import shutil
import tempfile
import threading

from core.stage import Stage, register, sel, num, rng, chk, txt, path, show_if
from core import tool


# ------------------------------------------------------------------ 小工具

def _num(s, dflt=0.0):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return dflt


def _ts(t):
    """秒 -> 文件名安全的时间戳 00-00-03.50（冒号在 Windows 上是非法字符）。"""
    t = max(0.0, _num(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    return "%02d-%02d-%05.2f" % (h, m, t % 60)


def _show(cmd):
    parts = []
    for c in cmd:
        c = str(c)
        parts.append('"%s"' % c if (" " in c or "\t" in c) else c)
    s = " ".join(parts)
    return s if len(s) <= 900 else s[:900] + " ...[已截断]"


_ENC_CACHE = None


def _encoders():
    """ffmpeg -encoders 的输出（缓存，只跑一次）。"""
    global _ENC_CACHE
    if _ENC_CACHE is None:
        try:
            rc, out, _ = tool.run([tool.FFMPEG, "-hide_banner", "-encoders"], timeout=25)
            _ENC_CACHE = out if rc == 0 else ""
        except Exception:                                    # noqa: BLE001
            _ENC_CACHE = ""
    return _ENC_CACHE


def _has_enc(name):
    return name in _encoders()


# ------------------------------------------------------------------ 时间段解析

_SPLIT = re.compile(r"\s*(?:-|\u2014|\u2013|~|\uff5e|\u81f3|\u5230|\bto\b)\s*", re.I)


def _parse_time(s):
    """支持 123.5 / 1:23.5 / 00:01:23.50 三种写法。"""
    t = str(s).strip().replace("\uff1a", ":").replace(",", ".").replace("\uff0c", ".")
    if not t:
        raise ValueError("空时间")
    if ":" in t:
        ps = t.split(":")
        if len(ps) == 2:
            h, m, sec = 0.0, ps[0], ps[1]
        elif len(ps) == 3:
            h, m, sec = ps[0], ps[1], ps[2]
        else:
            raise ValueError("时间格式不对")
        return float(h) * 3600.0 + float(m) * 60.0 + float(sec)
    return float(t)


def _parse_segments(raw):
    """每行一段：3-12 / 00:01:23.5-00:02:10 / 1:23.5~2:10 / 3,12。返回 [(a,b), ...]。"""
    segs = []
    for i, ln in enumerate(str(raw or "").splitlines(), 1):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = _SPLIT.split(ln, maxsplit=1)
        if len(m) < 2:
            m = [x for x in re.split(r"[\s,]+", ln) if x]
        if len(m) < 2:
            raise RuntimeError("第 %d 行时间段看不懂：%s（应形如 3-12 或 00:01:23.5-00:02:10）" % (i, ln))
        try:
            a, b = _parse_time(m[0]), _parse_time(m[1])
        except ValueError:
            raise RuntimeError("第 %d 行时间格式不对：%s" % (i, ln))
        if b <= a:
            raise RuntimeError("第 %d 行结束时间必须大于开始时间：%s" % (i, ln))
        segs.append((a, b))
    if not segs:
        raise RuntimeError("没有解析出任何时间段：请每行写一段，例如 3-12")
    return segs


def _merge_intervals(segs):
    out = []
    for a, b in sorted(segs):
        if out and a <= out[-1][1] + 1e-6:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _complement(segs, dur, gap=0.05):
    """在 [0, dur] 里扣掉 segs，得到"删除这些段之后剩下什么"。"""
    keep, cur = [], 0.0
    for a, b in _merge_intervals(segs):
        if a - cur > gap:
            keep.append((cur, a))
        cur = max(cur, b)
    if dur - cur > gap:
        keep.append((cur, dur))
    return keep


# ------------------------------------------------------------------ 容器 / 几何

_MP4_V = ("h264", "hevc", "mpeg4", "av1", "mpeg2video", "mjpeg")
_MP4_A = ("aac", "mp3", "ac3", "eac3", "mp2", "alac", "pcm_s16le", "")
_WEBM_V = ("vp8", "vp9")
_WEBM_A = ("opus", "vorbis", "")


def _mux_ext(info):
    """流拷贝时挑一个能装下这些编码的容器。"""
    v = (info or {}).get("codec") or ""
    a = ((info or {}).get("audio_codec") or "") if (info or {}).get("has_audio") else ""
    if v in _MP4_V and a in _MP4_A:
        return ".mp4"
    if v in _WEBM_V and a in _WEBM_A:
        return ".webm"
    return ".mkv"


def _disp_dims(info):
    """考虑旋转元数据后的显示尺寸（±90° 时宽高互换）。"""
    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    if abs(int(info.get("rotation") or 0)) % 180 == 90:
        w, h = h, w
    return w, h


def _even(v):
    v = int(v)
    return v if v % 2 == 0 else v + 1


def _compatible(infos):
    """无损 concat 的前提：编码参数逐项一致。"""
    names = ("视频编码", "宽", "高", "帧率", "像素格式", "有无音轨", "音频编码", "旋转")

    def key(i):
        return (i.get("codec"), int(i.get("width") or 0), int(i.get("height") or 0),
                round(_num(i.get("fps")), 3), i.get("pix_fmt"),
                bool(i.get("has_audio")),
                (i.get("audio_codec") or "") if i.get("has_audio") else "",
                int(i.get("rotation") or 0))

    first = key(infos[0])
    for k, inf in enumerate(infos[1:], 2):
        cur = key(inf)
        if cur != first:
            diff = [n for n, x, y in zip(names, first, cur) if x != y]
            return False, "第 %d 个与第 1 个不同：%s" % (k, "/".join(diff))
    return True, "%s %sx%s %sfps %s" % (infos[0].get("codec"), infos[0].get("width"),
                                        infos[0].get("height"), infos[0].get("fps"),
                                        infos[0].get("pix_fmt"))


def _concat_quote(p):
    """concat demuxer 的单引号转义；Windows 反斜杠统一写成正斜杠。"""
    p = os.path.abspath(p).replace("\\", "/")
    return p.replace("'", "'\\''")


def _write_concat_list(paths):
    tmp = tempfile.mkdtemp(prefix="ev_concat_")
    lp = os.path.join(tmp, "list.txt")
    with open(lp, "w", encoding="utf-8", newline="\n") as f:
        for p in paths:
            f.write("file '%s'\n" % _concat_quote(p))
    return lp, tmp


# ------------------------------------------------------------------ 阶段

@register
class TrimStage(Stage):
    key = "trim"
    name = "剪切 & 合并"
    icon = "\u2702"
    order = 20
    desc = ("无损流拷贝切割（关键帧对齐、零损失）或帧级精确重编码切割；"
            "也可把多个视频按顺序拼接成一个文件（编码一致时无损、不一致自动重编码）。")
    accepts = "video"
    produces = "video"
    uses_range = True          # 预览里显示入/出点手柄

    # ---- 表单 ------------------------------------------------------
    def schema(self):
        return [
            sel("mode", "模式", [
                ("keep", "切割 · 保留所选段"),
                ("cut", "切割 · 删掉所选段"),
                ("merge", "合并 · 按输入顺序拼接"),
            ], default="keep",
                help="切割模式每个时间段导出一个文件；合并模式使用当前输入的多个视频。"),
            txt("segments", "时间段（切割模式用，每行一段）", "", multiline=True,
                help="支持 秒 或 时:分:秒 混写，例如：\n3-12\n00:01:23.5-00:02:10\n1:05~1:20"),
            chk("faststart", "MP4 输出加 +faststart", True,
                help="把索引挪到文件头，网络播放/拖动更顺；对 mkv/webm 无影响。"),
            txt("outname", "输出文件名（留空自动命名）", "",
                help="合并模式推荐填；切割模式会自动追加时间段后缀。"),
            path("outdir", "输出目录（留空 = 与源文件同目录）", "", kind="dir"),
            chk("allow_reencode", "允许重编码（高级 · 默认关闭）", False,
                help="剪辑默认是**纯无损流拷贝**（-c copy）：画质零损失、秒出。"
                     "只有两种情况才需要打开——"
                     "① 必须帧级精确切割（流拷贝的起点会往前吸附到关键帧）；"
                     "② 合并的多个视频编码参数不一致（无法直接拼接）。"),
            *show_if([
                chk("precise", "精确切割（帧级精准，会重编码）", False,
                    help="把 -ss 放在 -i 之后重编码，起止都精确到帧；代价是画质轻微损失 + 耗时。"),
                sel("encoder", "重编码编码器", [
                    ("auto", "自动（有 NVENC 优先用 NVENC）"),
                    ("libx264", "libx264（CPU 软编，最稳）"),
                    ("h264_nvenc", "h264_nvenc（N 卡硬编，快；HDR 源会转 SDR）"),
                    ("hevc_nvenc", "hevc_nvenc（N 卡硬编 H.265，可保留 10bit HDR）"),
                    ("libx265", "libx265（CPU 软编 H.265，可保留 10bit HDR）"),
                ], default="auto", help="仅在精确切割或合并需要重编码时生效。"
                    "H.264 只能出 8bit：源是 HDR 时会自动做 HDR→SDR(bt709) 色调映射；"
                    "想保留 HDR 请选 hevc_nvenc / libx265。"),
                num("crf", "质量 CRF（越小越清晰、体积越大）", 18, 0, 35, 1,
                    help="软编 18 视觉无损、20~23 常规；NVENC 下作为 -cq 使用。"),
                sel("preset", "编码速度", [
                    ("veryfast", "veryfast（快）"),
                    ("fast", "fast"),
                    ("medium", "medium（均衡）"),
                    ("slow", "slow（小体积）"),
                ], default="veryfast"),
                sel("merge_fit", "合并重编码时的画幅统一方式", [
                    ("fit", "统一到最大尺寸并加黑边"),
                    ("first", "统一到第一个视频的尺寸并加黑边"),
                ], default="fit", help="仅当输入编码参数不一致、需要重编码拼接时生效。"),
                num("merge_fps", "合并统一帧率（0 = 取最大帧率）", 0, 0, 240, 1),
            ], "allow_reencode", True),
        ]

    # ---- 预览里选好区间按 Enter 的快捷动作：直接无损剪出来 ----
    def quick(self, inputs, a, b):
        return {"mode": "keep", "segments": "%.2f-%.2f" % (a, b),
                "allow_reencode": False, "precise": False}

    def meta(self):
        enc = _encoders()
        return {"_ffmpeg": tool.FFMPEG,
                "_h264": [e for e in ("libx264", "h264_nvenc", "hevc_nvenc", "libx265") if e in enc]}

    # ---- 可用性 ----------------------------------------------------
    def available(self):
        exe = tool.FFMPEG
        if not exe or (not os.path.isfile(exe) and not shutil.which(exe)):
            return {"ok": False,
                    "detail": "未找到 ffmpeg：请安装 ffmpeg 并加入 PATH，"
                              "或放到 C:\\ffmpeg\\bin、D:\\ffmpeg\\bin，或任意已加入 PATH 的位置。"}
        pbe = tool.FFPROBE
        if not pbe or (not os.path.isfile(pbe) and not shutil.which(pbe)):
            return {"ok": False,
                    "detail": "找到 ffmpeg 但缺少 ffprobe（%s）：切割要按时长裁段、合并要比较编码参数，"
                              "必须有 ffprobe。" % (pbe or "ffprobe")}
        enc = _encoders()
        h264 = [e for e in ("libx264", "h264_nvenc") if e in enc]
        if not h264:
            return {"ok": False,
                    "detail": "ffmpeg（%s）里没有 libx264 / h264_nvenc 编码器："
                              "精确切割与合并重编码无法工作。" % exe}
        extra = [e for e in ("hevc_nvenc", "libx265") if e in enc]
        detail = "ffmpeg: %s；H.264 编码器: %s" % (exe, ", ".join(h264))
        if extra:
            detail += "；另可用: %s" % ", ".join(extra)
        return {"ok": True, "detail": detail}

    # ---- 执行 ------------------------------------------------------
    def run(self, job, inputs, opts):
        opts = dict(opts or {})
        # 默认纯无损：任何会导致重编码的开关一律压掉
        if not opts.get("allow_reencode"):
            if opts.get("precise"):
                job.log("「精确切割」需要重编码，但未打开「允许重编码」→ 已按无损流拷贝执行")
            opts["precise"] = False
        srcs = [p for p in (inputs or []) if p and os.path.isfile(p)]
        if not srcs:
            raise RuntimeError("没有可用的输入视频（文件不存在或未选择）")
        mode = str(opts.get("mode") or "keep").lower()
        outdir = str(opts.get("outdir") or "").strip().strip('"')
        if not outdir:
            outdir = os.path.dirname(os.path.abspath(srcs[0]))
        os.makedirs(outdir, exist_ok=True)

        job.log("ffmpeg: %s" % tool.FFMPEG)
        job.log("输出目录: %s" % outdir)
        job.check()

        if mode == "merge":
            outs = self._merge(job, srcs, opts, outdir)
        elif mode in ("keep", "cut"):
            if len(srcs) > 1:
                job.log("切割模式一次只处理一个视频，已使用第一个：%s" % os.path.basename(srcs[0]))
            outs = self._cut(job, srcs[0], opts, outdir, mode)
        else:
            raise RuntimeError("未知模式：%s（应为 keep / cut / merge）" % mode)

        if not outs:
            raise RuntimeError("没有生成任何输出文件")
        job.set_progress(1.0, step="完成")
        for p in outs:
            sz = os.path.getsize(p) if os.path.exists(p) else 0
            job.log("完成: %s  (%s)" % (p, tool.human_size(sz)))
        return outs if len(outs) > 1 else outs[0]

    # ============================================================ 切割
    def _cut(self, job, src, opts, outdir, mode):
        info = tool.probe(src)
        if not info:
            raise RuntimeError("ffprobe 读不出这个文件的信息（可能损坏 / 不是视频）：%s" % src)
        dur = _num(info.get("duration"))
        segs = _parse_segments(opts.get("segments"))
        precise = bool(opts.get("precise"))
        if dur <= 0:
            dur = max(b for _, b in segs)
            job.log("提示：容器没有时长信息，按时间段最大值 %.3f 秒处理" % dur)

        if mode == "cut":
            spans = _complement(segs, dur)
            if not spans:
                raise RuntimeError("按这些时间段删除后没有剩余内容；视频总长 %s，请检查时间段"
                                   % tool.human_dur(dur))
            job.log("删除 %d 段，保留剩余 %d 段" % (len(_merge_intervals(segs)), len(spans)))
        else:
            spans = []
            for a, b in segs:
                a2, b2 = max(0.0, min(a, dur)), max(0.0, min(b, dur))
                if b2 - a2 <= 0.02:
                    job.log("跳过超出视频长度（%s）的段：%s"
                            % (tool.human_dur(dur), _show([tool.human_dur(a), tool.human_dur(b)])))
                    continue
                if (a2, b2) != (a, b):
                    job.log("段 %s → %s 已被视频长度裁剪为 %s → %s"
                            % (tool.human_dur(a), tool.human_dur(b),
                               tool.human_dur(a2), tool.human_dur(b2)))
                spans.append((a2, b2))
            if not spans:
                raise RuntimeError("所有时间段都落在视频长度（%s）之外" % tool.human_dur(dur))

        total = sum(b - a for a, b in spans) or 1.0
        ext = ".mp4" if precise else _mux_ext(info)
        job.log("方式 = %s，共 %d 段，总时长 %s，容器 %s"
                % ("精确重编码" if precise else "无损流拷贝(-c copy)", len(spans),
                   tool.human_dur(total), ext))
        if not precise:
            job.log("流拷贝：-ss 放在 -i 之前 = 关键帧对齐。ffmpeg 在 -c copy 时不会丢弃"
                    "“关键帧→起点”之间的内容，所以每段可能比要求的起点更早开始（结束点准确）；"
                    "需要帧级精准请勾选“精确切割”。")
        if int(info.get("rotation") or 0) and not precise:
            job.log("源带旋转元数据 %s°：流拷贝会原样保留，播放器会按元数据摆正，这不是错误。"
                    % info.get("rotation"))
        if int(info.get("rotation") or 0) and precise:
            job.log("源带旋转元数据 %s°：重编码时 ffmpeg 会自动摆正画面并去掉旋转标签，输出朝向正确。"
                    % info.get("rotation"))
        if precise and info.get("hdr"):
            if self._need_tonemap(opts, info):
                job.log("源是 HDR 但选的编码器只能出 8bit：已启用 HDR→SDR(bt709) 色调映射，"
                        "输出为标准 SDR（Hable 曲线，高光会被压进 SDR 范围）。"
                        "要真正的 HDR 输出请把编码器换成 hevc_nvenc 或 libx265。")
            else:
                job.log("源是 HDR（transfer=%s, %sbit）：输出保留 10bit 与 %s/%s 色彩标签，"
                        "但 Dolby Vision 动态元数据无法保留。"
                        % (info.get("transfer") or "?", info.get("depth") or 8,
                           info.get("primaries") or "?", info.get("transfer") or "?"))

        base_name = tool.safe_name(os.path.splitext(os.path.basename(src))[0])
        outs, done = [], 0.0
        for i, (a, b) in enumerate(spans, 1):
            job.check()
            job.set_progress(done / total, step="第 %d/%d 段 %s → %s"
                             % (i, len(spans), tool.human_dur(a), tool.human_dur(b)))
            stem = ("%s_keep_%02d_%s~%s" % (base_name, i, _ts(a), _ts(b))) if mode == "cut" \
                else ("%s_%s~%s" % (base_name, _ts(a), _ts(b)))
            out = tool.unique_out(outdir, stem, ext)
            cmd = self._cut_cmd(src, out, a, b, opts, info, precise)
            job.log("$ " + _show(cmd))
            self._run_ffmpeg(job, cmd, dur=(b - a), base=done, total=total,
                             tag="第 %d/%d 段" % (i, len(spans)))
            if not os.path.isfile(out):
                raise RuntimeError("ffmpeg 没有生成输出文件（%s），请检查上方日志" % out)
            outs.append(out)
            done += (b - a)
        return outs

    def _cut_cmd(self, src, out, a, b, opts, info, precise):
        cmd = [tool.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-progress", "pipe:1", "-nostats"]
        if not precise:
            # 原 server.py 的写法：-ss / -to 都在 -i 前 → 关键帧对齐的快速定位
            cmd += ["-ss", "%.3f" % a, "-to", "%.3f" % b, "-i", src,
                    "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                    "-avoid_negative_ts", "make_zero"]
        else:
            # -ss 放在 -i 之后 → 解码后精确到帧
            cmd += ["-i", src, "-ss", "%.3f" % a, "-to", "%.3f" % b,
                    "-map", "0:v:0", "-map", "0:a:0?"]
            if self._need_tonemap(opts, info):
                cmd += ["-vf", self.HDR_TO_SDR]
            cmd += self._venc_args(opts, info)
            cmd += ["-c:a", "aac", "-b:a", "192k"]
            cmd += self._color_args(opts, info)
        if bool(opts.get("faststart")) and os.path.splitext(out)[1].lower() in (".mp4", ".mov", ".m4v"):
            cmd += ["-movflags", "+faststart"]
        cmd += [out]
        return cmd

    # ============================================================ 合并
    def _merge(self, job, srcs, opts, outdir):
        infos = []
        for p in srcs:
            job.check()
            info = tool.probe(p)
            if not info:
                raise RuntimeError("ffprobe 读不出这个文件的信息，无法合并：%s" % p)
            infos.append(info)
        total = sum(_num(i.get("duration")) for i in infos) or 1.0
        job.log("准备合并 %d 个视频，总时长约 %s" % (len(srcs), tool.human_dur(total)))
        job.check()

        name = str(opts.get("outname") or "").strip()
        stem = tool.safe_name(name) if name else tool.safe_name(
            os.path.splitext(os.path.basename(srcs[0]))[0] + "_merge")

        same, why = _compatible(infos)
        if same:
            job.log("编码参数一致（%s）→ concat demuxer + -c copy，无损拼接" % why)
            return [self._merge_copy(job, srcs, infos, opts, outdir, stem, total)]

        if not opts.get("allow_reencode"):
            raise RuntimeError(
                "这些视频的编码参数不一致（%s），无法无损拼接。\n\n"
                "两个办法：\n"
                "  ① 先用「压缩」阶段把它们统一成同一套参数（推荐，可控）；\n"
                "  ② 在参数里打开「允许重编码（高级）」让本阶段重新编码拼接"
                "（画面会统一缩放+补黑边，音频统一 48kHz 立体声）。" % why)
        job.log("编码参数不一致（%s）→ 回退重编码拼接：画面统一缩放+补黑边、音频统一 48kHz 立体声"
                % why)
        if any(i.get("hdr") for i in infos):
            job.log("提示：输入含 HDR，重编码拼接后色调映射不做处理，输出色彩可能与源不同。")
        return [self._merge_encode(job, srcs, infos, opts, outdir, stem, total)]

    def _merge_copy(self, job, srcs, infos, opts, outdir, stem, total):
        ext = _mux_ext(infos[0])
        out = tool.unique_out(outdir, stem, ext)
        listpath, tmpdir = _write_concat_list(srcs)
        try:
            with open(listpath, "r", encoding="utf-8") as f:
                job.log("concat list:\n" + f.read().strip())
            cmd = [tool.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                   "-progress", "pipe:1", "-nostats",
                   "-f", "concat", "-safe", "0", "-i", listpath,
                   "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                   "-avoid_negative_ts", "make_zero"]
            if bool(opts.get("faststart")) and ext in (".mp4", ".mov", ".m4v"):
                cmd += ["-movflags", "+faststart"]
            cmd += [out]
            job.log("$ " + _show(cmd))
            self._run_ffmpeg(job, cmd, dur=total, base=0.0, total=total, tag="合并(流拷贝)")
        finally:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:                                # noqa: BLE001
                pass
        return out

    def _merge_encode(self, job, srcs, infos, opts, outdir, stem, total):
        out = tool.unique_out(outdir, stem, ".mp4")
        tenbit = all(int(i.get("depth") or 8) > 8 for i in infos)
        pix = "yuv420p10le" if tenbit else "yuv420p"
        W, H, fps = self._geometry(infos, opts)
        for i in infos:
            if int(i.get("rotation") or 0):
                job.log("提示：%s 带旋转元数据 %s°，重编码会自动摆正并去掉旋转标签。"
                        % (os.path.basename(i["path"]), i.get("rotation")))
        job.log("统一到 %dx%d @ %.4gfps，像素格式 %s，音频 48kHz 立体声" % (W, H, fps, pix))

        parts, seq = [], []
        for n, info in enumerate(infos):
            parts.append("[%d:v]fps=%.6f,scale=%d:%d:force_original_aspect_ratio=decrease,"
                         "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=%s[v%d]"
                         % (n, fps, W, H, W, H, pix, n))
            if info.get("has_audio"):
                parts.append("[%d:a]aresample=48000:async=1:first_pts=0,"
                             "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                             "asetpts=PTS-STARTPTS[a%d]" % (n, n))
            else:
                parts.append("aevalsrc=0:d=%.3f:c=stereo:s=48000,asetpts=PTS-STARTPTS[a%d]"
                             % (max(0.05, _num(info.get("duration"))), n))
            seq.append("[v%d][a%d]" % (n, n))
        graph = ";".join(parts) + ";" + "".join(seq) + \
            "concat=n=%d:v=1:a=1[outv][outa]" % len(infos)

        cmd = [tool.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-progress", "pipe:1", "-nostats"]
        for p in srcs:
            cmd += ["-i", p]
        cmd += ["-filter_complex", graph, "-map", "[outv]", "-map", "[outa]"]
        cmd += self._venc_args(opts, {"depth": 10 if tenbit else 8, "hdr": any(i.get("hdr") for i in infos)})
        cmd += ["-c:a", "aac", "-b:a", "192k"]
        if bool(opts.get("faststart")):
            cmd += ["-movflags", "+faststart"]
        cmd += [out]

        if sum(len(str(c)) + 1 for c in cmd) > 28000:
            raise RuntimeError("合并重编码的输入太多（%d 个），命令行超出 Windows 长度上限。"
                               "请分批合并，或先把这些视频统一转码成同样的编码参数再合并。"
                               % len(srcs))
        job.log("$ " + _show(cmd))
        self._run_ffmpeg(job, cmd, dur=total, base=0.0, total=total, tag="合并(重编码)")
        return out

    def _geometry(self, infos, opts):
        dims = [_disp_dims(i) for i in infos]
        if str(opts.get("merge_fit") or "fit") == "first":
            W, H = _even(dims[0][0] or 1920), _even(dims[0][1] or 1080)
        else:
            W = _even(max(d[0] for d in dims) or 1920)
            H = _even(max(d[1] for d in dims) or 1080)
        fps = _num(opts.get("merge_fps"))
        if fps <= 0:
            fps = max([_num(i.get("fps")) for i in infos] + [0.0]) or 30.0
        return W, H, fps

    # ============================================================ 编码参数 / 跑进程
    # HDR(HLG/PQ) -> SDR(bt709) 转换链。只在"源是 HDR 但输出是 8bit"时用：
    # 不转换、直接把 HLG 信号当 SDR 输出，在支持 HDR 的播放器里会明显偏亮发灰。
    HDR_TO_SDR = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                  "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")

    def _out_tenbit(self, opts, info):
        """输出到底是不是 10bit —— 必须和 _venc_args 的选择保持一致。"""
        want = str(opts.get("encoder") or "auto")
        if want == "auto":
            want = "h264_nvenc" if _has_enc("h264_nvenc") else "libx264"
        if want in ("h264_nvenc", "libx264"):
            return False
        if want in ("hevc_nvenc", "libx265"):
            return int(info.get("depth") or 8) > 8 and _has_enc(want)
        return False

    def _need_tonemap(self, opts, info):
        return bool(info.get("hdr")) and not self._out_tenbit(opts, info)

    def _color_args(self, opts, info):
        """色彩标签。

        - 10bit HEVC 输出：保留源的 HDR 标签（真正的 HDR 输出）
        - 8bit 输出：必须显式标 bt709。否则"HLG 数据 + bt2020 标签"这种半残标签
          会让各播放器各显各的 —— 支持 HDR 的按 HLG 解释、不支持的按 SDR 解释，
          同一份文件两种观感。
        """
        if self._out_tenbit(opts, info) and info.get("hdr"):
            a = []
            if info.get("transfer"):
                a += ["-color_trc", str(info["transfer"])]
            if info.get("primaries"):
                a += ["-color_primaries", str(info["primaries"])]
            if str(info.get("primaries") or "") == "bt2020":
                a += ["-colorspace", "bt2020nc"]
            return a
        return ["-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709", "-color_range", "tv"]

    def _venc_args(self, opts, info):
        """返回视频编码参数列表。info 只需含 depth / hdr 两个键。

        各编码器的 10bit 能力（实测这台机器，踩过坑）：
          libx264     —— **不支持 10bit**，只能 yuv420p
          h264_nvenc  —— **不支持 10bit**（H.264 规范层面就没有 10bit 编码），只能 yuv420p
          hevc_nvenc  —— 支持，p010le
          libx265     —— 支持，yuv420p10le
        以前这里把三个分支都按 tenbit 上了 10bit 像素格式，结果 ffmpeg 直接报
        "Nothing was written into output file, because at least one of its streams
        received no packets" —— 因为编码器一个包都没编出来。
        """
        want = str(opts.get("encoder") or "auto")
        tenbit = int(info.get("depth") or 8) > 8
        crf = int(_num(opts.get("crf"), 18))
        preset = str(opts.get("preset") or "veryfast")
        if want == "auto":
            want = "h264_nvenc" if _has_enc("h264_nvenc") else "libx264"
        if want in ("h264_nvenc", "hevc_nvenc") and not _has_enc(want):
            want = "h264_nvenc" if _has_enc("h264_nvenc") else "libx264"
        if want == "libx265" and not _has_enc("libx265"):
            want = "libx264"
        # H.264 一律降到 8bit（源是 10bit HDR 也一样），否则编码器编不出东西
        if want in ("h264_nvenc", "libx264") and tenbit:
            tenbit = False

        if want == "h264_nvenc":
            nv = {"veryfast": "p1", "fast": "p3", "medium": "p4", "slow": "p6"}.get(preset, "p4")
            return ["-c:v", "h264_nvenc", "-preset", nv, "-rc", "vbr",
                    "-cq", str(crf), "-b:v", "0", "-pix_fmt", "yuv420p"]
        if want == "hevc_nvenc":
            nv = {"veryfast": "p1", "fast": "p3", "medium": "p4", "slow": "p6"}.get(preset, "p4")
            return ["-c:v", "hevc_nvenc", "-preset", nv, "-rc", "vbr",
                    "-cq", str(crf), "-b:v", "0",
                    "-pix_fmt", "p010le" if tenbit else "yuv420p", "-tag:v", "hvc1"]
        if want == "libx265":
            xp = {"veryfast": "ultrafast", "fast": "fast", "medium": "medium", "slow": "slow"}.get(preset, "fast")
            return ["-c:v", "libx265", "-preset", xp, "-crf", str(crf),
                    "-pix_fmt", "yuv420p10le" if tenbit else "yuv420p", "-tag:v", "hvc1"]
        xp = {"veryfast": "veryfast", "fast": "fast", "medium": "medium", "slow": "slow"}.get(preset, "veryfast")
        return ["-c:v", "libx264", "-preset", xp, "-crf", str(crf), "-pix_fmt", "yuv420p"]

    def _run_ffmpeg(self, job, cmd, dur, base, total, tag=""):
        """起进程、按 out_time_us 累加进度、被取消时杀掉子进程。"""
        dur = max(0.001, float(dur))
        total = max(0.001, float(total))
        base = max(0.0, float(base))
        try:
            proc = tool.popen(cmd)
        except FileNotFoundError:
            raise RuntimeError("找不到 ffmpeg 可执行文件：%s" % cmd[0])
        except OSError as e:
            raise RuntimeError("无法启动 ffmpeg：%s" % e)

        errs = []

        def _drain():
            try:
                for line in proc.stderr:
                    line = line.rstrip()
                    if line:
                        errs.append(line)
                        del errs[:-40]
            except Exception:                                # noqa: BLE001
                pass

        th = threading.Thread(target=_drain, daemon=True)
        th.start()
        done = base
        try:
            for line in proc.stdout:
                job.check()                                  # 取消 → Cancelled 向上抛
                line = line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    t = _num(line.split("=", 1)[1]) / 1e6
                    done = max(done, min(base + t, base + dur))
                    job.set_progress(done / total, step=tag)
                elif line == "progress=end":
                    done = base + dur
                    job.set_progress(done / total, step=tag)
            proc.wait()
        finally:
            if proc.poll() is None:                          # 被取消 / 异常 → 收尸
                try:
                    proc.kill()
                except Exception:                            # noqa: BLE001
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:                            # noqa: BLE001
                    pass
            th.join(timeout=3)
        if proc.returncode != 0:
            tail = errs[-1] if errs else ("ffmpeg 退出码 %s" % proc.returncode)
            raise RuntimeError("ffmpeg 失败：%s" % tail)
