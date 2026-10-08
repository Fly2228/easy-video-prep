/* zones.js —— 画面框选（打码阶段的"模糊这块 / 忽略这块"）
   在预览画面上直接拖拽画框、拖动移动、拖右下角缩放；
   结果序列化成归一化 JSON 写进当前阶段的 regions 字段。 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var ON = false, FIELD = "", BOXES = [], SEL = -1, drag = null, MZ = "blur";

  function toast(m, bad) { if (window.EV && EV.toast) EV.toast(m, bad); }
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

  // 视频在容器里实际渲染出来的内容区（object-fit:contain 会有黑边）
  function contentBox() {
    var v = $("player");
    if (!v || !v.videoWidth) return null;
    var r = v.getBoundingClientRect();
    var s = Math.min(r.width / v.videoWidth, r.height / v.videoHeight);
    var w = v.videoWidth * s, h = v.videoHeight * s;
    return { left: (r.width - w) / 2, top: (r.height - h) / 2, w: w, h: h };
  }

  function layout() {
    var ov = $("zOverlay");
    if (!ov) return null;
    var b = contentBox();
    if (!b) { ov.style.display = "none"; return null; }
    ov.style.display = "";
    ov.style.left = b.left + "px";
    ov.style.top = b.top + "px";
    ov.style.width = b.w + "px";
    ov.style.height = b.h + "px";
    return b;
  }

  /* ---------------- 读写字段 ---------------- */
  function load() {
    BOXES = []; SEL = -1;
    var ta = $(FIELD);
    if (ta && ta.value.trim()) {
      try {
        var a = JSON.parse(ta.value);
        if (Array.isArray(a)) BOXES = a.filter(function (r) { return r && isFinite(r.x); });
      } catch (e) { /* 手写坏了就当空 */ }
    }
    draw();
  }
  function save() {
    var ta = $(FIELD);
    if (ta) ta.value = BOXES.length ? JSON.stringify(BOXES) : "";
    setMode(MZ);
  }

  /* ---------------- 画 ---------------- */
  function draw() {
    var ov = $("zOverlay");
    if (!ov || !ON) return;
    if (!layout()) return;
    ov.innerHTML = "";
    BOXES.forEach(function (r, i) {
      var d = document.createElement("div");
      d.className = "zbox" + (r.mode === "ignore" ? " ignore" : "") + (i === SEL ? " sel" : "");
      d.style.left = (r.x * 100) + "%";
      d.style.top = (r.y * 100) + "%";
      d.style.width = (r.w * 100) + "%";
      d.style.height = (r.h * 100) + "%";
      var t = document.createElement("span");
      t.className = "ztag";
      t.textContent = (r.mode === "ignore" ? "绝不模糊" : "模糊") + " #" + (i + 1);
      d.appendChild(t);
      var gr = document.createElement("span");
      gr.className = "zgrip";
      d.appendChild(gr);
      d.addEventListener("pointerdown", function (e) { startDrag(e, i, e.target === gr ? "size" : "move"); });
      ov.appendChild(d);
    });
    $("zDel").disabled = SEL < 0;
    // 按钮高亮 = "再画一个框会是什么模式"：
    // 选中了框就显示那个框的模式（此时点另一个按钮 = 改这个框），否则显示默认模式
    setMode(SEL >= 0 && BOXES[SEL] ? BOXES[SEL].mode : MZ);
    var hi = $("zHint");
    if (hi) {
      hi.textContent = SEL >= 0
        ? ("已选中 #" + (SEL + 1) + " — 点另一个按钮就把它改成那个模式")
        : "先选上面的模式，再在画面上拖拽画框；点框可选中改模式或删除";
    }
  }

  function rel(e) {
    var b = $("zOverlay").getBoundingClientRect();
    return { x: clamp((e.clientX - b.left) / Math.max(1, b.width), 0, 1),
             y: clamp((e.clientY - b.top) / Math.max(1, b.height), 0, 1) };
  }

  function startDrag(e, i, kind) {
    if (!ON) return;
    e.preventDefault(); e.stopPropagation();
    SEL = i;
    var p = rel(e), r = BOXES[i];
    drag = { i: i, kind: kind, ox: p.x - r.x, oy: p.y - r.y, sx: r.w, sy: r.h, px: p.x, py: p.y };
    $("zOverlay").setPointerCapture(e.pointerId);
    draw();
  }

  /* ---------------- 绑定 ---------------- */
  function bind() {
    var ov = $("zOverlay");
    if (!ov || ov._b) return;
    ov._b = true;

    ov.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".zbox")) return;
      e.preventDefault();
      var p = rel(e);
      BOXES.push({ x: p.x, y: p.y, w: 0.001, h: 0.001, mode: MZ });
      SEL = BOXES.length - 1;
      drag = { i: SEL, kind: "size", ox: 0, oy: 0, sx: 0.001, sy: 0.001, px: p.x, py: p.y, fresh: true };
      ov.setPointerCapture(e.pointerId);
      draw();
    });

    ov.addEventListener("pointermove", function (e) {
      if (!drag) return;
      var p = rel(e), r = BOXES[drag.i];
      if (!r) return;
      if (drag.kind === "move") {
        r.x = clamp(p.x - drag.ox, 0, 1 - r.w);
        r.y = clamp(p.y - drag.oy, 0, 1 - r.h);
      } else {
        r.x = Math.min(drag.px, p.x);
        r.y = Math.min(drag.py, p.y);
        r.w = Math.max(0.002, Math.abs(p.x - drag.px));
        r.h = Math.max(0.002, Math.abs(p.y - drag.py));
      }
      draw();
    });

    ov.addEventListener("pointerup", function (e) {
      if (!drag) return;
      try { ov.releasePointerCapture(e.pointerId); } catch (x) {}
      var r = BOXES[drag.i], wasFresh = drag.fresh;
      drag = null;
      if (r && (r.w < 0.01 || r.h < 0.01)) {           // 太小的当误触
        BOXES.splice(BOXES.indexOf(r), 1);
        SEL = -1;
      } else if (wasFresh) {
        // 画完就取消选中：这样"模糊/忽略"按钮纯粹决定"下一个框是什么"，
        // 不会因为上一个框还选着而被顺手改掉。要改已有框，先点它再点模式。
        SEL = -1;
      }
      save(); draw();
    });

    Array.prototype.forEach.call(document.querySelectorAll("[data-zmode]"), function (b) {
      b.addEventListener("click", function () {
        MZ = b.dataset.zmode;
        if (SEL >= 0 && BOXES[SEL]) { BOXES[SEL].mode = MZ; save(); }
        else { setMode(MZ); }
        draw();
      });
    });
    if ($("zDel")) $("zDel").onclick = function () {
      if (SEL >= 0) { BOXES.splice(SEL, 1); SEL = -1; save(); draw(); toast("已删除区域"); }
    };
    if ($("zClear")) $("zClear").onclick = function () {
      BOXES = []; SEL = -1; save(); draw(); toast("已清空区域");
    };
    window.addEventListener("resize", function () { if (ON) draw(); });

    // 关键：enable() 时视频往往还没拿到元数据（videoWidth=0），
    // 那时算不出内容区，框选层会被隐藏。等元数据到了必须重绘。
    var v = $("player");
    if (v) {
      ["loadedmetadata", "loadeddata", "canplay", "durationchange"].forEach(function (ev) {
        v.addEventListener(ev, function () { if (ON) draw(); });
      });
    }
  }

  function setMode(m) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-zmode]"), function (b) {
      b.classList.toggle("on", b.dataset.zmode === m);
    });
  }

  /* ---------------- 对外 ---------------- */
  window.ZONES = {
    // 由 shell 在渲染阶段后调用：on=是否启用，fieldId=写回哪个字段
    enable: function (on, fieldId) {
      ON = !!on; FIELD = fieldId || FIELD;
      bind();
      var ov = $("zOverlay"), tb = $("zTools");
      if (ov) { ov.className = "zoverlay" + (ON ? "" : " off"); ov.style.display = ON ? "" : "none"; }
      if (tb) tb.style.display = ON ? "" : "none";
      if (ON) { load(); if (!$("player").paused) $("player").pause(); }
    },
    refresh: function () { if (ON) draw(); },
    redraw: function () { if (ON) draw(); }
  };
})();
