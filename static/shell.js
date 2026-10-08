(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var api = function (u, o) { return fetch(u, o).then(function (r) { return r.json(); }); };
  var post = function (u, b) { return api(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }); };
  var fmtSize = function (b) { b = +b || 0; var u = ["B", "KB", "MB", "GB", "TB"], i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return b.toFixed(i ? 1 : 0) + " " + u[i]; };
  var fmtTime = function (s) { s = +s || 0; var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(Math.floor(s % 60)).padStart(2, "0"); };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); };
  var base = function (p) { return String(p || "").split(/[\\/]/).pop(); };
  window.EV = { toast: null };
  function toast(m, bad) {
    var t = $("toast"); t.textContent = m; t.className = "toast" + (bad ? " bad" : "");
    clearTimeout(t._t); t._t = setTimeout(function () { t.className = "toast hidden"; }, 3400);
  }
  window.EV.toast = toast;

  var S = { state: {}, stages: [], stage: null, inputs: [], opts: {}, job: null, poll: null, flow: [] };

  /* ---------------- 阶段导航 ---------------- */
  function renderRail() {
    var box = $("railList"); box.innerHTML = "";
    S.stages.forEach(function (s, i) {
      var d = document.createElement("div");
      var done = S.state.history && S.state.history.length && S.stage && S.stage.key === s.key && S.job && S.job.status === "done";
      d.className = "st" + (S.stage && S.stage.key === s.key ? " on" : "") + (s.available && s.available.ok === false ? " miss" : "");
      d.innerHTML = '<span class="dot">' + (i + 1) + '</span><span><span class="nm">' + esc(s.icon + " " + s.name) +
        '</span><span class="sub">' + esc(s.available && s.available.ok === false ? "缺少依赖" : (s.desc || "").slice(0, 16)) + '</span></span>';
      d.onclick = function () { openStage(s.key); };
      box.appendChild(d);
    });
    var w = document.createElement("div");
    w.className = "st" + (S.stage && S.stage.key === "__flow" ? " on" : "");
    w.innerHTML = '<span class="dot">★</span><span><span class="nm">工作流编排</span><span class="sub">串起来一次跑完</span></span>';
    w.onclick = function () { openFlow(); };
    box.appendChild(w);
  }

  /* ---------------- 表单渲染 ---------------- */
  function fieldEl(f) {
    var w = document.createElement("div");
    w.className = "fld" + (f.type === "textarea" ? " full" : "");
    var id = "f_" + f.key;
    var lab = '<label for="' + id + '">' + esc(f.label) + '</label>';
    var help = f.help ? '<span class="h">' + esc(f.help) + '</span>' : '';
    var v = S.opts[f.key] !== undefined ? S.opts[f.key] : f.default;
    var h = "";
    if (f.type === "select") {
      h = '<select id="' + id + '">' + f.options.map(function (o) {
        return '<option value="' + esc(o[0]) + '"' + (String(o[0]) === String(v) ? " selected" : "") + '>' + esc(o[1]) + '</option>';
      }).join("") + '</select>';
    } else if (f.type === "checkbox") {
      h = '<label class="sw"><input type="checkbox" id="' + id + '"' + (v ? " checked" : "") + '><i></i></label>';
    } else if (f.type === "range") {
      h = '<div class="row2"><input type="range" id="' + id + '" min="' + f.min + '" max="' + f.max + '" step="' + f.step + '" value="' + v + '"><span class="rv" id="' + id + '_v">' + v + '</span></div>';
    } else if (f.type === "number") {
      h = '<input type="number" id="' + id + '" value="' + (v === null || v === undefined ? "" : v) + '"' +
        (f.min !== undefined ? ' min="' + f.min + '"' : "") + (f.max !== undefined ? ' max="' + f.max + '"' : "") +
        (f.step !== undefined ? ' step="' + f.step + '"' : "") + '>';
    } else if (f.type === "textarea") {
      h = '<textarea id="' + id + '" spellcheck="false">' + esc(v || "") + '</textarea>';
    } else if (f.type === "path") {
      h = '<div class="row2"><input type="text" id="' + id + '" value="' + esc(v || "") + '" spellcheck="false">' +
        '<button class="mini" data-pick="' + id + '" data-kind="' + (f.kind || "file") +
        '" data-ext="' + esc(f.ext || "") + '">…</button></div>';
    } else {
      h = '<input type="text" id="' + id + '" value="' + esc(v === null || v === undefined ? "" : v) + '" spellcheck="false">';
    }
    w.innerHTML = lab + h + help;
    return w;
  }

  function readOpts() {
    var o = {};
    (S.stage.fields || []).forEach(function (f) {
      var e = $("f_" + f.key);
      if (!e) return;
      if (f.type === "checkbox") o[f.key] = e.checked;
      else if (f.type === "number" || f.type === "range") o[f.key] = e.value === "" ? null : Number(e.value);
      else o[f.key] = e.value;
    });
    return o;
  }

  function bindForm() {
    (S.stage.fields || []).forEach(function (f) {
      var e = $("f_" + f.key);
      if (!e) return;
      if (f.type === "range") {
        var rv = $("f_" + f.key + "_v");
        e.oninput = function () { rv.textContent = e.value; };
      }
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-pick]"), function (b) {
      b.onclick = function () {
        var id = b.dataset.pick, kind = b.dataset.kind;
        pickPath(kind, $(id).value, function (p) { $(id).value = p; }, b.dataset.ext);
      };
    });
  }

  /* ---------------- 阶段页 ---------------- */
  function openStage(key) {
    var s = null;
    S.stages.forEach(function (x) { if (x.key === key) s = x; });
    if (!s) return;
    S.stage = s;
    S.opts = Object.assign({}, s.defaults || {});
    if (!S.opts.outdir) S.opts.outdir = S.state.outdir || "";
    renderRail(); renderStage();
  }

  function renderStage() {
    var s = S.stage, box = $("stageView");
    var av = s.available || { ok: true, detail: "" };
    var inputsHtml = S.inputs.length
      ? S.inputs.map(function (p, i) { return '<span class="chip"><b title="' + esc(p) + '">' + esc(base(p)) + '</b><span class="x" data-rm="' + i + '">✕</span></span>'; }).join("")
      : "尚未选择素材";
    var nativeBtn = (s.key === "scrub" && s.native_ui) ? '<a class="btn ghost" href="' + esc(s.native_ui) + '" target="_blank">打开 OpenScrub 原生界面 ↗</a>' : "";
    box.innerHTML =
      '<div class="shead"><span class="ico">' + esc(s.icon) + '</span><div style="flex:1">' +
      '<h2>' + esc(s.name) + ' <span class="badge ' + (av.ok ? "ok" : "no") + '">' + (av.ok ? "就绪" : "缺少依赖") + '</span></h2>' +
      '<p>' + esc(s.desc || "") + '</p>' + (av.detail ? '<p style="color:var(--warn)">' + esc(av.detail) + '</p>' : '') +
      '</div>' + nativeBtn + '</div>' +

      '<div class="card"><h3>① 输入素材<span class="r">' + S.inputs.length + ' 个</span></h3><div class="cardbody">' +
      '<div class="iobox' + (S.inputs.length ? " has" : "") + '">' + inputsHtml + '</div>' +
      '<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">' +
      '<button class="btn ghost" id="btnAddIn">+ 选择视频</button>' +
      '<button class="btn ghost" id="btnUsePrev"' + (S.state.current ? "" : " disabled") + '>↳ 用上一阶段产物</button>' +
      (S.inputs.length ? '<button class="btn ghost" id="btnClearIn">清空</button>' : "") +
      '</div></div></div>' +

      (s.analyze ? '<div class="card"><h3>② 曲线分析<span class="r" id="chartsHint">' +
        (S.inputs.length ? "分析中…" : "选素材后自动分析") + '</span></h3>' +
        '<div class="cardbody"><div id="chartsBox" class="charts"></div></div></div>' : '') +
      '<div class="card"><h3>' + (s.analyze ? "③" : "②") + ' 参数</h3><div class="cardbody"><div class="form" id="formBox"></div></div></div>' +

      '<div class="runbar">' +
      '<button class="btn" id="btnRun"' + (av.ok && S.inputs.length ? "" : " disabled") + '>▶ 执行' + esc(s.name) + '</button>' +
      '<span style="color:var(--fg3);font-size:11.5px" id="runHint">' +
      (av.ok ? (S.inputs.length ? "输出到 " + esc(S.opts.outdir || "(未设)") : "先选素材") : "请先解决依赖问题") + '</span>' +
      '</div>';

    var fb = $("formBox");
    (s.fields || []).forEach(function (f) { fb.appendChild(fieldEl(f)); });
    bindForm();
    applyShowIf(s);
    bindPreview(s);

    Array.prototype.forEach.call(box.querySelectorAll("[data-rm]"), function (x) {
      x.onclick = function () { S.inputs.splice(+x.dataset.rm, 1); renderStage(); };
    });
    if ($("btnAddIn")) $("btnAddIn").onclick = function () {
      pickVideos(S.inputs, function (list) { S.inputs = list; renderStage(); });
    };
    if ($("btnClearIn")) $("btnClearIn").onclick = function () { S.inputs = []; renderStage(); };
    if ($("btnUsePrev")) $("btnUsePrev").onclick = function () {
      if (S.state.current) { S.inputs = [S.state.current]; renderStage(); toast("已接收上游产物"); }
    };
    if ($("btnRun")) $("btnRun").onclick = runStage;
  }

  // ---------------- 素材列表（左栏） ----------------
  function loadList(dir) {
    var d = dir || S.state.root || "";
    if (!d) { return Promise.resolve(); }
    return api("/api/list?dir=" + encodeURIComponent(d)).then(function (r) {
      if (!r.ok) { $("matCount").textContent = "0";
        $("matList").innerHTML = '<div class="matempty">' + esc(r.error || "读不到目录") + "</div>"; return; }
      S.dir = r.dir; S.videos = r.videos || []; S.dirs = r.dirs || [];
      renderMaterials();
    });
  }

  function renderMaterials() {
    var box = $("matList");
    if (!box) return;
    var vs = S.videos || [];
    $("matCount").textContent = vs.length;
    box.innerHTML = "";
    if (!vs.length) {
      box.innerHTML = '<div class="matempty">这个目录里没有视频' +
        ((S.dirs || []).length ? "<br>子目录：" + (S.dirs || []).slice(0, 8).map(esc).join("、") : "") + "</div>";
      return;
    }
    vs.forEach(function (v) {
      var d = document.createElement("div");
      d.className = "mitem" + (S.inputs[0] === v.path ? " on" : "");
      d.title = v.path;
      d.innerHTML = '<span class="mn">' + esc(v.name) + '</span><span class="ms">' + fmtSize(v.size) + "</span>";
      d.onclick = function () {
        S.inputs = [v.path];
        toast("已选 " + v.name + "（" + (S.inputs.length) + " 个）");
        if (S.stage && S.stage.key === "__flow") renderFlow();
        else if (S.stage) renderStage();
        renderMaterials();
      };
      box.appendChild(d);
    });
  }

  // 条件字段：只有 show_if 指定的开关满足时才显示（把"高级选项"收起来）
  function applyShowIf(s) {
    var fs = s.fields || [];
    var upd = function () {
      fs.forEach(function (f) {
        if (!f.show_if) return;
        var el = $("f_" + f.key);
        var wrap = el ? el.closest(".fld") : null;
        if (!wrap) return;
        var ctl = $("f_" + f.show_if.field);
        var on;
        if (!ctl) on = false;
        else if (ctl.type === "checkbox") on = (!!ctl.checked === !!f.show_if.equals);
        else on = (String(ctl.value) === String(f.show_if.equals));
        wrap.style.display = on ? "" : "none";
      });
    };
    fs.forEach(function (f) {
      var ctl = $("f_" + f.key);
      if (!ctl) return;
      var drives = fs.some(function (x) { return x.show_if && x.show_if.field === f.key; });
      if (drives) ctl.addEventListener(ctl.type === "checkbox" ? "change" : "input", upd);
    });
    upd();
  }

  // 把常驻预览接到当前阶段：自动载入素材、按阶段裁剪时间轴、"应用到参数"/快捷剪
  function bindPreview(s) {
    var v = $("player");
    if (v) v.style.filter = "";
    if (S.inputs.length && window.PV) {
      window.PV.load(S.inputs[0], { alt: (S.outputs && S.outputs[0]) || S.state.current });
    }
    if (!window.PV) return;

    // 只有吃时间段的阶段（剪辑/曝光）才露出入出点手柄
    window.PV.setRange(!!s.uses_range);
    if (s.quick) {
      window.PV.setApplyLabel(s.key === "trim" ? "✂ 剪出这一段 (Enter)" : "＋ 应用 (Enter)");
    } else if (s.uses_range) {
      window.PV.setApplyLabel("＋ 应用到参数 (Enter)");
    } else {
      window.PV.setApplyLabel(null);
    }

    window.PV.onApply(function (a, b) {
      // 支持快捷动作的阶段：Enter 直接执行，产物直接进右侧「产物」列表
      if (s.quick) {
        if (!S.inputs.length) return toast("先选素材", true);
        post("/api/quick", { stage: s.key, inputs: S.inputs, a: a, b: b }).then(function (r) {
          if (!r.ok) return toast(r.error || "执行失败", true);
          S.job = r.job; startPoll();
          toast("已开始：" + s.name + " " + a.toFixed(2) + "–" + b.toFixed(2));
        });
        return;
      }
      var line = a.toFixed(2) + "-" + b.toFixed(2);
      if (s.key === "trim") {
        var ta = $("f_segments");
        if (!ta) return toast("找不到时间段输入框", true);
        ta.value = (ta.value.replace(/\s+$/, "") ? ta.value.replace(/\s+$/, "") + "\n" : "") + line;
        ta.scrollTop = ta.scrollHeight;
        toast("已加入片段：" + line);
      } else if (s.key === "exposure") {
        var t = $("f_target");
        if (t) {
          // 拾取参考：把选区中点这一帧的亮度填成目标
          fetch("/api/luma?path=" + encodeURIComponent(S.inputs[0]) + "&t=" + ((a + b) / 2).toFixed(2))
            .then(function (r) { return r.json(); })
            .then(function (r) {
              if (r.ok) { t.value = Math.round(r.luma); toast("参考亮度 = " + Math.round(r.luma) + "（取选区中点）"); }
              else toast("取亮度失败", true);
            });
        } else toast("已记录选区 " + line);
      } else {
        toast("当前阶段不使用区间，选区已记录 " + line);
      }
    });

    // 画面框选：打码阶段（模糊区 / 忽略区）
    if (window.ZONES && window.ZONES.enable) {
      window.ZONES.enable(!!s.uses_zones, "f_regions");
    }

    // 曲线分析：只有实现了 analyze() 的阶段才有（目前是曝光）
    if (s.analyze && window.CH) {
      var rel = ["mode", "stat", "target", "strength", "direction", "smooth", "max_ev", "hdr", "points"];
      window.CH.load(S.inputs.length ? S.inputs[0] : "", readOpts(), s.key);
      var hint = $("chartsHint");
      if (hint) hint.textContent = S.inputs.length ? "" : "选素材后自动分析";
      rel.forEach(function (k) {
        var e = $("f_" + k);
        if (!e) return;
        e.addEventListener(e.type === "checkbox" ? "change" : "input", function () {
          if (window.CH) window.CH.load(S.inputs.length ? S.inputs[0] : "", readOpts(), s.key);
        });
      });
    }

    // 曝光静态参数 → 用 CSS 滤镜做即时预览（不需要后端，改滑杆立刻见效）
    if (s.key === "exposure") {
      var upd = function () {
        var g = $("f_gamma"), br = $("f_brightness"), c = $("f_contrast"), sa = $("f_saturation");
        var f = [];
        if (br) f.push("brightness(" + (1 + Number(br.value)).toFixed(3) + ")");
        if (g && Math.abs(Number(g.value) - 1) > 1e-6)
          f.push("brightness(" + (1 / Math.pow(Number(g.value), 1.4)).toFixed(3) + ")");
        if (c) f.push("contrast(" + Number(c.value).toFixed(3) + ")");
        if (sa) f.push("saturate(" + Number(sa.value).toFixed(3) + ")");
        if (v) v.style.filter = f.join(" ");
      };
      ["f_gamma", "f_brightness", "f_contrast", "f_saturation"].forEach(function (id) {
        var e = $(id); if (e) e.addEventListener("input", upd);
      });
      upd();
    }
  }

  function runStage() {
    if (!S.inputs.length) return toast("先选素材", true);
    S.opts = readOpts();
    var body = { stage: S.stage.key, inputs: S.inputs, opts: S.opts };
    post("/api/run", body).then(function (r) {
      if (!r.ok) return toast(r.error || "启动失败", true);
      S.job = r.job; startPoll(); toast("已开始");
    });
  }

  /* ---------------- 工作流编排 ---------------- */
  function openFlow() {
    S.stage = { key: "__flow", name: "工作流编排", icon: "★", desc: "把阶段排成链，一次跑完；上一步的产物自动喂给下一步", fields: [] };
    renderRail(); renderFlow();
  }

  function renderFlow() {
    var box = $("stageView");
    var steps = S.flow.map(function (st, i) {
      var sd = null; S.stages.forEach(function (x) { if (x.key === st.stage) sd = x; });
      var sum = sd ? (sd.fields || []).slice(0, 3).map(function (f) {
        var v = st.opts[f.key] !== undefined ? st.opts[f.key] : f.default;
        return f.key + "=" + (v === null || v === "" ? "-" : String(v).slice(0, 10));
      }).join("  ") : "";
      return '<div class="fstep"><span class="num">' + (i + 1) + '</span><div><div class="fn">' +
        esc(sd ? sd.icon + " " + sd.name : st.stage) + '</div><div class="fsum">' + esc(sum) + '</div></div>' +
        '<div class="fa"><button class="mini" data-up="' + i + '"' + (i === 0 ? " disabled" : "") + '>↑</button>' +
        '<button class="mini" data-dn="' + i + '"' + (i === S.flow.length - 1 ? " disabled" : "") + '>↓</button>' +
        '<button class="mini" data-del="' + i + '">✕</button></div></div>';
    }).join('<div class="arrow">▼</div>');

    var inputsHtml = S.inputs.length
      ? S.inputs.map(function (p, i) { return '<span class="chip"><b title="' + esc(p) + '">' + esc(base(p)) + '</b><span class="x" data-rm="' + i + '">✕</span></span>'; }).join("")
      : "尚未选择素材";

    box.innerHTML =
      '<div class="shead"><span class="ico">★</span><div><h2>工作流编排</h2>' +
      '<p>把多个阶段串成一条流水线：曝光 → 剪辑 → 补帧 → 打码 → 压缩（顺序随你排）</p></div></div>' +
      '<div class="card"><h3>① 起点素材 <span class="r">' + S.inputs.length + ' 个</span></h3><div class="cardbody">' +
      '<div class="iobox' + (S.inputs.length ? " has" : "") + '">' + inputsHtml + '</div>' +
      '<div style="display:flex;gap:8px;margin-top:10px"><button class="btn ghost" id="btnAddIn">+ 选择视频</button>' +
      '<button class="btn ghost" id="btnUsePrev"' + (S.state.current ? "" : " disabled") + '>↳ 用上一阶段产物</button></div>' +
      '</div></div>' +
      '<div class="card"><h3>② 阶段链 <span class="r">' + S.flow.length + ' 步</span></h3><div class="cardbody">' +
      (S.flow.length ? '<div class="flowlist">' + steps + '</div>' : '<div class="empty">还没有阶段，从下面添加</div>') +
      '<div class="addrow"><select id="flowPick">' + S.stages.map(function (x) {
        return '<option value="' + x.key + '">' + esc(x.icon + " " + x.name) + '</option>';
      }).join("") + '</select><button class="mini" id="btnAddStep">+ 添加阶段</button>' +
      '<button class="btn ghost" id="btnClearFlow">清空链</button></div>' +
      '</div></div>' +
      '<div class="runbar"><button class="btn" id="btnRunFlow"' + (S.inputs.length && S.flow.length ? "" : " disabled") + '>▶ 运行整条工作流</button>' +
      '<span style="color:var(--fg3);font-size:11.5px">上一步产物自动作为下一步输入</span></div>';

    Array.prototype.forEach.call(box.querySelectorAll("[data-rm]"), function (x) {
      x.onclick = function () { S.inputs.splice(+x.dataset.rm, 1); renderFlow(); };
    });
    if ($("btnAddIn")) $("btnAddIn").onclick = function () { pickVideos(S.inputs, function (l) { S.inputs = l; renderFlow(); }); };
    if ($("btnUsePrev")) $("btnUsePrev").onclick = function () { if (S.state.current) { S.inputs = [S.state.current]; renderFlow(); } };
    if ($("btnAddStep")) $("btnAddStep").onclick = function () {
      var k = $("flowPick").value, sd = null;
      S.stages.forEach(function (x) { if (x.key === k) sd = x; });
      S.flow.push({ stage: k, opts: Object.assign({}, (sd && sd.defaults) || {}) });
      renderFlow();
    };
    if ($("btnClearFlow")) $("btnClearFlow").onclick = function () { S.flow = []; renderFlow(); };
    if ($("btnRunFlow")) $("btnRunFlow").onclick = function () {
      post("/api/flow", { inputs: S.inputs, steps: S.flow }).then(function (r) {
        if (!r.ok) return toast(r.error || "启动失败", true);
        S.job = r.job; startPoll(); toast("工作流已开始");
      });
    };
    Array.prototype.forEach.call(box.querySelectorAll("[data-up]"), function (b) {
      b.onclick = function () { var i = +b.dataset.up, t = S.flow[i - 1]; S.flow[i - 1] = S.flow[i]; S.flow[i] = t; renderFlow(); };
    });
    Array.prototype.forEach.call(box.querySelectorAll("[data-dn]"), function (b) {
      b.onclick = function () { var i = +b.dataset.dn, t = S.flow[i + 1]; S.flow[i + 1] = S.flow[i]; S.flow[i] = t; renderFlow(); };
    });
    Array.prototype.forEach.call(box.querySelectorAll("[data-del]"), function (b) {
      b.onclick = function () { S.flow.splice(+b.dataset.del, 1); renderFlow(); };
    });
  }

  /* ---------------- 任务轮询 ---------------- */
  function startPoll() {
    $("btnCancel").disabled = false;
    if (S.poll) clearInterval(S.poll);
    S.poll = setInterval(tick, 700); tick();
  }
  function tick() {
    if (!S.job) return;
    api("/api/job?id=" + encodeURIComponent(S.job.id)).then(function (r) {
      if (!r.ok) return;
      var j = r.job; S.job = j;
      $("progFill").style.width = Math.round((j.progress || 0) * 100) + "%";
      $("progText").textContent = (j.status === "running" ? "运行中 " : j.status === "done" ? "完成 " : j.status + " ") +
        Math.round((j.progress || 0) * 100) + "%  " + (j.step || "") + "  " + (j.elapsed || 0).toFixed(1) + "s";
      $("logBox").textContent = (j.log_tail || []).join("\n") || "—";
      $("logBox").scrollTop = $("logBox").scrollHeight;
      if (j.status === "done" || j.status === "failed" || j.status === "cancelled") {
        clearInterval(S.poll); S.poll = null; $("btnCancel").disabled = true;
        if (j.status === "done") {
          toast("完成：" + (j.outputs || []).length + " 个产物");
          if (window.PV && (j.outputs || []).length && S.inputs.length) {
            window.PV.load(S.inputs[0], { alt: j.outputs[0] });   // 同一个预览里 A/B 对比原片与成品
          }
        }
        else if (j.status === "failed") toast(j.error || "失败", true);
        refreshState(); renderOut();
      }
    });
  }

  function renderOut() {
    var box = $("outList"), h = (S.state.history || []);
    if (!h.length) { box.innerHTML = '<div class="empty">还没有产物</div>'; return; }
    box.innerHTML = h.map(function (p) {
      return '<div class="oitem"><div class="on">' + esc(base(p)) + '</div><div class="om">' + esc(p) + '</div>' +
        '<div class="oa"><button class="mini" data-play="' + esc(p) + '">预览</button>' +
        '<button class="mini" data-use="' + esc(p) + '">设为输入</button>' +
        '<button class="mini" data-folder="' + esc(p) + '">打开目录</button></div></div>';
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll("[data-use]"), function (b) {
      b.onclick = function () { S.inputs = [b.dataset.use]; toast("已设为输入"); if (S.stage && S.stage.key === "__flow") renderFlow(); else if (S.stage) renderStage(); };
    });
    Array.prototype.forEach.call(box.querySelectorAll("[data-play]"), function (b) {
      b.onclick = function () { playFile(b.dataset.play); };
    });
    Array.prototype.forEach.call(box.querySelectorAll("[data-folder]"), function (b) {
      b.onclick = function () {
        // 浏览器禁止 http 页面跳 file://，必须让服务端调资源管理器
        post("/api/openfolder", { path: b.dataset.folder }).then(function (r) {
          if (!r.ok) toast(r.error || "打不开目录", true);
        });
      };
    });
  }

  function playFile(p) {
    var w = window.open("", "_blank");
    if (!w) return toast("浏览器拦截了弹窗", true);
    w.document.write('<!doctype html><title>' + esc(base(p)) + '</title>' +
      '<body style="margin:0;background:#0b0e13;display:grid;place-items:center;height:100vh">' +
      '<video src="/media?path=' + encodeURIComponent(p) + '" controls autoplay style="max-width:100%;max-height:100vh"></video>');
    w.document.close();
  }

  /* ---------------- 文件选择弹窗 ---------------- */
  var PICK = { mode: "one", dir: "", selected: [], cb: null, listCb: null };
  function pickVideos(cur, cb) { PICK.mode = "multi"; PICK.selected = (cur || []).slice(); PICK.listCb = cb; openPick(S.state.root || ""); }
  function pickPath(kind, cur, cb, exts) {
    PICK.mode = kind === "folder" ? "folder" : "one";
    PICK.exts = exts || "";
    PICK.selected = [];
    PICK.cb = cb;
    // cur 可能是"文件"（如模型 .onnx），不能直接当目录去打开 —— 取它所在目录
    openPick(dirOf(cur) || S.state.root || "");
  }

  // 看着像文件（末尾有扩展名）就取所在目录
  function dirOf(p) {
    if (!p) return "";
    return /[^\\/]+\.[A-Za-z0-9]{1,8}$/.test(p) ? p.replace(/[\\/][^\\/]*$/, "") : p;
  }

  function openPick(dir) {
    PICK.dir = dir;
    $("mTitle").textContent = PICK.mode === "multi" ? "选择视频（可多选）" : (PICK.mode === "folder" ? "选择文件夹" : "选择文件");
    $("modal").className = "modal";
    loadPick(dir);
  }
  function closePick() { $("modal").className = "modal hidden"; }

  function loadPick(dir) {
    var q = "/api/list?dir=" + encodeURIComponent(dir) + (PICK.exts ? "&ext=" + encodeURIComponent(PICK.exts) : "");
    api(q).then(function (r) {
      if (!r.ok) {
        // 传进来的其实是个文件 → 退一步打开它所在目录
        var up = dirOf(dir);
        if (up && up !== dir) { loadPick(up); return; }
        return toast(r.error || "读取失败", true);
      }
      PICK.dir = r.dir; PICK.parent = r.parent;
      $("mDir").textContent = r.dir;
      var ds = $("mDirs"); ds.innerHTML = "";
      var up = document.createElement("div");
      up.className = "mrow"; up.innerHTML = '<span class="ic">↑</span><span class="n">上级目录</span>';
      up.onclick = function () { loadPick(r.parent); };
      ds.appendChild(up);
      (r.dirs || []).forEach(function (n) {
        var d = document.createElement("div");
        d.className = "mrow"; d.innerHTML = '<span class="ic">▸</span><span class="n">' + esc(n) + '</span>';
        d.onclick = function () { loadPick(r.dir.replace(/[\\/]$/, "") + "\\" + n); };
        ds.appendChild(d);
      });
      var fs = $("mFiles"); fs.innerHTML = "";
      var all = (r.videos || []).concat(r.files || []);
      if (!all.length) {
        fs.innerHTML = '<div class="empty">' + (PICK.exts ? ("这个目录里没有 " + PICK.exts + " 文件")
          : "这个目录里没有视频") + "</div>";
      }
      all.forEach(function (v) {
        var d = document.createElement("div");
        d.className = "mrow" + (PICK.selected.indexOf(v.path) >= 0 ? " on" : "");
        d.innerHTML = '<span class="ic">■</span><span class="n">' + esc(v.name) + '</span><span class="s">' + fmtSize(v.size) + '</span>';
        d.onclick = function () {
          if (PICK.mode === "multi") {
            var i = PICK.selected.indexOf(v.path);
            if (i >= 0) PICK.selected.splice(i, 1); else PICK.selected.push(v.path);
          } else PICK.selected = [v.path];
          loadPick(r.dir);
        };
        fs.appendChild(d);
      });
      $("mSel").textContent = PICK.mode === "multi" ? ("已选 " + PICK.selected.length + " 个") : "";
    });
  }

  /* ---------------- 状态 ---------------- */
  function refreshState() {
    return api("/api/state").then(function (r) {
      if (!r.ok) return;
      S.state = r.state || {};
      $("rootInput").value = S.state.root || "";
      $("outInput").value = S.state.outdir || "";
      $("curName").textContent = S.state.current ? base(S.state.current) : "未选择";
      $("curName").title = S.state.current || "";
      $("ffmpegHint").textContent = "ffmpeg: " + (r.ffmpeg || "?").split(/[\\/]/).pop();
      renderOut();
    });
  }

  function loadStages() {
    return api("/api/stages").then(function (r) {
      S.stages = r.stages || [];
      renderRail();
      if (!S.stage && S.stages.length) openStage(S.stages[0].key);
    });
  }

  /* ---------------- 绑定 ---------------- */
  function bind() {
    $("btnRoot").onclick = function () { pickPath("folder", $("rootInput").value, function (p) { post("/api/state", { root: p }).then(function () { refreshState(); loadStages(); loadList(p); toast("素材目录已设置"); }); }); };
    $("btnOut").onclick = function () { pickPath("folder", $("outInput").value, function (p) { post("/api/state", { outdir: p }).then(function () { S.opts.outdir = p; refreshState(); if (S.stage) renderStage(); }); }); };
    $("btnPick").onclick = function () { pickVideos(S.inputs, function (l) { S.inputs = l; if (S.stage && S.stage.key === "__flow") renderFlow(); else if (S.stage) renderStage(); }); };
    // 顶栏路径框按 Enter 直接载入
    [["rootInput", "root", "素材目录"], ["outInput", "outdir", "输出目录"]].forEach(function (t) {
      var el = $(t[0]);
      if (!el) return;
      el.addEventListener("keydown", function (e) {
        if (e.key !== "Enter") return;
        e.preventDefault();
        var body = {};
        body[t[1]] = el.value.trim();
        post("/api/state", body).then(function () {
          refreshState();
          if (t[1] === "root") { loadStages(); loadList(el.value.trim()); }
          if (S.stage) renderStage();
          toast(t[2] + "已载入");
        });
      });
    });
    $("btnFlow").onclick = openFlow;
    $("btnCancel").onclick = function () { if (S.job) post("/api/cancel", { id: S.job.id }).then(function () { toast("已请求取消"); }); };
    $("btnClear").onclick = function () { $("logBox").textContent = "—"; };
    $("mClose").onclick = closePick;
    $("mUp").onclick = function () { if (PICK.parent) loadPick(PICK.parent); };
    $("mOk").onclick = function () {
      closePick();
      if (PICK.mode === "multi" && PICK.listCb) PICK.listCb(PICK.selected.slice());
      else if (PICK.cb && PICK.selected.length) PICK.cb(PICK.selected[0]);
    };
    document.querySelectorAll(".sidetabs button").forEach(function (b) {
      b.onclick = function () {
        document.querySelectorAll(".sidetabs button").forEach(function (x) { x.className = ""; });
        b.className = "on";
        $("jobPane").className = "sidebody" + (b.dataset.t === "job" ? "" : " hidden");
        $("outPane").className = "sidebody" + (b.dataset.t === "out" ? "" : " hidden");
        renderOut();
      };
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePick(); });
  }

  bind();
  refreshState().then(loadStages).then(loadList).then(function () {
    api("/api/jobs").then(function (r) {
      var j = (r.jobs || [])[0];
      if (j && (j.status === "running" || j.status === "pending")) { S.job = j; startPoll(); }
    });
  });
})();
