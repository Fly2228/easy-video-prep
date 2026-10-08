/* preview.js —— 常驻预览工作区：播放器 + 胶片时间轴 + 入/出点 + A/B 对比
   独立于具体阶段，任何阶段都能用它看画面、选区间、把区间写进参数。 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var P = { path: "", alt: "", dur: 0, fps: 30, inT: 0, outT: 0, has: false, ab: false, rot: 0, rangeOn: true };
  var applyCb = null, dragMode = null, v = null;

  function fmtT(t) {
    t = Math.max(0, +t || 0);
    var m = Math.floor(t / 60), s = t - m * 60;
    return String(m).padStart(2, "0") + ":" + s.toFixed(2).padStart(5, "0");
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  function base(p) { return String(p || "").split(/[\\/]/).pop(); }

  /* ---------------- 载入 ---------------- */
  function load(path, opts) {
    if (!path) return;
    P.path = path; P.inT = 0; P.outT = 0; P.has = false;
    v = $("player");
    $("pEmpty").style.display = "none";
    $("pBadge").textContent = base(path);
    v.src = "/media?path=" + encodeURIComponent(path);
    v.load();
    // 兜底：即使 /api/probe 失败（文件损坏、ffprobe 认不出），
    // 也要用 <video> 自己报的 duration 把时间轴撑起来，否则整个工作区变砖。
    v.onloadedmetadata = function () {
      if (!P.dur && isFinite(v.duration) && v.duration > 0) P.dur = v.duration;
      renderRuler(); buildStrip(); drawSel(); drawHead();
      v.onloadedmetadata = null;
    };
    v.onerror = function () {
      $("metaHint").textContent = "⚠ 这个文件浏览器解不开（可能不是有效视频或已损坏）";
    };
    fetch("/api/probe?path=" + encodeURIComponent(path))
      .then(function (r) { return r.json(); })
      .then(function (r) {
        if (!r.ok) {
          $("metaHint").textContent = "⚠ ffprobe 解析不了这个文件，时间轴用浏览器读数兜底";
          renderRuler(); buildStrip(); drawSel();
          return;
        }
        P.dur = r.info.duration || 0;
        P.fps = r.info.fps || 30;
        P.rot = r.info.rotation || 0;
        $("metaHint").textContent = r.info.width + "x" + r.info.height + "  " +
          P.fps.toFixed(2) + "fps  " + fmtT(P.dur) +
          (r.info.hdr ? "  HDR(" + (r.info.transfer || "") + ")" : "") +
          (P.rot ? "  旋转" + P.rot + "°" : "");
        renderRuler(); buildStrip(); drawSel();
      });
    if (opts && opts.alt) setAlt(opts.alt);
  }

  function setAlt(path) {
    P.alt = path || "";
    var b = $("abFixed");
    b.disabled = !P.alt;
    b.textContent = P.alt ? ("已导出：" + base(P.alt).slice(0, 18)) : "已导出";
    $("abSwitch").style.display = P.alt ? "" : "none";
  }

  /* ---------------- 时间轴 ---------------- */
  function renderRuler() {
    var r = $("tlRuler"); if (!r) return;
    if (!P.dur) { r.innerHTML = ""; return; }
    var n = 10, h = "";
    for (var i = 0; i <= n; i++) {
      var t = P.dur * i / n;
      h += '<span class="tick" style="left:' + (i * 100 / n) + '%"><i></i><b>' + fmtT(t).replace(/\.\d+$/, "") + "</b></span>";
    }
    r.innerHTML = h;
  }

  function buildStrip() {
    var s = $("tlStrip"); if (!s) return;
    if (!P.dur) { s.innerHTML = ""; return; }
    var n = 14, h = "";
    for (var i = 0; i < n; i++) {
      var t = P.dur * (i + 0.5) / n;
      h += '<img loading="lazy" src="/api/thumb?path=' + encodeURIComponent(P.path) +
           "&t=" + t.toFixed(2) + '&h=64" style="left:' + (i * 100 / n) + "%;width:" + (100 / n + 0.15) + '%">';
    }
    s.innerHTML = h;
  }

  function pct(t) { return P.dur ? Math.max(0, Math.min(100, t / P.dur * 100)) : 0; }

  function drawSel() {
    var a = $("hIn"), b = $("hOut"), s = $("tlSel");
    if (!a) return;
    a.style.left = pct(P.inT) + "%";
    b.style.left = pct(P.outT) + "%";
    if (P.has && P.outT > P.inT && P.rangeOn) {
      s.style.display = "block";
      s.style.left = pct(P.inT) + "%";
      s.style.width = (pct(P.outT) - pct(P.inT)) + "%";
    } else {
      s.style.display = "none";
    }
    $("roIn").textContent = P.has || P.inT ? fmtT(P.inT) : "--:--.--";
    $("roOut").textContent = P.has || P.outT ? fmtT(P.outT) : "--:--.--";
    $("roDur").textContent = (P.has && P.outT > P.inT) ? fmtT(P.outT - P.inT) : "--:--.--";
  }

  function drawHead() {
    var v2 = $("player"), ph = $("playhead");
    if (!ph || !v2) return;
    ph.style.left = pct(v2.currentTime) + "%";
    $("clock").textContent = fmtT(v2.currentTime) + " / " + fmtT(P.dur || v2.duration || 0);
  }

  function tlTime(clientX) {
    var r = $("tlLane").getBoundingClientRect();
    var f = (clientX - r.left) / Math.max(1, r.width);
    return Math.max(0, Math.min(P.dur || 0, f * (P.dur || 0)));
  }

  function seek(t) {
    var v2 = $("player");
    if (!v2) return;
    v2.currentTime = Math.max(0, Math.min(P.dur || v2.duration || 0, t));
    drawHead();
  }

  /* ---------------- 控件 ---------------- */
  function togglePlay() {
    var v2 = $("player");
    if (!v2 || !P.path) return;
    if (v2.paused) { v2.play().catch(function () {}); } else { v2.pause(); }
  }
  function step(frames) {
    var v2 = $("player");
    if (!v2) return;
    v2.pause();
    seek(v2.currentTime + frames / (P.fps || 30));
  }
  function setIn() { P.inT = $("player").currentTime; if (P.outT <= P.inT) P.outT = Math.min(P.dur, P.inT + 1); P.has = true; drawSel(); }
  function setOut() { P.outT = $("player").currentTime; if (P.inT >= P.outT) P.inT = Math.max(0, P.outT - 1); P.has = true; drawSel(); }
  function clearSel() { P.has = false; P.inT = 0; P.outT = 0; drawSel(); }

  function onApply() {
    if (!P.has || P.outT <= P.inT) { toast("先设入点/出点", true); return; }
    if (typeof applyCb === "function") applyCb(P.inT, P.outT);
  }

  var toast = function (m) { if (window.EV && EV.toast) EV.toast(m); };

  /* ---------------- 绑定 ---------------- */
  function bind() {
    var lane = $("tlLane"), v2 = $("player");
    if (!lane || !v2) return;

    v2.addEventListener("timeupdate", drawHead);
    v2.addEventListener("durationchange", function () { if (!P.dur) P.dur = v2.duration || 0; });
    v2.addEventListener("play", function () { $("btnPlay").textContent = "❚❚"; });
    v2.addEventListener("pause", function () { $("btnPlay").textContent = "▶"; });
    v2.addEventListener("volumechange", function () { });

    lane.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".tlhandle")) return;
      dragMode = "seek";
      lane.setPointerCapture(e.pointerId);
      seek(tlTime(e.clientX));
    });
    lane.addEventListener("pointermove", function (e) {
      if (dragMode === "seek") { seek(tlTime(e.clientX)); return; }
      if (dragMode === "in") { P.inT = tlTime(e.clientX); P.has = true; drawSel(); return; }
      if (dragMode === "out") { P.outT = tlTime(e.clientX); P.has = true; drawSel(); }
    });
    lane.addEventListener("pointerup", function (e) {
      dragMode = null; try { lane.releasePointerCapture(e.pointerId); } catch (x) {}
    });
    ["hIn", "hOut"].forEach(function (id) {
      var h = $(id);
      h.addEventListener("pointerdown", function (e) {
        e.stopPropagation(); dragMode = id === "hIn" ? "in" : "out";
        lane.setPointerCapture(e.pointerId);
      });
    });

    $("btnPlay").onclick = togglePlay;
    $("btnPrevFrame").onclick = function () { step(-1); };
    $("btnNextFrame").onclick = function () { step(1); };
    $("btnSetIn").onclick = setIn;
    $("btnSetOut").onclick = setOut;
    $("btnClearSel").onclick = clearSel;
    $("btnAddSeg").onclick = onApply;
    $("pvVol").oninput = function () { v2.volume = +this.value; v2.muted = (+this.value === 0); };
    $("abSwitch").querySelectorAll("button").forEach(function (b) {
      b.onclick = function () {
        var t = v2.currentTime, playing = !v2.paused;
        $("abSwitch").querySelectorAll("button").forEach(function (x) { x.className = ""; });
        b.className = "on";
        P.ab = (b.dataset.src === "fixed");
        v2.src = "/media?path=" + encodeURIComponent(P.ab ? P.alt : P.path);
        v2.load();
        v2.onloadeddata = function () { v2.currentTime = t; if (playing) v2.play().catch(function () {}); v2.onloadeddata = null; };
      };
    });

    document.addEventListener("keydown", function (e) {
      var a = document.activeElement;
      if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT")) return;
      if (!P.path) return;
      // 播放/逐帧对所有阶段都有效
      if (e.code === "Space") { e.preventDefault(); togglePlay(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); step(e.shiftKey ? -(P.fps || 30) : -1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); step(e.shiftKey ? (P.fps || 30) : 1); }
      // 入/出点、应用、清除只对吃时间段的阶段生效
      else if (P.rangeOn && (e.key === "i" || e.key === "I")) { setIn(); }
      else if (P.rangeOn && (e.key === "o" || e.key === "O")) { setOut(); }
      else if (P.rangeOn && e.key === "Enter") { e.preventDefault(); onApply(); }
      else if (P.rangeOn && e.key === "Escape") { clearSel(); }
    });
  }

  // 区间模式：只有真正吃"时间段"的阶段才会露出入/出点手柄与相关按钮，
  // 其它阶段只留一个定位用的播放头 —— 免得把剪辑的操作露给补帧/打码/压缩。
  function setRange(on) {
    P.rangeOn = !!on;
    // 一个 class 管掉按钮/读数/页脚注释，避免漏掉某一项
    var w = document.querySelector(".tlwrap");
    if (w) w.classList.toggle("range-off", !P.rangeOn);
    ["hIn", "hOut"].forEach(function (id) {
      var e = $(id); if (e) e.style.display = P.rangeOn ? "" : "none";
    });
    if (!P.rangeOn) { P.has = false; P.inT = 0; P.outT = 0; }
    drawSel();
    var b = $("btnAddSeg");
    if (b && !P.rangeOn) { b.style.display = "none"; }
  }

  function setApplyLabel(text) {
    var b = $("btnAddSeg");
    if (b) { b.textContent = text || "＋ 应用到参数"; b.disabled = !text; b.style.display = text ? "" : "none"; }
  }

  window.PV = {
    load: load, setAlt: setAlt, seek: seek, togglePlay: togglePlay,
    setRange: setRange, setApplyLabel: setApplyLabel,
    selection: function () { return { in: P.inT, out: P.outT, has: P.has }; },
    onApply: function (cb) { applyCb = cb; },
    clear: clearSel, info: function () { return P; },
    ready: bind
  };
  if (document.readyState !== "loading") bind();
  else document.addEventListener("DOMContentLoaded", bind);
})();
