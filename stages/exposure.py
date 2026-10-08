# -*- coding: utf-8 -*-
"""曝光调整阶段 —— 自动 / 手动 / 静态 三种 gamma 曲线对齐。

原理（算法移植自 exposure-lab/exposure_core.py，数值路径逐步对齐）
------------------------------------------------------------------
相机自动曝光在开头没收敛会让画面偏暗，随后才回到正常亮度。
用「逐帧可变的 gamma」把每帧亮度匹配到参考亮度：

    out = 255 * (in/255) ** (1/gamma)        （ffmpeg eq 滤镜的 gamma 定义）

* 先用一次解码量出每帧的 Y 直方图（160x90，很小）；
* eq 的 gamma 是点运算，所以能用直方图**离线精确求解** gamma，不必反复调 ffmpeg；
* gamma 随时间变化的曲线被简化成折线，写成 ffmpeg 表达式，
  用 eq=gamma='<表达式>':eval=frame 一次编码完成。

与原实现的唯一差别：不依赖 numpy（本项目只允许标准库）。
直方图用 list[256] + collections.Counter 计数，mean / 分位数 / bincount 重映射
与 numpy 版一一对应（见 _stat_of / _stat_after / _lut）。
"""
import math
import os
import re
import statistics
import subprocess
import threading
from collections import Counter

from core.stage import Stage, register, sel, num, rng, chk, txt, path
from core import tool, job as jobmod

# ---------------------------------------------------------------- 常量
SW, SH = 160, 90                     # 量测用的缩小尺寸
FRAME_BYTES = SW * SH                # 一帧 gray 的字节数
GAMMA_MIN, GAMMA_MAX = 0.35, 4.0

QUALITY = {"high": dict(enc="libx264", preset="medium", crf=16),
           "balanced": dict(enc="libx264", preset="veryfast", crf=18),
           "fast": dict(enc="h264_nvenc", preset="p5", crf=18)}

# HDR -> SDR(bt709)：先线性化，再用 hable 曲线压到 SDR。
# 不转换、直接把 HDR 信号当 SDR 输出，在支持 HDR 的播放器里会明显偏亮发灰。
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")

HDR_TRC = ("arib-std-b67", "smpte2084", "smpte428", "bt2020-10", "bt2020-12")

# 能直接装进 mp4（-c:a copy）的音轨编码；其它要转 AAC，否则 mux 会失败。
MP4_AUDIO_OK = ("aac", "mp3", "ac2", "ac3", "eac3", "alac", "mp2", "opus")

_ENC_CACHE = {}
_FLT_CACHE = {}


# ---------------------------------------------------------------- 能力探测
def _has_encoder(name):
    if name not in _ENC_CACHE:
        _ENC_CACHE[name] = tool.has_encoder(name)
    return _ENC_CACHE[name]


def _has_filter(name):
    """ffmpeg 是否带某个滤镜（zscale / tonemap 不是所有构建都有）。"""
    if name not in _FLT_CACHE:
        rc, out, _ = tool.run([tool.FFMPEG, "-hide_banner", "-filters"])
        _FLT_CACHE[name] = (rc == 0 and (" %s " % name) in out)
    return _FLT_CACHE[name]


def _ffmpeg_version():
    rc, out, _ = tool.run([tool.FFMPEG, "-version"])
    if rc != 0 or not out:
        return ""
    line = (out or "").splitlines()[0]
    m = re.search(r"ffmpeg version (\S+)", line)
    return m.group(1) if m else line.strip()[:60]


def _hdr_tags(info):
    """源应有的色彩标签 -> (transfer, primaries, colorspace)。

    有些 HDR 文件只标了一半（例如只有 bt2020 十位、没有 transfer），
    zscale 会直接报 "no path between colorspaces"；缺的就用同族默认值补上，
    已在源里标好的原样写回（等于没改）。
    """
    trc = (info.get("transfer") or "").lower()
    if trc not in HDR_TRC:
        # 杜比视界 profile 5 的基线层是 PQ，其余（8.x 等）是 HLG
        trc = "smpte2084" if info.get("dovi") == 5 else "arib-std-b67"
    pri = (info.get("primaries") or "").lower()
    if pri in ("", "unspecified", "unknown", "reserved"):
        pri = "bt2020"
    return trc, pri, "bt2020nc"


def _tonemap_vf(info):
    """zscale 前的 setparams：把源缺的色彩标签补齐，zscale 才能找到转换路径。"""
    trc, pri, spc = _hdr_tags(info)
    return "setparams=color_trc=%s:color_primaries=%s:colorspace=%s,%s" % (trc, pri, spc, TONEMAP)


# ---------------------------------------------------------------- 直方图数学
def _hist_of(buf):
    """一帧 gray 字节 -> 256 桶直方图（list[int]）。"""
    h = [0] * 256
    for v, c in Counter(buf).items():
        h[v] += c
    return h


def _stat_of(hist, mode="mean"):
    """直方图的统计量；mode=mean/p25/median/p75/p90（同 numpy 版）。"""
    n = 0
    for c in hist:
        n += c
    if n <= 0:
        return 0.0
    if mode == "mean":
        s = 0.0
        for i, c in enumerate(hist):
            s += i * c
        return s / n
    q = {"median": 0.50, "p90": 0.90, "p75": 0.75, "p25": 0.25}.get(mode, 0.5)
    acc = 0.0
    for i, c in enumerate(hist):
        acc += c
        if acc >= q * n:                      # == np.searchsorted(cumsum/n, q)
            return float(i)
    return 255.0


def _lut(g):
    """gamma 的 256 项点运算查表（等价 np.clip(power(y,1/g)*255+0.5,0,255)）。"""
    inv = 1.0 / float(g) if g else 1.0
    out = [0] * 256
    for i in range(256):
        v = math.pow(i / 255.0, inv) * 255.0 + 0.5
        out[i] = 255 if v > 255.0 else (0 if v < 0.0 else int(v))
    return out


def _stat_after(hist, g, mode):
    """把直方图过一遍 gamma 查表后的统计量（离线精确，不用真解码）。"""
    lut = _lut(g)
    h2 = [0.0] * 256
    for i, c in enumerate(hist):
        if c:
            h2[lut[i]] += c
    return _stat_of(h2, mode)


def _solve_gamma(hist, target, mode="mean"):
    """对数域二分求 gamma，使该帧统计量等于 target（同原实现）。"""
    if sum(hist) <= 0 or target <= 1:
        return 1.0
    lo, hi = GAMMA_MIN, GAMMA_MAX
    if _stat_after(hist, lo, mode) > target:
        return lo
    if _stat_after(hist, hi, mode) < target:
        return hi
    for _ in range(28):
        mid = math.sqrt(lo * hi)
        if _stat_after(hist, mid, mode) < target:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


# ---------------------------------------------------------------- 曲线
def _smooth_series(vals, times, window_s):
    """三角窗时间平滑（原样移植）。"""
    if window_s <= 0 or len(vals) < 3:
        return list(vals)
    out = []
    n = len(vals)
    for i in range(n):
        acc = wsum = 0.0
        for j in range(n):
            d = abs(times[j] - times[i])
            if d <= window_s:
                w = 1.0 - d / (window_s + 1e-9)
                acc += vals[j] * w
                wsum += w
        out.append(acc / wsum if wsum else vals[i])
    return out


def _auto_target(samples, mode="mean", from_frac=0.4):
    """自动参考：取视频后段（默认 40% 之后）的稳定亮度中位数。"""
    if not samples:
        return 0.0
    tmax = samples[-1][0]
    vals = [_stat_of(h, mode) for t, h in samples if t >= from_frac * tmax]
    if not vals:
        vals = [_stat_of(h, mode) for _, h in samples]
    return float(statistics.median(vals))


def _build_curve(samples, target, mode="mean", strength=1.0, smooth_s=0.6,
                 max_ev=2.0, floor_stat=6.0, direction="both", tick=None):
    """由量测结果生成 (t, gamma) 曲线（原样移植，多个进度回调）。

    direction: both=双向匹配 / brighten=只提亮偏暗帧 / darken=只压暗偏亮帧
    """
    times = [s[0] for s in samples]
    gammas = []
    total = len(samples)
    for i, (t, h) in enumerate(samples):
        if tick and i % 20 == 0:
            tick(i, total)
        if _stat_of(h, mode) < floor_stat:     # 近乎全黑，不硬拉
            gammas.append(1.0)
            continue
        g = _solve_gamma(h, target, mode)
        g = min(max(g, 2.0 ** -max_ev), 2.0 ** max_ev)
        gammas.append(g)
    gammas = _smooth_series(gammas, times, smooth_s)
    gammas = [1.0 + (g - 1.0) * strength for g in gammas]
    if direction == "brighten":
        gammas = [max(g, 1.0) for g in gammas]
    elif direction == "darken":
        gammas = [min(g, 1.0) for g in gammas]
    return list(zip(times, gammas))


def _simplify(points, tol=0.012, max_points=40):
    """道格拉斯-普克简化，必要时等距抽稀（原样移植）。"""
    if len(points) <= 2:
        return list(points)
    pts = list(points)
    keep = [0, len(pts) - 1]
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        x0, y0 = pts[i]
        x1, y1 = pts[j]
        dx = x1 - x0
        worst, wi = -1.0, -1
        for k in range(i + 1, j):
            x, y = pts[k]
            if dx <= 1e-9:
                d = abs(y - y0)
            else:
                d = abs(y - (y0 + (y1 - y0) * (x - x0) / dx))
            if d > worst:
                worst, wi = d, k
        if worst > tol:
            keep.append(wi)
            stack.append((i, wi))
            stack.append((wi, j))
    keep = sorted(set(keep))
    pts = [pts[i] for i in keep]
    if len(pts) > max_points:
        step = (len(pts) - 1) / float(max_points - 1)
        pts = [pts[int(round(i * step))] for i in range(max_points)]
        pts = sorted(set(pts), key=lambda p: p[0])
    return pts


def _gamma_expr(points, t_offset=0.0):
    """把折线 (t, gamma) 写成 ffmpeg 表达式（原样移植）。"""
    pts = [(max(0.0, t - t_offset), g) for t, g in points]
    pts = [(0.0, pts[0][1])] + pts if pts and pts[0][0] > 0 else pts
    if not pts:
        return "1.0"
    if len(pts) == 1:
        return "%.6f" % pts[0][1]
    expr = "%.6f" % pts[-1][1]
    for i in range(len(pts) - 2, -1, -1):
        t0, g0 = pts[i]
        t1, g1 = pts[i + 1]
        dt = t1 - t0
        if dt <= 1e-6:
            seg = "%.6f" % g1
        else:
            seg = "(%.6f+(%.6f)*(t-%.6f)/%.6f)" % (g0, g1 - g0, t0, dt)
        expr = "if(lt(t,%.6f),%s,%s)" % (t1, seg, expr)
    return expr


# ---------------------------------------------------------------- 手动控制点
def _parse_points(text):
    """把多行 '时间:gamma' 文本解析成 [(t, gamma)]（按时间排序、同点取后者）。"""
    pts = []
    for raw in (text or "").replace("；", ";").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        line = line.replace("，", ",").replace("：", ":")
        parts = [x for x in re.split(r"[\s,:;=]+", line) if x]
        if len(parts) < 2:
            raise RuntimeError("控制点格式不对：%r —— 每行写 时间:gamma，例如 0:1.60"
                               % raw.strip())
        try:
            t, g = float(parts[0]), float(parts[1])
        except ValueError:
            raise RuntimeError("控制点里不是数字：%r" % raw.strip())
        if t < 0 or g <= 0:
            raise RuntimeError("控制点要满足 时间>=0 且 gamma>0：%r" % raw.strip())
        pts.append((t, min(max(g, GAMMA_MIN), GAMMA_MAX)))
    if not pts:
        raise RuntimeError("手动模式还没有控制点：请每行填一个 时间:gamma，例如 0:1.60")
    pts.sort(key=lambda p: p[0])
    out = []
    for t, g in pts:
        if out and abs(t - out[-1][0]) < 1e-6:
            out[-1] = (t, g)          # 同一时刻只留最后一个
        else:
            out.append((t, g))
    return out


# ---------------------------------------------------------------- 量测
def _drain(pipe, sink, limit=200):
    """后台把 stderr 抽干：不读的话长命令会写满管道而死锁。"""
    try:
        for line in pipe:
            if len(sink) < limit:
                sink.append(line.rstrip())
    except Exception:                                   # noqa: BLE001
        pass
    finally:
        try:
            pipe.close()
        except Exception:                               # noqa: BLE001
            pass


def _measure(job, src, dur, tone_vf, keep_hdr, on_tick):
    """一遍解码，量出 [(t, hist[256])]。开头 20 秒密一点，长视频补一段稀疏尾部。"""
    samples = []
    if dur <= 20:
        plan = [(0.0, max(0.0, dur), 10.0, 300)]
    else:
        plan = [(0.0, 20.0, 10.0, 220), (20.0, 0.0, 1.5, 220)]
        tail = 20.0 + 220 / 1.5                    # 1.5fps 只覆盖到 ~166s
        if dur > tail + 10.0:
            plan.append((tail, 0.0, 0.5, 300))      # 补到结尾，否则自动目标落在中段

    for ss, t, fps, cap in plan:
        job.check()
        if tone_vf:
            # 先缩小再 tonemap：量到的就是最终 SDR 像素域，解出的 gamma 才对得上
            vf = "fps=%g,scale=%d:%d,%s,format=gray" % (fps, SW, SH, tone_vf)
        elif keep_hdr:
            vf = "fps=%g,scale=%d:%d,format=gray" % (fps, SW, SH)
        else:
            vf = "fps=%g,scale=%d:%d:in_range=pc:out_range=pc,format=gray" % (fps, SW, SH)
        cmd = [tool.FFMPEG, "-hide_banner", "-loglevel", "error"]
        if ss > 0:
            cmd += ["-ss", "%.4f" % ss]
        if t > 0:
            cmd += ["-t", "%.4f" % t]
        cmd += ["-i", src, "-an", "-vf", vf,
                "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        # tool.popen 是文本模式，会破坏 rawvideo 字节流，所以这里直接用 subprocess
        # （标准库）；ffmpeg 路径仍来自 tool.FFMPEG。
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        errs = []
        th = threading.Thread(target=_drain, args=(p.stderr, errs), daemon=True)
        th.start()
        k = 0
        try:
            while k < cap:
                job.check()
                buf = p.stdout.read(FRAME_BYTES)
                if len(buf) < FRAME_BYTES:
                    break
                samples.append((ss + k / fps, _hist_of(buf)))
                k += 1
                if on_tick and k % 8 == 0:
                    on_tick(len(samples))
        except jobmod.Cancelled:
            try:
                p.kill()
            except Exception:                           # noqa: BLE001
                pass
            raise
        finally:
            try:
                p.stdout.close()
            except Exception:                           # noqa: BLE001
                pass
        p.wait()
        th.join(timeout=2)
        if k == 0 and errs:
            job.log("量测片段 @%.1fs 没取到帧：%s" % (ss, errs[-1][:160]))
    return samples


# ---------------------------------------------------------------- 渲染
def _run_render(job, cmd, total_dur, p0, span, step="编码"):
    """跑 ffmpeg，用 -progress pipe:1 的 out_time_us 换算进度。"""
    p = tool.popen(cmd)
    errs = []
    th = threading.Thread(target=_drain, args=(p.stderr, errs), daemon=True)
    th.start()
    try:
        for line in p.stdout:
            job.check()
            line = line.strip()
            if not line.startswith("out_time_us="):
                continue
            try:
                t = float(line.split("=", 1)[1]) / 1e6
            except (ValueError, IndexError):
                continue
            if total_dur > 0:
                frac = max(0.0, min(0.999, t / total_dur))
                job.set_progress(p0 + span * frac, step="%s %.0f%%" % (step, frac * 100.0))
    except jobmod.Cancelled:
        try:
            p.kill()
        except Exception:                               # noqa: BLE001
            pass
        raise
    finally:
        try:
            p.stdout.close()
        except Exception:                               # noqa: BLE001
            pass
    p.wait()
    th.join(timeout=2)
    return p.returncode, "\n".join(errs)


def _unlink(p):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


@register
class ExposureStage(Stage):
    key = "exposure"
    name = "曝光调整"
    icon = "☀"
    order = 10
    desc = "自动/手动/静态 gamma 曲线，把偏暗段提到目标亮度（支持 HLG/DV 源）"
    accepts = "video"
    produces = "video"
    uses_range = True          # 选区可作"曝光正常的参考片段"

    # ------------------------------------------------------------ 表单
    def schema(self):
        return [
            sel("mode", "模式", [("auto", "自动（分析整段，自动求曲线）"),
                                ("manual", "手动（时间/gamma 控制点）"),
                                ("static", "静态（整段统一）")],
                default="auto",
                help="自动=量测每帧亮度后自动解 gamma 曲线；手动=自己给控制点；静态=整段同一组参数"),
            rng("strength", "强度", 1.0, 0.5, 2.0, 0.05,
                help="1.0=完全对齐；<1 更保守；>1 更激进（曲线按 1+(g-1)*强度 缩放）"),
            num("target", "目标亮度 0-255（0=自动）", 0, 0, 255, 1,
                help="自动=取视频后段稳定亮度作参考；想手动定标就填 0-255"),
            sel("stat", "亮度统计口径", [("mean", "平均亮度（推荐）"), ("median", "中位亮度"),
                                     ("p25", "25 分位"), ("p75", "75 分位"), ("p90", "90 分位")],
                default="mean", help="自动模式量测与求解都用这个统计量"),
            sel("direction", "调整方向", [("both", "双向（提亮+压暗）"),
                                      ("brighten", "只提亮偏暗段"),
                                      ("darken", "只压暗偏亮段")],
                default="both", help="只想救开头偏暗，就选「只提亮」"),
            txt("points", "手动控制点", "",
                "每行一个 时间:gamma，例如 0:1.60 / 2.5:1.20 / 10:1.00；# 后面是注释。仅在手动模式生效",
                multiline=True),
            rng("gamma", "静态 Gamma", 1.0, 0.35, 3.0, 0.01, "仅静态模式"),
            rng("brightness", "静态 亮度", 0.0, -1.0, 1.0, 0.01, "仅静态模式，eq 的 brightness"),
            rng("contrast", "静态 对比度", 1.0, 0.0, 3.0, 0.01, "仅静态模式"),
            rng("saturation", "静态 饱和度", 1.0, 0.0, 3.0, 0.01, "仅静态模式"),
            sel("hdr", "HDR 处理", [("auto", "自动（HDR 源 → 转 SDR bt709）"),
                                   ("keep", "保留 HDR（10bit HEVC 输出）"),
                                   ("off", "不处理（按 SDR 直接输出）")],
                default="auto",
                help="源是 HLG/DV/PQ 时才生效；保留 HDR 需要 libx265 或 hevc_nvenc"),
            sel("quality", "编码质量", [("high", "高（libx264 crf16 medium）"),
                                     ("balanced", "均衡（libx264 crf18 veryfast）"),
                                     ("fast", "快（h264_nvenc cq18，需显卡）")],
                default="high", help="只影响重新编码的成品，曲线本身不受影响"),
            num("smooth", "平滑窗口 秒", 0.6, 0.0, 5.0, 0.1,
                help="曲线时间平滑的三角窗半径；0=不平滑"),
            num("max_ev", "最大曝光补偿 EV", 2.0, 0.25, 4.0, 0.25,
                help="单帧 gamma 的上限：2.0 表示最多 4 倍/1/4 倍亮度"),
            chk("force_reencode", "曲线近似恒等也重新编码", False,
                help="默认恒等且无需 HDR 转换时直接复用源文件，绝不二次损失画质"),
            path("outdir", "输出目录", "", kind="dir",
                 help="留空=与源文件同目录；文件名自动加 -exposure 后缀且不覆盖已有文件"),
        ]

    # ------------------------------------------------------------ 可用性
    def available(self):
        ver = _ffmpeg_version()
        if not ver:
            return {"ok": False,
                    "detail": "找不到 ffmpeg：请把 ffmpeg.exe 放进 PATH 或 C:\\ffmpeg\\bin"}
        miss = []
        if not _has_encoder("libx264"):
            miss.append("libx264（无 CPU H.264 编码器）")
        if not (_has_filter("zscale") and _has_filter("tonemap")):
            miss.append("zscale/tonemap（HDR→SDR 不可用，只能选「保留 HDR」或「不处理」）")
        if not (_has_encoder("libx265") or _has_encoder("hevc_nvenc")):
            miss.append("10bit HEVC 编码器（「保留 HDR」不可用）")
        if not _has_encoder("h264_nvenc"):
            miss.append("h264_nvenc（「快」档会回退 CPU）")
        detail = "ffmpeg %s" % ver
        if miss:
            detail += "；缺少 " + "、".join(miss)
        return {"ok": True, "detail": detail}

    # ------------------------------------------------------------ 执行
    # ------------------------------------------------------------ 曲线数据（前端画图）
    def analyze(self, inputs, opts):
        """返回给前端画「亮度曲线 / 曝光补偿曲线」用的数据。

        复刻 run() 的量测域处理：HDR 且选了自动转 SDR 时，先在 tonemap 之后的
        SDR 域量测 —— 这样画出来的曲线和实际成品是对得上的。
        """
        if not inputs:
            return None
        src = inputs[0]
        info = tool.probe(src)
        if not info:
            return None
        dur = float(info.get("duration") or 0)
        if dur <= 0:
            return None

        class _Shim(object):
            """_measure 需要一个能 check/log 的对象；这里不需要真进度。"""

            def check(self):
                return False

            def log(self, *a):
                pass

        hdr_mode = str(opts.get("hdr") or "auto")
        keep_hdr = bool(info.get("hdr")) and hdr_mode == "keep"
        tonemap = bool(info.get("hdr")) and hdr_mode == "auto"
        tone_vf = None
        if tonemap:
            if _has_filter("zscale") and _has_filter("tonemap"):
                tone_vf = _tonemap_vf(info)
            else:
                tonemap = False

        samples = _measure(_Shim(), src, dur, tone_vf, keep_hdr, lambda *a: None)
        if not samples:
            return None

        stat = str(opts.get("stat") or "mean")
        luma = [[round(float(t), 3), round(float(_stat_of(h, stat)), 2)] for t, h in samples]

        target = _f(opts.get("target"), 0.0)
        auto_t = False
        if target <= 0:
            target = _auto_target(samples, stat)
            auto_t = True

        curve = []
        if str(opts.get("mode") or "auto") == "auto":
            raw = _build_curve(samples, target, stat,
                               _f(opts.get("strength"), 1.0),
                               _f(opts.get("smooth"), 0.6),
                               _f(opts.get("max_ev"), 2.0),
                               direction=str(opts.get("direction") or "both"))
            curve = [[round(float(t), 3), round(float(g), 4)] for t, g in raw]
        return {"duration": round(dur, 3), "target": round(float(target), 2),
                "auto_target": auto_t, "luma": luma, "curve": curve,
                "fps": info.get("fps"), "w": info.get("width"), "h": info.get("height"),
                "hdr": bool(info.get("hdr")), "tonemapped": bool(tonemap)}

    def run(self, job, inputs, opts):
        if not inputs:
            raise RuntimeError("没有输入文件")
        src = inputs[0]
        if not os.path.isfile(src):
            raise RuntimeError("输入文件不存在：%s" % src)
        info = tool.probe(src)
        if not info:
            raise RuntimeError("ffprobe 读不出这个视频的信息，可能不是视频或已损坏：%s"
                               % os.path.basename(src))

        mode = (opts.get("mode") or "auto").strip()
        quality = (opts.get("quality") or "high").strip()
        strength = _f(opts.get("strength"), 1.0)
        smooth_s = max(0.0, _f(opts.get("smooth"), 0.6))
        max_ev = min(max(_f(opts.get("max_ev"), 2.0), 0.25), 4.0)
        stat = (opts.get("stat") or "mean").strip()
        direction = (opts.get("direction") or "both").strip()

        dur = _f(info.get("duration"), 0.0)
        if dur <= 0 and info.get("fps") and info.get("nb_frames"):
            dur = info["nb_frames"] / info["fps"]
        outdir = (opts.get("outdir") or "").strip() or os.path.dirname(os.path.abspath(src))
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as e:
            raise RuntimeError("输出目录建不出来：%s（%s）" % (outdir, e))

        # ---- 源信息 / 旋转 / HDR --------------------------------------
        bits = ["%dx%d" % (info["width"], info["height"]),
                "%.2ffps" % (info["fps"] or 0.0), tool.human_dur(dur),
                tool.human_size(info["size"]), info["codec"] or "?", info["pix_fmt"] or "?"]
        job.log("源: %s" % " / ".join(b for b in bits if b))
        rot = int(info.get("rotation") or 0)
        if rot:
            # 旋转是元数据、不是错误：ffmpeg 默认按显示方向解码（autorotate），
            # 滤镜链是点运算所以不受影响；成品方向正确、但存储宽高会对调。
            job.log("源带旋转元数据 %+d°：按显示方向解码（不是错误），成品方向正确、"
                    "存储宽高会 %dx%d -> %dx%d"
                    % (rot, info["width"], info["height"], info["height"], info["width"]))

        hdr_mode = (opts.get("hdr") or "auto").strip()
        is_hdr = bool(info.get("hdr"))
        tonemap = keep_hdr = False
        if is_hdr and hdr_mode != "off":
            if hdr_mode == "keep":
                keep_hdr = True
            else:
                tonemap = True
        desc = " / ".join(x for x in [
            "DV%s" % info["dovi"] if info.get("dovi") is not None else "",
            {"arib-std-b67": "HLG", "smpte2084": "PQ/HDR10"}.get(info.get("transfer") or "",
                                                                 info.get("transfer") or ""),
            info.get("primaries") or "", "%dbit" % info.get("depth", 8)] if x)
        tone_vf = None
        if is_hdr:
            job.log("HDR 源: %s -> %s" % (desc, ("保留 HDR(10bit)" if keep_hdr else
                                                ("转 SDR bt709(tonemap)" if tonemap else "不转换"))))
            if tonemap:
                trc, pri, _spc = _hdr_tags(info)
                if (info.get("transfer") or "").lower() not in HDR_TRC:
                    job.log("源的 transfer 标签缺失，按 %s 解读（DV profile %s）"
                            % (trc, info.get("dovi")))
                tone_vf = _tonemap_vf(info)
            if tonemap and not (_has_filter("zscale") and _has_filter("tonemap")):
                raise RuntimeError("这个 ffmpeg 没编译 zscale/tonemap 滤镜，转不了 SDR；"
                                   "请把 HDR 处理改成「保留 HDR」或换一个 full build 的 ffmpeg")
            if keep_hdr and not (_has_encoder("libx265") or _has_encoder("hevc_nvenc")):
                raise RuntimeError("保留 HDR 需要 10bit HEVC 编码器（libx265 或 hevc_nvenc），当前 ffmpeg 都没有")
        elif hdr_mode == "keep":
            job.log("源不是 HDR，忽略「保留 HDR」（不会凭空造 HDR）")
        if is_hdr and hdr_mode == "off":
            job.log("注意：HDR 按 SDR 直接输出，在支持 HDR 的播放器里会偏亮发灰")

        # ---- 求曲线 ----------------------------------------------------
        expr = None
        static_eq = None
        pts = []
        if mode == "manual":
            pts = _parse_points(opts.get("points"))
            pts = [(t, min(max(1.0 + (g - 1.0) * strength, GAMMA_MIN), GAMMA_MAX)) for t, g in pts]
            expr = _gamma_expr(pts)
            job.log("手动曲线 %d 个控制点" % len(pts))
            p0, span = 0.05, 0.90
        elif mode == "static":
            static_eq = {"brightness": _f(opts.get("brightness"), 0.0),
                         "contrast": _f(opts.get("contrast"), 1.0),
                         "gamma": min(max(_f(opts.get("gamma"), 1.0), GAMMA_MIN), GAMMA_MAX),
                         "saturation": _f(opts.get("saturation"), 1.0)}
            job.log("静态: " + " ".join("%s=%.3f" % kv for kv in sorted(static_eq.items())))
            p0, span = 0.10, 0.85
        else:
            mode = "auto"
            job.set_progress(0.02, step="量测亮度")
            samples = _measure(job, src, dur, tone_vf, keep_hdr,
                               lambda n: job.set_progress(min(0.42, 0.02 + n * 0.001),
                                                          step="量测亮度 %d 帧" % n))
            if not samples:
                raise RuntimeError("量测失败：一帧都没读到，源里可能没有可解码的视频轨")
            tmax = samples[-1][0] or dur
            job.log("量测完成：%d 个采样点，覆盖到 %s" % (len(samples), tool.human_dur(tmax)))

            target = _f(opts.get("target"), 0.0)
            if target <= 0:
                target = _auto_target(samples, stat)
                job.log("自动目标亮度：%.2f（%s，取视频后段稳定亮度）" % (target, stat))
            else:
                target = min(max(target, 1.0), 254.0)
                job.log("指定目标亮度：%.2f（%s）" % (target, stat))

            job.set_progress(0.45, step="求解 gamma 曲线")
            raw = _build_curve(samples, target, stat, strength, smooth_s, max_ev,
                               direction=direction,
                               tick=lambda i, n: job.set_progress(
                                   0.45 + 0.10 * (float(i) / max(1, n)), step="求解 gamma 曲线"))
            pts = _simplify(raw, 0.012, 40)
            expr = _gamma_expr(pts)
            gs = [g for _, g in pts]
            job.log("曲线：%d 个采样 -> %d 段折线，gamma %.3f~%.3f，强度 %.2f，方向 %s"
                    % (len(raw), len(pts), min(gs), max(gs), strength, direction))
            for i in range(0, len(pts), 8):
                job.log("  " + "  ".join("%.2fs:%.3f" % p for p in pts[i:i + 8]))
            p0, span = 0.55, 0.44

        # ---- 恒等就免编码 ----------------------------------------------
        if mode == "static":
            ident = (abs(static_eq["brightness"]) < 1e-6 and abs(static_eq["contrast"] - 1.0) < 1e-6
                     and abs(static_eq["gamma"] - 1.0) < 1e-6 and abs(static_eq["saturation"] - 1.0) < 1e-6)
        else:
            ident = all(abs(g - 1.0) < 0.01 for _, g in pts)
        if ident and not tonemap and not keep_hdr and not opts.get("force_reencode"):
            job.log("曲线近似恒等，且无需 HDR 转换 —— 直接复用源文件（不再编码一次，零损失）")
            job.set_progress(1.0, step="无需处理")
            return src
        if ident:
            job.log("曲线近似恒等，但需要 HDR 转换/用户强制重编，继续编码")

        # ---- 组装命令 --------------------------------------------------
        vf = "eq=gamma='%s':eval=frame" % expr if static_eq is None else \
            "eq=brightness=%.4f:contrast=%.4f:gamma=%.4f:saturation=%.4f" % (
                static_eq["brightness"], static_eq["contrast"], static_eq["gamma"], static_eq["saturation"])
        if tone_vf:
            vf = tone_vf + "," + vf
        vflog = vf if len(vf) <= 300 else vf[:300] + " ...(表达式共 %d 字符)" % len(vf)
        job.log("滤镜: %s" % vflog)

        has_audio = bool(info.get("has_audio"))
        acodec = (info.get("audio_codec") or "").lower()
        copy_audio = has_audio and acodec in MP4_AUDIO_OK
        if has_audio and not copy_audio:
            job.log("音轨 %s 不能直接装进 mp4，转为 AAC 192k" % (acodec or "?"))

        stem = tool.safe_name(os.path.splitext(os.path.basename(src))[0])
        dst = tool.unique_out(outdir, stem + "-exposure" + ("-hdr" if keep_hdr else ""))
        cmd = self._cmd(src, dst, vf, tonemap, keep_hdr, quality, info, copy_audio)
        job.log("输出: %s" % dst)

        # ---- 编码 ------------------------------------------------------
        try:
            rc, err = _run_render(job, cmd, dur, p0, span)
            if rc != 0 and copy_audio and "Could not find tag for codec" in (err or ""):
                job.log("音轨直封装失败，改成转 AAC 重试…")
                copy_audio = False
                cmd = self._cmd(src, dst, vf, tonemap, keep_hdr, quality, info, copy_audio)
                rc, err = _run_render(job, cmd, dur, p0, span)
        except jobmod.Cancelled:
            _unlink(dst)                    # 取消时不留半成品，异常照常往上抛
            raise
        if rc != 0 or not os.path.exists(dst) or os.path.getsize(dst) <= 0:
            _unlink(dst)
            tail = [x for x in (err or "").strip().splitlines() if x.strip()]
            raise RuntimeError("编码失败（ffmpeg 退出码 %s）：%s"
                               % (rc, tail[-1][:300] if tail else "没有任何错误输出"))
        job.set_progress(1.0, step="完成")
        job.log("完成：%s（%s）" % (os.path.basename(dst), tool.human_size(os.path.getsize(dst))))
        return dst

    # ------------------------------------------------------------ 命令行
    def _cmd(self, src, dst, vf, tonemap, keep_hdr, quality, info, copy_audio):
        q = dict(QUALITY.get(quality, QUALITY["high"]))
        cmd = [tool.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
        cmd += ["-i", src, "-map", "0:v:0"]
        if info.get("has_audio"):
            cmd += ["-map", "0:a:0?"]
        cmd += ["-vf", vf]
        if keep_hdr:
            # 10bit HEVC：libx265 最稳，只有「快」档才用 NVENC（列出的编码器不一定有可用显卡）
            enc = "libx265"
            if _has_encoder("hevc_nvenc") and (quality == "fast" or not _has_encoder("libx265")):
                enc = "hevc_nvenc"
            trc, pri, spc = _hdr_tags(info)
            cmd += ["-c:v", enc, "-profile:v", "main10", "-pix_fmt", "yuv420p10le",
                    "-color_trc", trc, "-color_primaries", pri, "-colorspace", spc,
                    "-tag:v", "hvc1"]
            if enc == "hevc_nvenc":
                cmd += ["-preset", "p5", "-rc", "vbr", "-cq", str(q["crf"]), "-b:v", "0"]
            else:
                # colr 盒子的标签由上面几个 -color_* 写，比特流 VUI 必须由 x265 自己写
                cmd += ["-preset", "medium", "-crf", str(q["crf"]),
                        "-x265-params", "log-level=error:colorprim=%s:transfer=%s:colormatrix=%s"
                                        % (pri, trc, spc)]
        else:
            enc = q["enc"]
            if enc == "h264_nvenc" and not _has_encoder("h264_nvenc"):
                enc, q["preset"], q["crf"] = "libx264", "veryfast", 18
            cmd += ["-c:v", enc]
            if enc == "libx264":
                cmd += ["-preset", q["preset"], "-crf", str(q["crf"])]
            else:
                cmd += ["-preset", q["preset"], "-rc", "vbr", "-cq", str(q["crf"]), "-b:v", "0"]
            if tonemap:
                # tonemap 出来的是 tv 范围 bt709：必须按 tv 编码，不能再标 yuvj420p
                cmd += ["-pix_fmt", "yuv420p", "-color_primaries", "bt709",
                        "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
            else:
                # 与 exposure_core 一致：SDR 走全范围 yuvj420p
                cmd += ["-pix_fmt", "yuvj420p"]
        if info.get("has_audio"):
            if copy_audio:
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", dst]
        return cmd


def _f(v, dflt):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(dflt)
