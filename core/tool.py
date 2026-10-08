# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe 定位与轻封装 —— 全项目唯一的调用入口。"""
import json
import os
import shutil
import subprocess

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def _locate():
    global FFMPEG, FFPROBE
    # 常见安装位置；找不到就退回 PATH（不写死任何个人目录）
    for d in (r"C:\ffmpeg\bin", r"D:\ffmpeg\bin"):
        f = os.path.join(d, "ffmpeg.exe")
        if os.path.exists(f):
            FFMPEG = f
            FFPROBE = os.path.join(d, "ffprobe.exe")
            return
    w = shutil.which("ffmpeg")
    if w:
        FFMPEG = w
        FFPROBE = shutil.which("ffprobe") or "ffprobe"


_locate()


def run(args, timeout=None, cwd=None):
    """跑一条命令，返回 (returncode, stdout, stderr)。"""
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=cwd)
    return p.returncode, p.stdout or "", p.stderr or ""


def popen(args, stdout_pipe=True, stderr_pipe=True, cwd=None,
          creationflags=0, start_new_session=False, env=None):
    """起一个长进程（供需要逐行读进度的场景）。

    creationflags / start_new_session 让子进程独立成组 —— 取消时才能用
    taskkill /T（Windows）或 killpg 把**整棵进程树**带走，否则 ffmpeg 这类
    孙进程会残留。以前没这两个参数，阶段只好绕开本函数直接 Popen。
    """
    kw = {}
    if creationflags:
        kw["creationflags"] = creationflags
    if start_new_session:
        kw["start_new_session"] = True
    if env is not None:
        kw["env"] = env
    return subprocess.Popen(
        args, stdout=subprocess.PIPE if stdout_pipe else None,
        stderr=subprocess.PIPE if stderr_pipe else None,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        cwd=cwd, **kw)


def probe(path):
    """探测视频/音频基本信息。失败返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    rc, out, _ = run([FFPROBE, "-v", "error", "-print_format", "json",
                      "-show_format", "-show_streams", path])
    if rc != 0 or not out.strip():
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return None
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v is None:
        return None
    fmt = d.get("format", {}) or {}

    def _f(x, dflt=0.0):
        try:
            return float(x)
        except (TypeError, ValueError):
            return dflt

    fps = 0.0
    for src in (v.get("avg_frame_rate"), v.get("r_frame_rate")):
        if src and "/" in src:
            n, dd = src.split("/", 1)
            try:
                if float(dd) > 0:
                    fps = float(n) / float(dd)
                    break
            except ValueError:
                pass
    rot = 0
    for sd in (v.get("side_data_list") or []):
        if "rotation" in sd:
            try:
                rot = int(sd["rotation"])
            except (TypeError, ValueError):
                pass
    dovi = None
    for sd in (v.get("side_data_list") or []):
        if sd.get("dv_profile") is not None:
            dovi = sd.get("dv_profile")
    pix = v.get("pix_fmt") or ""
    depth = 10 if "10" in pix else (12 if "12" in pix else 8)
    trc = v.get("color_transfer") or ""
    hdr = bool(dovi is not None
               or trc in ("arib-std-b67", "smpte2084", "smpte428", "bt2020-10", "bt2020-12")
               or ((v.get("color_primaries") or "") == "bt2020" and depth > 8))
    return {
        "path": path,
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 4),
        "duration": _f(fmt.get("duration")),
        "size": int(_f(fmt.get("size"))),
        "codec": v.get("codec_name") or "",
        "pix_fmt": pix,
        "depth": depth,
        "rotation": rot,
        "hdr": hdr,
        "dovi": dovi,
        "transfer": trc,
        "primaries": v.get("color_primaries") or "",
        "has_audio": a is not None,
        "audio_codec": (a or {}).get("codec_name", "") if a else "",
        "nb_frames": int(_f(v.get("nb_frames"))) if v.get("nb_frames") else 0,
    }


def human_size(b):
    b = float(b or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return "%.1f %s" % (b, u)
        b /= 1024.0
    return "%.1f PB" % b


def human_dur(s):
    s = float(s or 0)
    h, m = int(s // 3600), int((s % 3600) // 60)
    return "%d:%02d:%02.2f" % (h, m, s % 60) if h else "%d:%02.2f" % (m, s % 60)


def unique_out(outdir, stem, ext=".mp4"):
    """在 outdir 下找一个不冲突的文件名。"""
    os.makedirs(outdir, exist_ok=True)
    p = os.path.join(outdir, stem + ext)
    i = 2
    while os.path.exists(p):
        p = os.path.join(outdir, "%s_%d%s" % (stem, i, ext))
        i += 1
    return p


def safe_name(s):
    bad = '<>:"/\\|?*'
    s = "".join(("_" if c in bad else c) for c in str(s)).strip()
    return s or "clip"


def has_encoder(name):
    """检查 ffmpeg 是否带某个编码器（如 h264_nvenc）。"""
    rc, out, _ = run([FFMPEG, "-hide_banner", "-encoders"])
    return rc == 0 and name in out
