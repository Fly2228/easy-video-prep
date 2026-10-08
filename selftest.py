# -*- coding: utf-8 -*-
"""easy-video-prep 自检：阶段注册、依赖可用性、以及真实跑一遍。

用法:
    python selftest.py                 # 只做静态检查
    python selftest.py --run trim      # 真实跑指定阶段（用 3 秒小片段）
    python selftest.py --flow          # 真实跑一条 剪辑→压缩 的工作流
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import tool, job as jobmod, state as st      # noqa: E402
from core.stage import all_stages, get as get_stage    # noqa: E402
import stages                                          # noqa: E402,F401

SRC = os.path.join(os.path.dirname(HERE), "video_example", "IMG_7489.MP4")
WORK = os.path.join(HERE, "_out", "_selftest")


def line(c="-"):
    print(c * 68)


def static_check():
    line("=")
    print("阶段注册情况")
    line("=")
    ss = all_stages()
    want = {"exposure", "trim", "rife", "scrub", "compress"}
    got = {s.key for s in ss}
    for s in ss:
        av = s.available()
        mark = "OK " if av.get("ok") else "缺失"
        print("  [%s] %-10s %-8s order=%-4s 字段=%d" % (mark, s.key, s.name, s.order, len(s.schema())))
        if av.get("detail"):
            print("        %s" % av["detail"])
    print()
    print("  已注册: %s" % (", ".join(sorted(got)) or "(无)"))
    miss = want - got
    if miss:
        print("  !! 缺失阶段: %s" % ", ".join(sorted(miss)))
    if stages.LOAD_ERRORS:
        print()
        line("!")
        print("加载失败的阶段模块:")
        for k, tb in stages.LOAD_ERRORS.items():
            print("  --- %s ---" % k)
            print("  " + tb.strip().replace("\n", "\n  "))
    print()
    return not miss and not stages.LOAD_ERRORS


def make_clip(seconds=3.0):
    """从源片切一小段，供真实测试用。"""
    os.makedirs(WORK, exist_ok=True)
    out = os.path.join(WORK, "_src3s.mp4")
    if os.path.exists(out):
        return out
    if not os.path.isfile(SRC):
        print("找不到样片:", SRC)
        return None
    line("=")
    print("准备 3 秒测试片段（-c copy，秒出）")
    rc, _, e = tool.run([tool.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                         "-ss", "30", "-t", str(seconds), "-i", SRC,
                         "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                         "-avoid_negative_ts", "make_zero", out])
    if rc != 0 or not os.path.exists(out):
        print("  切片失败:", e.strip()[:300])
        return None
    print("  ->", out, tool.human_size(os.path.getsize(out)))
    return out


def run_stage(key, src, opts=None):
    stage = get_stage(key)
    o = dict(stage.defaults())
    o["outdir"] = WORK
    o.update(opts or {})
    j = jobmod.create(key, "自检 " + stage.name, [src])
    print()
    line("=")
    print("真实执行阶段: %s   %s" % (stage.name, key))
    line("=")
    t0 = time.time()
    try:
        out = stage.run(j, [src], o)
    except Exception as ex:                       # noqa: BLE001
        print("  !! 失败: %s: %s" % (type(ex).__name__, ex))
        return None
    outs = [out] if isinstance(out, str) else list(out or [])
    line()
    print("  用时 %.1fs" % (time.time() - t0))
    for p in outs:
        ok = os.path.exists(p)
        print("  %s %s  %s" % ("OK " if ok else "!! ", p,
                               tool.human_size(os.path.getsize(p)) if ok else "不存在"))
    return outs


def flow_check(src):
    line("=")
    print("真实执行工作流: 剪辑(无损切 1s) → 压缩(720p)")
    line("=")
    steps = [
        {"stage": "trim", "opts": {"outdir": WORK, "mode": "keep",
                                   "ranges": "0.5-1.5", "exact": False}},
        {"stage": "compress", "opts": {"outdir": WORK, "mode": "quality",
                                       "target_h": 720}},
    ]
    j = jobmod.create("flow", "自检工作流", [src], meta={"steps": steps})
    t0 = time.time()
    try:
        outs = st.run_flow(j, [src], steps)
    except Exception as ex:                       # noqa: BLE001
        print("  !! 失败: %s: %s" % (type(ex).__name__, ex))
        for l in j.logs[-25:]:
            print("     " + l)
        return False
    line()
    print("  用时 %.1fs" % (time.time() - t0))
    for p in outs:
        print("  %s %s  %s" % ("OK " if os.path.exists(p) else "!! ", p,
                               tool.human_size(os.path.getsize(p)) if os.path.exists(p) else ""))
    return all(os.path.exists(p) for p in outs)


def main():
    argv = sys.argv[1:]
    ok = static_check()
    print()
    if "--run" in argv:
        key = argv[argv.index("--run") + 1]
        src = make_clip()
        if src:
            run_stage(key, src)
    if "--flow" in argv:
        src = make_clip()
        if src:
            ok = flow_check(src) and ok
    if not ok:
        sys.exit(2)
    print("\n自检通过")


if __name__ == "__main__":
    main()
