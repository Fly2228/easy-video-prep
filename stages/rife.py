# -*- coding: utf-8 -*-
"""补帧（RIFE）—— 用 rife-ncnn-vulkan 做神经网络插帧。

流程：ffprobe 探测 -> 拆帧到临时目录 -> rife-ncnn-vulkan 插帧 -> 合成回视频（原音频 -c:a copy）
约束：时间轴上按"时长 x 目标帧率"算目标帧数；临时目录 100% 清理；全部用绝对路径。
"""
import os
import re
import shutil
import tempfile
import threading
import time

from core.stage import Stage, register, sel, num, rng, chk, txt, path
from core import tool, job as jobmod

# RIFE 的位置由用户在表单里指定（不写死任何个人路径）。
# 留空时 available() 会提示，run() 会在真正开跑前给出人话错误。
RIFE_EXE = ""
MODEL_DIR = ""

# 进度分段：拆帧 / 插帧 / 合成
P_EXTRACT = 0.12
P_RIFE = 0.82
P_ENCODE = 0.995

# HDR(HLG/PQ) -> SDR BT.709 的标准 zscale + tonemap 链
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
           "format=yuv420p")

_RE_US = re.compile(r"out_time_us=(\d+)")
_RE_MS = re.compile(r"out_time_ms=(\d+)")
_RE_HH = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_RE_DONE = re.compile(r"\bdone\b")
_RE_IDX = re.compile(r"^(\d+)\.")

_ENV = {}


# ---- 小工具 ---------------------------------------------------------------
def _which(p):
    """把 "ffmpeg" 这类裸名字解析成真实路径；找不到返回 ""。"""
    if p and os.path.isfile(p):
        return p
    return (shutil.which(p) or "") if p else ""


def _has_encoder(name):
    if "enc" not in _ENV:
        try:
            rc, out, _ = tool.run([tool.FFMPEG, "-hide_banner", "-encoders"])
            _ENV["enc"] = out if rc == 0 else ""
        except Exception:                                    # noqa: BLE001
            _ENV["enc"] = ""
    return name in _ENV["enc"]


def _has_filter(name):
    if "filt" not in _ENV:
        try:
            rc, out, _ = tool.run([tool.FFMPEG, "-hide_banner", "-filters"])
            _ENV["filt"] = out if rc == 0 else ""
        except Exception:                                    # noqa: BLE001
            _ENV["filt"] = ""
    return re.search(r"(^|\s)%s(\s)" % re.escape(name), _ENV["filt"]) is not None


def _show(args):
    out = []
    for a in args:
        a = str(a)
        out.append(a if a and " " not in a else '"%s"' % a)
    return " ".join(out)


def _kill(proc):
    """取消/异常时确保子进程死透（Windows 上连整棵进程树一起杀）。"""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            tool.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], timeout=20)
        except Exception:                                    # noqa: BLE001
            pass
    try:
        proc.kill()
    except Exception:                                        # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:                                        # noqa: BLE001
        pass


def _count_frames(d, ext):
    try:
        return sum(1 for f in os.listdir(d) if f.lower().endswith(ext))
    except OSError:
        return 0


def _first_index(d, ext):
    low = None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for f in names:
        if not f.lower().endswith(ext):
            continue
        m = _RE_IDX.match(f)
        if m:
            v = int(m.group(1))
            if low is None or v < low:
                low = v
    return low


def _fmt_fps(f):
    f = float(f)
    if abs(f - round(f)) < 1e-6:
        return str(int(round(f)))
    return ("%.6f" % f).rstrip("0").rstrip(".")


# ---- 命令执行（带进度）----------------------------------------------------
def _run_ffmpeg(job, args, total, p0, p1, step):
    """跑 ffmpeg，按 out_time_us= 换算进度；失败抛人话错误。"""
    job.log("$ " + _show(args))
    proc = tool.popen(args, stdout_pipe=False, stderr_pipe=True)
    errs = []
    try:
        for line in proc.stderr:
            job.check()
            m = _RE_US.search(line) or _RE_MS.search(line)
            if m:
                frac = (float(m.group(1)) / 1e6 / total) if total > 0 else 0.0
            else:
                m = _RE_HH.search(line)
                if m:
                    sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    frac = (sec / total) if total > 0 else 0.0
                else:
                    s = line.strip()
                    if s:
                        errs.append(s)
                        if len(errs) > 80:
                            del errs[:30]
                    continue
            job.set_progress(p0 + (p1 - p0) * min(1.0, max(0.0, frac)), step)
        proc.wait()
    except jobmod.Cancelled:
        job.log("收到取消，正在终止 ffmpeg 进程…")
        raise
    finally:
        _kill(proc)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg %s 失败（返回码 %s）：%s"
                           % (step, proc.returncode, "\n".join(errs[-12:]) or "无错误输出"))


def _run_rife(job, exe, model, in_dir, out_dir, target, pattern, gpu_id, ext, p0, p1):
    """跑 rife-ncnn-vulkan。用 -v 让它逐帧打 "done" 行；同时数输出目录里的帧数兜底。"""
    cmd = [exe, "-v", "-i", in_dir, "-o", out_dir, "-m", model,
           "-n", str(int(target)), "-f", pattern]
    gpu_id = str(gpu_id or "").strip()
    if gpu_id:
        cmd += ["-g", gpu_id]
    job.log("$ " + _show(cmd))

    proc = tool.popen(cmd, stdout_pipe=True, stderr_pipe=True)
    st = {"done": 0}
    errs = []

    def _reader():                                           # 必须一直读，否则管道满会卡死子进程
        try:
            for line in proc.stdout:
                if _RE_DONE.search(line):
                    st["done"] += 1
        except Exception:                                    # noqa: BLE001
            pass

    def _drain():
        try:
            for line in proc.stderr:
                s = line.strip()
                if s:
                    errs.append(s)
                    if len(errs) > 80:
                        del errs[:30]
        except Exception:                                    # noqa: BLE001
            pass

    t1 = threading.Thread(target=_reader)
    t2 = threading.Thread(target=_drain)
    t1.daemon = t2.daemon = True
    t1.start()
    t2.start()

    t0 = time.time()
    last_log = t0
    job.log("RIFE 开始：目标 %d 帧（每个已完成帧都会刷新进度）" % int(target))
    try:
        while True:
            job.check()                                      # 取消时抛 Cancelled，下面 finally 杀进程
            if proc.poll() is not None:
                break
            now = time.time()
            done = max(st["done"], _count_frames(out_dir, ext))
            frac = min(1.0, done / float(target)) if target > 0 else 0.0
            job.set_progress(p0 + (p1 - p0) * frac, "插帧 %d/%d 帧" % (done, target))
            if now - last_log >= 30.0:
                last_log = now
                el = now - t0
                eta = (el / frac - el) if frac > 0.002 else 0.0
                job.log("插帧 %d/%d（%.1f%%）已用 %s，预计还需 %s"
                        % (done, target, frac * 100.0, tool.human_dur(el), tool.human_dur(eta)))
            time.sleep(0.4)
    except jobmod.Cancelled:
        job.log("收到取消，正在终止 RIFE 进程…")
        raise
    finally:
        _kill(proc)
        t1.join(timeout=5)
        t2.join(timeout=5)

    if proc.returncode != 0:
        raise RuntimeError("RIFE 插帧失败（返回码 %s）：%s"
                           % (proc.returncode, "\n".join(errs[-10:]) or "无错误输出"))
    job.set_progress(p1, "插帧完成")
    job.log("RIFE 完成：%d 帧，用时 %s"
            % (_count_frames(out_dir, ext), tool.human_dur(time.time() - t0)))


def _video_args(o):
    """编码器参数。auto 优先 NVENC，否则 libx264。"""
    enc = str(o.get("encoder") or "auto").lower()
    if enc == "auto":
        enc = "nvenc" if _has_encoder("h264_nvenc") else "x264"
    speed = str(o.get("speed") or "medium").lower()
    if speed not in ("slow", "medium", "fast"):
        speed = "medium"
    try:
        q = int(round(float(o.get("quality") or 17)))
    except (TypeError, ValueError):
        q = 17
    q = max(8, min(35, q))
    if enc == "nvenc":
        if not _has_encoder("h264_nvenc"):
            raise RuntimeError("这台机器的 ffmpeg 没有 h264_nvenc 编码器，请把编码器改成“自动”或 x264")
        preset = {"slow": "p7", "medium": "p5", "fast": "p3"}[speed]
        # -rc vbr + -cq + -b:v 0 = NVENC 的恒定质量模式
        return ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr",
                "-cq", str(q), "-b:v", "0", "-pix_fmt", "yuv420p", "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", speed, "-crf", str(q),
            "-pix_fmt", "yuv420p", "-profile:v", "high"]


@register
class RifeStage(Stage):
    key = "rife"
    name = "补帧 RIFE"
    icon = "⚡"
    order = 30
    desc = ("用 rife-ncnn-vulkan 神经网络插帧提升帧率（默认 60fps），原音频无损搬过来。"
            "很慢且中间帧很占磁盘，建议先用 JPEG 中间帧试跑。")
    accepts = "video"
    produces = "video"

    # ---- 能力检查 ---------------------------------------------------------
    def available(self):
        """只检查"改不了"的东西（ffmpeg/ffprobe）。

        RIFE 的路径是用户在表单里填的：available() 看不到当前表单值，
        若拿它去挡，默认留空会让运行按钮永远点不动。
        所以路径交给 run() 在开跑前校验并报人话错误。
        """
        miss = []
        if not _which(tool.FFMPEG):
            miss.append("ffmpeg")
        if not _which(tool.FFPROBE):
            miss.append("ffprobe")
        if miss:
            return {"ok": False, "detail": "缺少：%s（请安装并加入 PATH）" % "；".join(miss)}
        enc = "h264_nvenc" if _has_encoder("h264_nvenc") else "libx264"
        if RIFE_EXE and os.path.isfile(RIFE_EXE):
            return {"ok": True,
                    "detail": "RIFE: %s｜模型: %s｜编码: %s｜ffmpeg: %s"
                              % (os.path.basename(RIFE_EXE), os.path.basename(MODEL_DIR) or "(未指定)",
                                 enc, _which(tool.FFMPEG))}
        return {"ok": True,
                "detail": "请在表单里指定 rife-ncnn-vulkan.exe 与模型目录"
                          "（如 rife-v4.6，下载见 README）；编码 %s" % enc}

    # ---- 表单 -------------------------------------------------------------
    def schema(self):
        return [
            path("rife_exe", "RIFE 可执行文件", RIFE_EXE, kind="file",
                 help="rife-ncnn-vulkan.exe 的绝对路径"),
            path("model_dir", "RIFE 模型目录", MODEL_DIR, kind="dir",
                 help="例如 rife-v4.6；目录内应有 flownet.param / flownet.bin"),
            num("target_fps", "目标帧率", 60, min=1, max=480, step=1,
                help="目标帧数 = 时长 × 目标帧率；必须高于源帧率（RIFE 只插不降）"),
            num("dup_factor", "整数倍插帧(0=关)", 0, min=0, max=8, step=1,
                help=">1 时忽略目标帧率，直接按倍数插帧（rife -n = 源帧数 × 本值）"),
            sel("encoder", "编码器",
                [("auto", "自动（优先 NVENC）"), ("nvenc", "NVENC H.264（快）"),
                 ("x264", "libx264（兼容最好）")], default="auto",
                help="NVENC 需要 N 卡 + 驱动；失败会自动退回 libx264 的思路见“自动”"),
            rng("quality", "质量 CRF/CQ（越小越好）", 17, 8, 35, 1,
                help="x264 用 -crf，NVENC 用 -cq；17 接近视觉无损"),
            sel("speed", "编码速度/质量",
                [("slow", "慢（质量最好）"), ("medium", "中（平衡）"), ("fast", "快（质量略降）")],
                default="medium"),
            sel("frame_format", "中间帧格式",
                [("png", "PNG 无损（很占磁盘）"), ("jpg", "JPEG q2（快、省磁盘，轻微有损）")],
                default="png",
                help="1080p60 的 4 分钟素材，PNG 中间帧可能要几十 GB"),
            chk("hdr_tonemap", "HDR 转 SDR（zscale+tonemap）", False,
                help="源是 HLG/PQ 时勾选，转成 BT.709；不勾会按原样抽 8bit 帧，可能出现偏色/高光裁切"),
            chk("keep_audio", "保留原音频（-c:a copy）", True),
            txt("gpu_id", "GPU 编号（留空=自动）", "",
                help="留空自动选择；-1 用 CPU（很慢）；0/1… 指定显卡"),
            path("outdir", "输出目录（留空=与源同目录）", "", kind="dir"),
        ]

    # ---- 执行 -------------------------------------------------------------
    def run(self, job, inputs, opts):
        o = dict(opts or {})
        if not inputs:
            raise RuntimeError("补帧阶段没有拿到输入视频")
        src = os.path.abspath(str(inputs[0]))
        if not os.path.isfile(src):
            raise RuntimeError("输入文件不存在：%s" % src)

        rife = os.path.abspath(str(o.get("rife_exe") or RIFE_EXE))
        model = os.path.abspath(str(o.get("model_dir") or MODEL_DIR))
        if not os.path.isfile(rife):
            raise RuntimeError("找不到 RIFE 可执行文件：%s（可在表单里改 rife_exe）" % rife)
        if not os.path.isfile(os.path.join(model, "flownet.param")):
            raise RuntimeError("RIFE 模型目录不对（%s 里没有 flownet.param）" % model)
        if not _which(tool.FFMPEG) or not _which(tool.FFPROBE):
            raise RuntimeError("找不到 ffmpeg / ffprobe，无法补帧")

        job.set_progress(0.0, "探测源视频")
        job.check()
        info = tool.probe(src)
        if info is None:
            raise RuntimeError("ffprobe 读不出视频信息：%s" % os.path.basename(src))

        dur = float(info.get("duration") or 0)
        src_fps = float(info.get("fps") or 0)
        if src_fps <= 0 and dur > 0 and info.get("nb_frames"):
            src_fps = float(info["nb_frames"]) / dur
        if dur <= 0:
            raise RuntimeError("读不出源视频时长（ffprobe）：%s" % os.path.basename(src))
        if src_fps <= 0:
            raise RuntimeError("读不出源视频帧率（ffprobe）：%s" % os.path.basename(src))

        try:
            target_fps = float(o.get("target_fps") or 60)
        except (TypeError, ValueError):
            target_fps = 60.0
        if target_fps <= 0:
            target_fps = 60.0
        try:
            dup = int(round(float(o.get("dup_factor") or 0)))
        except (TypeError, ValueError):
            dup = 0
        dup = max(0, min(8, dup))
        if dup == 1:
            dup = 0

        fmt = str(o.get("frame_format") or "png").lower()
        ext = ".jpg" if fmt in ("jpg", "jpeg") else ".png"

        job.log("源: %s  %dx%d  %.3ffps  %s  %s  %s"
                % (os.path.basename(src), info.get("width", 0), info.get("height", 0),
                   src_fps, tool.human_dur(dur), tool.human_size(info.get("size", 0)),
                   info.get("codec") or "?"))
        rot = int(info.get("rotation") or 0)
        if rot:
            job.log("检测到旋转元数据 %+d°：抽帧时由 ffmpeg 自动摆正，这不是错误。" % rot)
        if info.get("hdr"):
            job.log("检测到 HDR（transfer=%s, %sbit）：中间帧只有 8bit，可能偏色/压高光。"
                    % (info.get("transfer") or "?", info.get("depth") or 8))
            if not bool(o.get("hdr_tonemap")):
                job.log("提示：若成片偏灰偏暗，请勾选“HDR 转 SDR”。")

        outdir = os.path.abspath(str(o.get("outdir") or os.path.dirname(src) or os.getcwd()))
        os.makedirs(outdir, exist_ok=True)

        tmp = tempfile.mkdtemp(prefix="rife_tmp_")
        fin = os.path.join(tmp, "in")
        fout = os.path.join(tmp, "out")
        os.makedirs(fin)
        os.makedirs(fout)
        try:
            # 预估一下磁盘，早点提醒比中途爆盘好
            tent_out_fps = src_fps * dup if dup >= 2 else target_fps
            est_frames = max(1, int(dur * tent_out_fps))
            per = 2.2e6 if ext == ".png" else 0.36e6
            need = est_frames * per * 2.0
            try:
                free = shutil.disk_usage(tmp)[2]
            except OSError:
                free = -1
            job.log("预计输出约 %d 帧（%.3ffps）；临时目录 %s，可用 %s，预计需要约 %s"
                    % (est_frames, tent_out_fps, tmp,
                       tool.human_size(free) if free >= 0 else "?",
                       tool.human_size(need)))
            if 0 <= free < need:
                job.log("警告：临时盘可用空间可能不够（PNG 中间帧非常占地方），"
                        "建议改用 JPEG 中间帧或清理磁盘。")

            # [1/3] 拆帧
            job.check()
            vf = []
            if info.get("hdr") and bool(o.get("hdr_tonemap")):
                if not (_has_filter("zscale") and _has_filter("tonemap")):
                    raise RuntimeError("这个 ffmpeg 缺少 zscale/tonemap 滤镜，做不了 HDR→SDR；"
                                       "请取消该选项，或换用带 libzimg 的 ffmpeg")
                vf.append(TONEMAP)
            args = [tool.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                    "-nostats", "-progress", "pipe:2", "-y", "-i", src,
                    "-an", "-sn", "-dn", "-fps_mode", "passthrough"]
            if vf:
                args += ["-vf", ",".join(vf)]
            if ext == ".jpg":
                args += ["-q:v", "2"]
            args += [os.path.join(fin, "frame_%08d" + ext)]
            _run_ffmpeg(job, args, dur, 0.0, P_EXTRACT, "拆帧")

            n_in = _count_frames(fin, ext)
            if n_in <= 0:
                raise RuntimeError("拆帧失败：临时目录里一帧都没有（源视频可能损坏）")

            # 目标帧数
            if dup >= 2:
                out_fps = src_fps * dup
                target = n_in * dup
            else:
                out_fps = target_fps
                target = max(2, int(round(dur * out_fps)))
            if target <= n_in:
                raise RuntimeError("目标帧率 %.3f 不高于源帧率 %.3f：RIFE 只能插帧不能降帧，"
                                   "请把目标帧率调高，或改用 dup_factor≥2 的整数倍插帧"
                                   % (out_fps, src_fps))
            job.log("插帧：%d 帧 → %d 帧（%.3f → %.3ffps，%s）"
                    % (n_in, target, src_fps, out_fps,
                       ("整数倍 ×%d" % dup) if dup >= 2 else ("目标 %.0ffps" % target_fps)))

            # [2/3] RIFE
            job.check()
            pattern = "%08d" + ext
            _run_rife(job, rife, model, fin, fout, target, pattern,
                      o.get("gpu_id"), ext, P_EXTRACT, P_RIFE)

            n_out = _count_frames(fout, ext)
            if n_out <= 0:
                raise RuntimeError("RIFE 没有产出任何帧：请检查显卡驱动 / Vulkan / 模型目录（%s）" % model)
            if n_out != target:
                job.log("注意：RIFE 产出 %d 帧，与目标 %d 帧不一致（少数情况下正常）" % (n_out, target))
            start = _first_index(fout, ext) or 1

            # [3/3] 合成
            job.check()
            stem = tool.safe_name(os.path.splitext(os.path.basename(src))[0]
                                  + "_rife%sfps" % _fmt_fps(out_fps))
            outpath = tool.unique_out(outdir, stem, ".mp4")
            job.log("输出：%s" % outpath)

            keep_audio = bool(o.get("keep_audio", True)) and bool(info.get("has_audio"))
            if bool(o.get("keep_audio", True)) and not info.get("has_audio"):
                job.log("源视频没有音频轨，直接出无声视频。")
            vargs = _video_args(o)

            def mux_args(acodec):
                a = [tool.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                     "-nostats", "-progress", "pipe:2", "-y",
                     "-framerate", _fmt_fps(out_fps), "-start_number", str(start),
                     "-i", os.path.join(fout, pattern)]
                if keep_audio:
                    a += ["-i", src, "-map", "0:v:0", "-map", "1:a:0?"]
                else:
                    a += ["-map", "0:v:0"]
                a += vargs
                if keep_audio:
                    a += acodec
                # 不搬源片的元数据：源里的 rotation 已被 ffmpeg 摆正，留着会二次旋转
                a += ["-movflags", "+faststart", outpath]
                return a

            try:
                _run_ffmpeg(job, mux_args(["-c:a", "copy"]), dur, P_RIFE, P_ENCODE, "合成")
            except RuntimeError:
                if not keep_audio:
                    raise
                job.log("音频 -c:a copy 失败（源音频可能装不进 mp4），改用 AAC 192k 重编码音频。")
                _run_ffmpeg(job, mux_args(["-c:a", "aac", "-b:a", "192k"]),
                            dur, P_RIFE, P_ENCODE, "合成")

            if not os.path.isfile(outpath) or os.path.getsize(outpath) < 1024:
                raise RuntimeError("合成结束但输出文件异常（不存在或过小）：%s" % outpath)

            job.set_progress(1.0, "完成")
            job.log("完成 → %s（%s）" % (outpath, tool.human_size(os.path.getsize(outpath))))
            return outpath
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            job.log("临时目录已清理：%s" % tmp)
