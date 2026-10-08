# -*- coding: utf-8 -*-
"""easy-video-prep —— 统一视频剪辑工作流 WebUI。

一个进程、一套静态资源、五个阶段页面（曝光/剪辑/补帧/打码/压缩），
既可单页独立使用，也可通过 /api/flow 串成工作流。
"""
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import http.server
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import job as jobmod          # noqa: E402
from core import state as st            # noqa: E402
from core import tool                   # noqa: E402
from core.stage import all_stages, get as get_stage, Stage   # noqa: E402
import stages                           # noqa: E402,F401  （导入即完成注册）

STATIC = os.path.join(HERE, "static")
CACHE = os.path.join(HERE, "_cache")
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".ts", ".m2ts", ".webm", ".flv", ".wmv"}


def _ts(t):
    """秒 -> mm:ss.s，用于任务标题。"""
    try:
        t = max(0.0, float(t))
    except (TypeError, ValueError):
        t = 0.0
    return "%02d:%04.1f" % (int(t // 60), t % 60)


def log(*a):
    print("[easy-video-prep]", *a, flush=True)


# ---------------------------------------------------------------- 静态资源
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "easy-video-prep/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---- 基础输出 ------------------------------------------------------
    def jout(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def err(self, msg, code=400):
        return self.jout({"ok": False, "error": str(msg)}, code)

    def bytes_out(self, data, ctype, code=200, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _stamp(self):
        """所有 js/css 的最新 mtime，作为资源版本号。

        拼在 /static/xxx.js?v=... 后面 —— 这样即使浏览器里还留着旧的
        max-age 缓存，URL 变了就会重新取，不用手动清缓存。
        """
        try:
            m = max(os.path.getmtime(os.path.join(STATIC, f))
                    for f in os.listdir(STATIC) if f.endswith((".js", ".css")))
            return "%x" % int(m)
        except (OSError, ValueError):
            return "0"

    def index_html(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
            h = fh.read()
        v = self._stamp()
        # 只允许安全的 URL 字符，避免正则里的引号把字符串截断（踩过）
        h = re.sub(r"(/static/[A-Za-z0-9_./-]+\.(?:js|css))", lambda m: m.group(1) + "?v=" + v, h)
        data = h.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def static(self, rel, cache=False):
        """静态资源一律 no-cache + ETag。

        本地工具不该发长缓存：用了 max-age 之后，改了 CSS/JS 而浏览器仍拿旧文件，
        就会出现"新 HTML + 旧样式"这种半残状态（实测：工作区标记无样式、视频不加载、
        整页无法滚动）。改成每次带 If-None-Match 校验，没变就 304，很便宜。
        """
        p = os.path.normpath(os.path.join(STATIC, rel))
        if not p.startswith(STATIC) or not os.path.isfile(p):
            return self.err("not found", 404)
        ct = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
              ".jpg": "image/jpeg", ".ico": "image/x-icon", ".json": "application/json"}.get(
            os.path.splitext(p)[1].lower(), "application/octet-stream")
        try:
            stt = os.stat(p)
            etag = '"%x-%x"' % (int(stt.st_mtime), stt.st_size)
        except OSError:
            etag = None
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        with open(p, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(data)

    # ---- 视频流（支持 Range，播放器拖拽要用）---------------------------
    def send_media(self, path):
        if not os.path.isfile(path):
            return self.err("文件不存在", 404)
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        ct = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
              ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}.get(ext, "application/octet-stream")
        rng = self.headers.get("Range")
        try:
            start, end = 0, size - 1
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            else:
                self.send_response(200)
            n = max(0, end - start + 1)
            self.send_header("Content-Type", ct)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(n))
            self.end_headers()
            with open(path, "rb") as fh:
                fh.seek(start)
                left = n
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ---- 目录浏览 ------------------------------------------------------
    def list_dir(self, d, exts=None):
        """列目录。exts 非空时，额外返回这些扩展名的文件（模型 .onnx / 报告 .json 等）。

        以前只列视频，导致"选模型"这类按钮点开是空的 —— 选择器需要按用途过滤。
        """
        dirs, vids, files = [], [], []
        want = set(x.lower() for x in (exts or []) if x)
        try:
            for e in sorted(os.scandir(d), key=lambda x: x.name.lower()):
                if e.is_dir(follow_symlinks=False):
                    if not e.name.startswith(".") and e.name not in ("_cache", "_out", "__pycache__"):
                        dirs.append(e.name)
                elif e.is_file():
                    ext = os.path.splitext(e.name)[1].lower()
                    item = {"name": e.name, "path": e.path, "size": e.stat().st_size}
                    if ext in VIDEO_EXTS:
                        vids.append(item)
                    if want and ext in want:
                        files.append(item)
        except OSError as ex:
            raise RuntimeError("无法读取目录: %s" % ex)
        return dirs, vids, files

    def within_root(self, p):
        root = st.STATE.get("root") or ""
        if not root:
            return True
        try:
            return os.path.commonpath([os.path.abspath(p), os.path.abspath(root)]) == os.path.abspath(root)
        except ValueError:
            return False

    # ---- GET -----------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        one = lambda k, d="": (q.get(k) or [d])[0]      # noqa: E731

        if p == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p in ("/", "/index.html"):
            return self.index_html()
        if p.startswith("/static/"):
            return self.static(p[8:])
        if p.startswith("/pages/"):
            return self.static(p[1:])

        if p == "/api/state":
            return self.jout({"ok": True, "state": st.snapshot(),
                              "ffmpeg": tool.FFMPEG, "home": HERE})

        if p == "/api/stages":
            return self.jout({"ok": True, "stages": [s.describe() for s in all_stages()]})

        if p == "/api/list":
            d = one("dir") or st.STATE.get("root") or ""
            if not os.path.isdir(d):
                return self.err("目录不存在：%s" % d)
            exts = [x.strip() for x in (one("ext") or "").replace(";", ",").split(",") if x.strip()]
            try:
                dirs, vids, files = self.list_dir(d, exts)
            except RuntimeError as e:
                return self.err(e)
            parent = os.path.dirname(os.path.abspath(d))
            return self.jout({"ok": True, "dir": d, "parent": parent, "dirs": dirs,
                              "videos": vids, "files": files, "exts": exts})

        if p == "/api/probe":
            f = one("path")
            if not os.path.isfile(f):
                return self.err("文件不存在")
            info = tool.probe(f)
            if not info:
                return self.err("无法解析该文件")
            return self.jout({"ok": True, "info": info})

        if p == "/api/jobs":
            return self.jout({"ok": True, "jobs": jobmod.all_jobs()})

        if p == "/api/job":
            j = jobmod.get(one("id"))
            if not j:
                return self.err("任务不存在", 404)
            return self.jout({"ok": True, "job": j.snapshot()})

        if p == "/media":
            f = one("path")
            if not os.path.isfile(f):
                return self.err("文件不存在", 404)
            return self.send_media(f)

        if p == "/api/luma":
            f, t = one("path"), one("t", "0")
            if not os.path.isfile(f):
                return self.err("文件不存在", 404)
            try:
                tt = float(t)
            except ValueError:
                tt = 0.0
            # rawvideo 是二进制流，不能走 tool.run（文本模式会破坏字节）
            pr = subprocess.run([tool.FFMPEG, "-v", "error", "-ss", "%.3f" % max(0.0, tt),
                                 "-i", f, "-frames:v", "1", "-vf", "scale=160:-2",
                                 "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                capture_output=True)
            if pr.returncode != 0 or not pr.stdout:
                return self.err("取亮度失败: %s" % pr.stderr.decode("utf-8", "replace").strip()[:160])
            px = pr.stdout
            return self.jout({"ok": True, "luma": sum(px) / float(len(px)), "t": tt})

        if p == "/api/thumb":
            f, t = one("path"), one("t", "0")
            if not os.path.isfile(f):
                return self.err("文件不存在", 404)
            try:
                tt = float(t)
            except ValueError:
                tt = 0.0
            h = one("h", "180")
            os.makedirs(CACHE, exist_ok=True)
            key = "%s_%s_%s.jpg" % (re.sub(r"\W+", "_", os.path.basename(f))[:40], tt, h)
            out = os.path.join(CACHE, key)
            if not os.path.exists(out):
                rc, _, e = tool.run([tool.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                                     "-ss", "%.3f" % max(0.0, tt), "-i", f, "-frames:v", "1",
                                     "-vf", "scale=-2:%s" % h, out])
                if rc != 0 or not os.path.exists(out):
                    return self.err("取帧失败: %s" % e.strip()[:200])
            with open(out, "rb") as fh:
                return self.bytes_out(fh.read(), "image/jpeg", cache=True)

        return self.err("未知接口 %s" % p, 404)

    # ---- POST ----------------------------------------------------------
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        b = self._body()

        if p == "/api/state":
            return self.jout({"ok": True, "state": st.update(
                root=b.get("root"), outdir=b.get("outdir"), current=b.get("current"))})

        if p == "/api/pickfolder":
            title = b.get("title") or "选择文件夹"
            initial = b.get("initial") or st.STATE.get("root") or ""
            ps = ("Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
                  "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "$d.Description='%s';" % title.replace("'", "''") +
                  ("$d.SelectedPath='%s';" % initial.replace("'", "''") if initial else "") +
                  "if($d.ShowDialog() -eq 'OK'){Write-Output $d.SelectedPath}")
            rc, out, _ = tool.run(["powershell", "-NoProfile", "-STA", "-Command", ps], timeout=300)
            path = (out or "").strip().splitlines()
            return self.jout({"ok": True, "path": path[-1] if path else ""})

        if p == "/api/run":
            key = b.get("stage")
            try:
                stage = get_stage(key)
            except KeyError as e:
                return self.err(e)
            inputs = [x for x in (b.get("inputs") or []) if x]
            if not inputs:
                return self.err("没有选择输入文件")
            mode = b.get("mode") or "all"          # all=每个输入各跑一次
            opts = dict(stage.defaults())
            opts.update(b.get("opts") or {})
            j = jobmod.create(key, "%s · %d 个文件" % (stage.name, len(inputs)), inputs)

            def work(job):
                outs = []
                for i, f in enumerate(inputs):
                    job.check()
                    job.set_progress(float(i) / len(inputs), step="%d/%d %s" % (i + 1, len(inputs), os.path.basename(f)))
                    o = stage.run(job, [f], opts)
                    for x in ([o] if isinstance(o, str) else (o or [])):
                        outs.append(x)
                        job.add_output(x)
                        st.push_output(x)
                return outs
            return self.jout({"ok": True, "job": jobmod.run(j, work).snapshot(tail=0)})

        if p == "/api/openfolder":
            # 浏览器禁止 http 页面跳 file://，所以"打开目录"必须由服务端调资源管理器
            d = b.get("path") or ""
            if not d:
                return self.err("缺少路径")
            select = ""
            if os.path.isfile(d):
                select = os.path.normpath(d)
                d = os.path.dirname(select)
            if not os.path.isdir(d):
                return self.err("目录不存在: %s" % d)
            try:
                if select:
                    subprocess.Popen(["explorer", "/select,", select])
                else:
                    subprocess.Popen(["explorer", os.path.normpath(d)])
            except OSError as ex:
                return self.err("打不开资源管理器: %s" % ex)
            return self.jout({"ok": True, "dir": d, "selected": bool(select)})

        if p == "/api/stage_data":
            key, f = b.get("stage"), b.get("path")
            if not f or not os.path.isfile(f):
                return self.err("文件不存在")
            try:
                stage = get_stage(key)
            except KeyError as e:
                return self.err(e)
            if type(stage).analyze is Stage.analyze:
                return self.err("「%s」不提供分析数据" % stage.name)
            opts = dict(stage.defaults())
            opts.update(b.get("opts") or {})
            try:
                d = stage.analyze([f], opts)
            except Exception as ex:                     # noqa: BLE001
                return self.err("分析失败: %s" % ex)
            if d is None:
                return self.err("分析没有结果")
            return self.jout({"ok": True, "data": d})

        if p == "/api/quick":
            key = b.get("stage")
            try:
                stage = get_stage(key)
            except KeyError as e:
                return self.err(e)
            inputs = [x for x in (b.get("inputs") or []) if x]
            if not inputs:
                return self.err("没有选择输入文件")
            try:
                a, b2 = float(b.get("a")), float(b.get("b"))
            except (TypeError, ValueError):
                return self.err("区间无效")
            q = stage.quick(inputs, a, b2)
            if not q:
                return self.err("「%s」不支持区间快捷操作" % stage.name)
            opts = dict(stage.defaults())
            opts.update(q)
            j = jobmod.create(key, "%s · %s–%s" % (stage.name, _ts(a), _ts(b2)), inputs)

            def work(job):
                job.log("快捷动作：%s（预览选区 %.2f–%.2f）" % (stage.name, a, b2))
                o = stage.run(job, inputs, opts)
                outs = [o] if isinstance(o, str) else list(o or [])
                for x in outs:
                    job.add_output(x)
                    st.push_output(x)      # 让产物进入"当前素材/产物列表"，才能接着传给下一阶段
                return outs
            return self.jout({"ok": True, "job": jobmod.run(j, work).snapshot(tail=0)})

        if p == "/api/flow":
            inputs = [x for x in (b.get("inputs") or []) if x]
            steps = [s for s in (b.get("steps") or []) if s.get("stage")]
            if not inputs:
                return self.err("没有选择输入文件")
            if not steps:
                return self.err("工作流是空的")
            names = []
            for s in steps:
                try:
                    names.append(get_stage(s["stage"]).name)
                except KeyError:
                    return self.err("未知阶段: %s" % s["stage"])
            j = jobmod.create("flow", "工作流 · " + " → ".join(names), inputs,
                              meta={"steps": steps})
            return self.jout({"ok": True, "job": jobmod.run(j, lambda jb: st.run_flow(jb, inputs, steps)).snapshot(tail=0)})

        if p == "/api/cancel":
            j = jobmod.get(b.get("id"))
            if not j:
                return self.err("任务不存在", 404)
            j.cancel()
            j.log("收到取消请求…")
            return self.jout({"ok": True})

        return self.err("未知接口 %s" % p, 404)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def free_port(preferred=8820):
    for port in range(preferred, preferred + 60):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else ""
    outdir = args[1] if len(args) > 1 else ""
    if root:
        st.update(root=os.path.abspath(root))
    if outdir:
        st.update(outdir=os.path.abspath(outdir))
    elif root:
        st.update(outdir=os.path.join(os.path.abspath(root), "_easy_out"))

    port = free_port()
    srv = Server(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    log("服务已启动:", url)
    log("素材根目录:", st.STATE.get("root") or "(未设置)")
    log("输出目录  :", st.STATE.get("outdir") or "(未设置)")
    log("已注册阶段:", ", ".join("%s(%s)" % (s.key, s.name) for s in all_stages()) or "(无)")
    if "--no-browser" not in sys.argv:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")


if __name__ == "__main__":
    main()
