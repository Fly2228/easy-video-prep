/* charts.js —— 阶段分析图表（目前用于曝光的亮度曲线 / 曝光补偿曲线）
   数据来自 /api/stage_data（阶段自己实现 analyze()），前端只负责画。 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var D = null, KEY = "", OPTS = {}, TIMER = null;

  function post(u, b) {
    return fetch(u, { method: "POST", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify(b || {}) }).then(function (r) { return r.json(); });
  }
  function toast(m, bad) { if (window.EV && EV.toast) EV.toast(m, bad); }
  function fmtT(t) {
    t = Math.max(0, +t || 0);
    var m = Math.floor(t / 60), s = t - m * 60;
    return String(m).padStart(2, "0") + ":" + s.toFixed(0).padStart(2, "0");
  }

  /* ---------------- 载入 ---------------- */
  function load(path, opts, key) {
    KEY = key; OPTS = opts || {};
    if (!path) { render(null, "先选素材"); return; }
    if (TIMER) clearTimeout(TIMER);
    // 参数一直在动，去抖一下，别把后端打爆
    TIMER = setTimeout(function () {
      var box = $("chartsBox");
      if (box) box.classList.add("busy");
      post("/api/stage_data", { stage: KEY, path: path, opts: OPTS }).then(function (r) {
        if (box) box.classList.remove("busy");
        if (!r.ok) { D = null; render(null, r.error); return; }
        D = r.data; render(D, null);
      }).catch(function (e) { render(null, String(e)); });
    }, 350);
  }

  function refresh() { if (D) render(D, null); }

  /* ---------------- 画 ---------------- */
  function poly(pts, w, h, ymin, ymax) {
    if (!pts.length) return "";
    var x = function (t) { return D.duration ? (t / D.duration) * w : 0; };
    var y = function (v) {
      var f = (v - ymin) / Math.max(1e-9, ymax - ymin);
      return h - Math.max(0, Math.min(1, f)) * h;
    };
    return pts.map(function (p, i) { return (i ? "L" : "M") + x(p[0]).toFixed(1) + " " + y(p[1]).toFixed(1); }).join(" ");
  }

  function ticks(w) {
    var h = "";
    for (var i = 0; i <= 10; i++) {
      var x = i * w / 10;
      h += '<line x1="' + x + '" y1="0" x2="' + x + '" y2="120" stroke="#2b323d" stroke-width="0.6"/>' +
           '<text x="' + (x + 3) + '" y="117" fill="#63707f" font-size="10" font-family="monospace">' +
           fmtT(D.duration * i / 10) + '</text>';
    }
    return h;
  }

  function render(d, err) {
    var box = $("chartsBox");
    if (!box) return;
    if (!d) {
      box.innerHTML = '<div class="chempty">' + (err ? ("分析不可用：" + err) :
        "选一个素材后这里会显示亮度曲线与曝光补偿曲线") + "</div>";
      return;
    }
    var W = 1000, H = 120;
    // 亮度曲线：0~255 固定轴
    var luma = poly(d.luma, W, H, 0, 255);
    var ty = H - Math.max(0, Math.min(255, d.target)) / 255 * H;
    // 曝光补偿曲线：以 1.0 为中心，取数据范围的对称区间
    var gs = d.curve.map(function (p) { return p[1]; });
    var span = Math.max(0.15, Math.max.apply(null, gs.concat([1]) ) - Math.min.apply(null, gs.concat([1])));
    var gmin = 1 - span, gmax = 1 + span;
    var g1y = function (v) { return H - Math.max(0, Math.min(1, (v - gmin) / (gmax - gmin))) * H; };
    var curve = poly(d.curve, W, H, gmin, gmax);
    var oneY = g1y(1);

    // 手动控制点
    var man = [], ta = $("f_points");
    if (ta && ta.value) {
      ta.value.split("\n").forEach(function (ln) {
        ln = ln.split("#")[0].trim();
        if (!ln) return;
        var m = ln.split(":");
        if (m.length < 2) return;
        var t = parseFloat(m[0]), g = parseFloat(m[1]);
        if (isFinite(t) && isFinite(g)) man.push([t, g]);
      });
    }
    var dots = man.map(function (p) {
      var x = (D.duration ? p[0] / D.duration : 0) * W;
      return '<circle cx="' + x.toFixed(1) + '" cy="' + g1y(p[1]).toFixed(1) +
             '" r="4" fill="#ffb454" stroke="#12151a" stroke-width="1.5"/>';
    }).join("");

    box.innerHTML =
      '<div class="chart-box"><div class="chart-title"><b>亮度曲线</b>' +
      '<span>Y ' + (d.hdr ? (d.tonemapped ? "（已 tone-map 到 SDR 域）" : "（HDR 原域）") : "平均") + ' · 0–255</span>' +
      '<em>目标亮度 ' + d.target + (d.auto_target ? "（自动）" : "（手填）") + '</em></div>' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" class="ch">' +
      ticks(W) +
      '<line x1="0" y1="' + ty.toFixed(1) + '" x2="' + W + '" y2="' + ty.toFixed(1) +
      '" stroke="#3ddc97" stroke-width="1" stroke-dasharray="5 4" opacity="0.85"/>' +
      '<path d="' + luma + '" fill="none" stroke="#6ea8ff" stroke-width="1.8"/></svg></div>' +

      '<div class="chart-box"><div class="chart-title"><b>曝光补偿曲线</b>' +
      '<span>gamma / EV · 点一下加控制点</span>' +
      '<em>' + (d.curve.length ? ("范围 " + Math.min.apply(null, gs).toFixed(3) + " ~ " + Math.max.apply(null, gs).toFixed(3)) :
        "当前模式（" + (OPTS.mode || "?") + "）不出曲线，切到「自动」可见") + '</em></div>' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" class="ch" id="chCurve">' +
      ticks(W) +
      '<line x1="0" y1="' + oneY.toFixed(1) + '" x2="' + W + '" y2="' + oneY.toFixed(1) +
      '" stroke="#63707f" stroke-width="1" stroke-dasharray="4 4"/>' +
      '<text x="4" y="' + (oneY - 3).toFixed(1) + '" fill="#63707f" font-size="10" font-family="monospace">1.00</text>' +
      (curve ? '<path d="' + curve + '" fill="none" stroke="#3ddc97" stroke-width="1.8"/>' : "") +
      dots + '</svg></div>' +
      '<div class="chfoot">亮度曲线是实际量测（' + d.luma.length + ' 个采样点）' +
      (d.tonemapped ? "；HDR 源已在 tone-map 后的 SDR 域量测，与成品一致" : "") +
      '。在「曝光补偿曲线」上点击会按该时刻的 gamma 生成一个手动控制点。</div>';

    var svg = $("chCurve");
    if (svg && D.duration) {
      svg.style.cursor = "crosshair";
      svg.addEventListener("click", function (ev) {
        var r = svg.getBoundingClientRect();
        var fx = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
        var t = fx * D.duration;
        var g = 1.0;
        if (D.curve.length) {                       // 取最接近该时刻的曲线值
          var best = D.curve[0], bd = 1e9;
          D.curve.forEach(function (p) { var dd = Math.abs(p[0] - t); if (dd < bd) { bd = dd; best = p; } });
          g = best[1];
        }
        var ta = $("f_points");
        if (!ta) { toast("这个阶段没有控制点输入框", true); return; }
        ta.value = (ta.value.trim() ? ta.value.trim() + "\n" : "") + t.toFixed(2) + ":" + g.toFixed(3);
        var m = $("f_mode");
        toast("已加控制点 " + t.toFixed(2) + "s → gamma " + g.toFixed(3) +
              (m && m.value !== "manual" ? "（记得把模式切到「手动」）" : ""));
        refresh();
      });
    }
  }

  window.CH = { load: load, refresh: refresh, has: function () { return !!D; } };
})();
