/* ==========================================================================
   设备编队 · Windows 图形版 W1 的前端逻辑。

   设计约束（和后端 server.py 一套纪律）：
     · 纯原生 JS，不引框架、不引 CDN、不装 npm 包。断网可用。
     · **前端不直接调设备**。所有能力动作都 POST /api/call，由后端走
       registry.call —— 闸门在服务端，前端绕不过去。
     · token 只存在内存里。壳用 URL 片段把它带进来（片段不随 HTTP 请求
       发出，不会进日志），读出来立刻存进变量，**不写 localStorage**。
     · 失败要看得见：后端返回的 err 原样显示，不做「友好兜底」把原因吃掉。

   这个文件只管三件事：把后端的 JSON/SSE 变成界面、把界面动作变成请求、
   以及把「为什么没成」讲清楚。
   ========================================================================== */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ 常量 */
  var TOKEN_HEADER = "X-Fleet-Token";
  var TOKEN = "";            // 运行时注入，内存里
  var SESSIONS = [];         // 本地会话列表（真数据：随对话追加）
  var BUSY = false;          // 有请求在飞时禁掉发送

  /* ------------------------------------------------------------------ 小工具 */
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = String(text); }
    return n;
  }
  function toast(msg, isErr) {
    var t = $("toast");
    t.textContent = msg;
    t.classList.toggle("err", !!isErr);
    t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(function () { t.hidden = true; }, 3600);
  }

  /* ------------------------------------------------------------------ 鉴权 */
  function readTokenFromHash() {
    // 壳注入的形如：#t=<token>&theme=light
    var h = (location.hash || "").replace(/^#/, "");
    var out = { token: "", theme: "" };
    h.split("&").forEach(function (kv) {
      var i = kv.indexOf("=");
      if (i < 0) { return; }
      var k = decodeURIComponent(kv.slice(0, i));
      var v = decodeURIComponent(kv.slice(i + 1));
      if (k === "t") { out.token = v; }
      if (k === "theme") { out.theme = v; }
    });
    return out;
  }

  function requireToken() {
    if (!TOKEN) {
      // 没令牌就直接说清楚为什么，而不是发一个注定 401 的请求。
      toast("缺少访问令牌：请从设备编队启动的窗口里打开本页", true);
      return false;
    }
    return true;
  }

  /* ------------------------------------------------------------------ API */
  function api(path, opts) {
    opts = opts || {};
    var headers = { "X-Fleet-Token": TOKEN };
    if (opts.body) { headers["Content-Type"] = "application/json"; }
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: "no-store"
    }).then(function (r) {
      return r.text().then(function (txt) {
        var data = null;
        if (txt) { try { data = JSON.parse(txt); } catch (e) { data = { ok: false, err: "后端返回的不是 JSON：" + txt.slice(0, 200) }; } }
        if (!r.ok) {
          var why = (data && data.err) ? data.err : ("HTTP " + r.status);
          var e = new Error(why);
          e.status = r.status;
          e.data = data;
          throw e;
        }
        return data;
      });
    });
  }

  /* ------------------------------------------------------------------ 主题 */
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  /* ------------------------------------------------------------------ 消息流 */
  function scrollBottom() {
    var s = $("stream");
    s.scrollTop = s.scrollHeight;
  }

  function addUserMsg(text) {
    var m = el("div", "msg user");
    m.appendChild(el("div", "bubble", text));
    $("streamIn").appendChild(m);
    scrollBottom();
  }

  function addBotMsg() {
    var m = el("div", "msg bot");
    m.appendChild(el("div", "ava", "F"));
    var body = el("div", "body");
    var who = el("div", "who");
    who.appendChild(el("b", null, personaName()));
    who.appendChild(el("span", "pill", "fleet"));
    body.appendChild(who);
    var txt = el("div", "txt");
    body.appendChild(txt);
    var act = el("div", "actbar");
    act.appendChild(el("i", null, "⧉"));
    act.appendChild(el("i", null, "👍"));
    act.appendChild(el("i", null, "👎"));
    act.appendChild(el("i", null, "↻"));
    body.appendChild(act);
    m.appendChild(body);
    $("streamIn").appendChild(m);
    scrollBottom();
    return { root: m, txt: txt, act: act };
  }

  function personaName() {
    var n = $("personaName").textContent;
    if (!n || n === "读取中…" || n === "无角色卡") { return "设备编队"; }
    return n;
  }

  function renderLines(node, lines) {
    node.textContent = "";
    if (!lines.length) { node.appendChild(el("p", null, "（后端没有吐出内容）")); return; }
    lines.forEach(function (ln) {
      node.appendChild(el("p", null, ln));
    });
  }

  /* ------------------------------------------------------------------ 会话列表 */
  function pushSession(title) {
    var s = { id: "s" + (SESSIONS.length + 1), title: title || "新会话" };
    SESSIONS.unshift(s);
    renderSessions();
    return s;
  }

  function renderSessions() {
    var box = $("sessionList");
    box.textContent = "";
    $("sessCount").textContent = "(" + SESSIONS.length + ")";
    if (!SESSIONS.length) {
      box.appendChild(el("div", "sb-conv", "（还没有会话）"));
      return;
    }
    SESSIONS.forEach(function (s, i) {
      var row = el("div", "sb-conv" + (i === 0 ? " on" : ""));
      row.appendChild(el("span", "dot" + (i === 0 ? "" : " off")));
      row.appendChild(el("span", "txt", s.title));
      row.addEventListener("click", function () {
        Array.prototype.forEach.call(box.children, function (c) { c.classList.remove("on"); });
        row.classList.add("on");
      });
      box.appendChild(row);
    });
  }

  /* ------------------------------------------------------------------ 设备列表 */
  function renderDevices(devices) {
    var box = $("deviceList");
    box.textContent = "";
    $("devCount").textContent = "(" + devices.length + ")";
    if (!devices.length) {
      box.appendChild(el("div", "sb-conv", "（没有可用设备）"));
      return;
    }
    devices.forEach(function (d) {
      var row = el("div", "sb-conv");
      var ok = d.available !== false && d.ok !== false;
      row.appendChild(el("span", "dot" + (ok ? "" : " off")));
      row.appendChild(el("span", "txt", d.name || d.kind || "(未命名)"));
      row.title = d.kind + " · " + (d.via || "");
      box.appendChild(row);
    });
  }

  /* ------------------------------------------------------------------ 能力面板 */
  function renderCaps(payload) {
    var body = $("capBody");
    body.textContent = "";
    ASSET_CACHE.caps = payload.caps || [];
    $("capCount").textContent = String(payload.count);
    var domains = payload.domains || {};
    Object.keys(domains).forEach(function (dom) {
      body.appendChild(el("div", "capdom", dom + " · " + domains[dom].length + " 项"));
      (payload.caps || []).filter(function (c) { return c.domain === dom; })
        .forEach(function (c) {
          var row = el("div", "caprow");
          row.appendChild(el("div", "nm", c.name));
          if (c.title) { row.appendChild(el("div", "tt", c.title)); }
          var tags = el("div", "tags");
          tags.appendChild(el("span", "badge", "approval:" + (c.approval || "?")));
          if (c.side_effect) { tags.appendChild(el("span", "badge se", "副作用")); }
          if (c.destructive) { tags.appendChild(el("span", "badge deny", "破坏性")); }
          if (c.devices && c.devices.length) {
            tags.appendChild(el("span", "badge", c.devices.length + " 个设备"));
          }
          row.appendChild(tags);
          // 「执行」按钮：点它 → POST /api/call → 后端走 registry.call。
          // 副作用/ask 档的能力会在闸门上挂起，等主人在审批弹窗里点确认
          // （前端只是把「点了确认」这个信号回传，判定权在注册表层）。
          var go = el("button", "hbtn go", "执行");
          go.addEventListener("click", function () {
            runCap(c.name, {}, (c.devices && c.devices[0]) || "");
          });
          row.appendChild(go);
          body.appendChild(row);
        });
    });
  }

  /* ==================================================================
     X1 · 专家 / 技能 / 连接器（三 Tab）

     语义（2026-10-21 定，取代旧计划书里「连接器 = 设备」的写法）：
       专家     = 角色卡（chara_card_v3）——人格 + 边界
       技能     = SKILL.md——「怎么做」的方法论，自己不动手
       连接器   = MCP server 清单——**设备也是 MCP**，
                  所以内建设备与外接 MCP server 是同一张表，
                  用 source 字段区分（builtin / external）

     🔴 前端零硬编码：能力名、设备名、技能名、连接器名全部来自后端接口。
     🔴 空状态不许装作有：拉不到就显示原因。
     ================================================================== */

  var ASSET_TAB = "fleet";
  var ASSET_CACHE = { fleet: null, cards: null, skills: null, conns: null, caps: null, audio: null };

  function setAssetTab(tab) {
    ASSET_TAB = tab;
    Array.prototype.forEach.call(document.querySelectorAll("#tabBar .tab"), function (b) {
      var on = b.getAttribute("data-tab") === tab;
      b.classList.toggle("on", on);
      // aria-selected 必须跟 .on 同步：只有 class 变了而 ARIA 没变，
      // 屏幕阅读器会一直念「第一个 tab 选中着」——视觉换了、语义没换。
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    loadAssetTab(tab);
  }

  // 页签渲染代号：**只认最后一次加载**，迟到的旧响应一律丢弃。
  // 为什么要它（2026-10-23 装上 playwright 后活线门禁第一次真跑就踩到）：
  // loadAssetTab 只是把 #tabBody 清空重画，在途的上一页请求并不会取消。
  // 于是「点技能页」之后，先前那个更慢的 /api/fleet 响应晚一步回来，
  // 照样往同一个 #tabBody 里画设备面板 —— 表现为
  // 「页签高亮在技能、内容却是设备面板」，或者技能页刚画好又被盖掉。
  // 这不是加载慢，是渲染写错了对象：每单渲染都得先问自己还算不算数。
  var ASSET_SEQ = 0;

  function loadAssetTab(tab) {
    var body = $("tabBody");
    var my = ++ASSET_SEQ;
    function stale() { return my !== ASSET_SEQ; }
    body.textContent = "";
    body.appendChild(el("div", "empty", "读取中…"));

    if (tab === "fleet") {
      // 两个端点并行：总览 + 配对。配对读不到不许拖垮总览——
      // 总览是「现在什么状态」，配对是「怎么让新设备进来」，两件事。
      var pPair = api("/api/pair/devices").catch(function (e) {
        return { ok: false, err: e.message, devices: [] };
      });
      Promise.all([api("/api/fleet"), pPair]).then(function (rs) {
        if (stale()) { return; }
        ASSET_CACHE.fleet = rs[0];
        ASSET_CACHE.pair = rs[1];
        renderFleetTab(rs[0]);
        renderPairBlock(rs[1]);
      }).catch(function (e) {
        if (stale()) { return; }
        renderTabErr(body, "读不到编队总览：" + e.message);
      });
      return;
    }
    if (tab === "cards") {
      api("/api/cardstore").then(function (d) {
        if (stale()) { return; }
        ASSET_CACHE.cards = d;
        renderCardsTab(d);
      }).catch(function (e) {
        if (stale()) { return; }
        renderTabErr(body, "读不到角色卡：" + e.message);
      });
      return;
    }
    if (tab === "skills") {
      // 两个端点并行：/api/skills 是 vendored+用户级（只读清单），
      // /api/skills/evolved 是自写技能（带动作）。后者失败不拖垮前者——
      // 自写区单独显示原因，上面两区照常出（空状态不许装作有，但也不许
      // 一个区读不到就整页白屏）。
      var pEvolved = api("/api/skills/evolved").catch(function (e) {
        return { ok: false, error: e.message, skills: [] };
      });
      Promise.all([api("/api/skills"), pEvolved]).then(function (rs) {
        if (stale()) { return; }
        ASSET_CACHE.skills = rs[0];
        ASSET_CACHE.evolved = rs[1];
        renderSkillsTab(rs[0], rs[1]);
      }).catch(function (e) {
        if (stale()) { return; }
        renderTabErr(body, "读不到技能清单：" + e.message);
      });
      return;
    }
    if (tab === "audio") {
      api("/api/audio/presets").then(function (d) {
        if (stale()) { return; }
        ASSET_CACHE.audio = d;
        renderAudioTab(d);
      }).catch(function (e) {
        if (stale()) { return; }
        renderTabErr(body, "读不到音频预设：" + e.message);
      });
      return;
    }
    api("/api/connectors").then(function (d) {
      if (stale()) { return; }
      ASSET_CACHE.conns = d;
      renderConnsTab(d);
    }).catch(function (e) {
      if (stale()) { return; }
      renderTabErr(body, "读不到连接器清单：" + e.message);
    });
  }

  /* ==================================================================
     X2 · 编队总览

     一句话：把「谁在线 / 有没有活 / 活干成没干成」摊在一屏上，
     不用挨个接口点进去拼。

     口径跟后端保持一致，前端**不重算**：
       · 「成功率」分母是 done + failed，**取消不算失败**——主人主动取消
         不是系统出错，算进去只会天天假红。
       · 一个作业都没跑过时显示「未知」而不是 0%：0% 读起来像「全挂了」，
         真相是「还没数据」，这两件事不能糊在一起。
       · 网关那几条显示的是**上次探测的缓存**，不是此刻的实时结果；
         额度会自己刷新，所以写「等刷新」而不是「不可用」。
     ================================================================== */
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1073741824) { return (n / 1073741824).toFixed(1) + " GB"; }
    if (n >= 1048576) { return (n / 1048576).toFixed(1) + " MB"; }
    if (n >= 1024) { return (n / 1024).toFixed(0) + " KB"; }
    return n + " B";
  }

  function fmtAgo(ts) {
    if (!ts) { return ""; }
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) { return s + " 秒前"; }
    if (s < 3600) { return Math.round(s / 60) + " 分钟前"; }
    if (s < 86400) { return Math.round(s / 3600) + " 小时前"; }
    return Math.round(s / 86400) + " 天前";
  }

  /* ------------------------------------------------------------------ 配对
     主人提的真问题：令牌每次启动都变，手机天天要重抄。
     所以这里不是"再显示一个令牌"，而是"把一次配对变成长期关系"：
     6 位码 → 手机拿去换成设备凭据 → 之后永久有效，电脑重启也不用再配。
     码只显示在这块屏上（屏幕上肉眼抄），HTTP 里没有任何接口吐它。
  */
  function renderPairBlock(p) {
    var body = $("tabBody");
    var sec = el("div", "dp-sec");
    sec.appendChild(el("div", "dp-sech", "配对 · 新设备"));

    if (!p || !p.ok) {
      sec.appendChild(el("div", "dpnote",
        "读不到配对状态：" + ((p && p.err) || "未知原因")));
      body.appendChild(sec);
      return;
    }

    var code = p.code || "";
    var st = p.code_state || {};
    var row = el("div", "pair-row");

    var codeBox = el("div", "pair-code" + (code ? "" : " off"));
    codeBox.textContent = code ? code.slice(0, 3) + " " + code.slice(3) : "······";
    row.appendChild(codeBox);

    var meta = el("div", "pair-meta");
    if (code) {
      var left = Math.max(0, Math.round(st.expires_in || 0));
      meta.appendChild(el("div", "pair-t",
        "还有 " + Math.floor(left / 60) + " 分 " + (left % 60) + " 秒过期"));
      meta.appendChild(el("div", "pair-s", "手机：天网 → 设置 → 配对，输这 6 位"));
    } else if ((st.locked_for || 0) > 0) {
      meta.appendChild(el("div", "pair-t warn",
        "已锁定 " + Math.round(st.locked_for) + " 秒（试错太多）"));
      meta.appendChild(el("div", "pair-s", "重新生成一枚即可解锁"));
    } else {
      meta.appendChild(el("div", "pair-t", "现在没有可用的码"));
      meta.appendChild(el("div", "pair-s", "点右边「生成」"));
    }
    row.appendChild(meta);

    var btn = el("button", "hbtn", code ? "换一枚" : "生成");
    btn.onclick = function () {
      btn.disabled = true;
      api("/api/pair/code", { method: "POST", body: {} })
        .then(loadAssetTab.bind(null, "fleet"))
        .catch(function (e) { alert("生成失败：" + e.message); btn.disabled = false; });
    };
    row.appendChild(btn);
    sec.appendChild(row);

    sec.appendChild(el("div", "dpnote",
      "配对一次就好：手机拿到的是长期设备凭据（存在电脑的 ~/.fleet/devices.json），"
      + "电脑重启也不会失效，以后不用再输码。"));

    var devs = p.devices || [];
    sec.appendChild(el("div", "dp-sech", "已配对设备 · " + devs.length));
    if (!devs.length) {
      sec.appendChild(el("div", "empty", "还没有设备配过对"));
    }
    devs.forEach(function (d) {
      var dr = el("div", "drow");
      dr.appendChild(el("span", "dot-ok"));
      dr.appendChild(el("span", "grow",
        (d.name || "未命名") + (d.platform ? " · " + d.platform : "")
        + " · " + (d.secret_head || "")));
      dr.appendChild(el("span", "badge",
        d.last_seen ? "上次 " + fmtAgo(d.last_seen) : "—"));
      var rv = el("button", "hbtn", "撤销");
      rv.onclick = function () {
        if (!confirm("撤销「" + (d.name || d.id) + "」？它下次请求就会被拒。")) { return; }
        api("/api/pair/devices/revoke", { method: "POST", body: { device_id: d.id } })
          .then(loadAssetTab.bind(null, "fleet"))
          .catch(function (e) { alert("撤销失败：" + e.message); });
      };
      dr.appendChild(rv);
      sec.appendChild(dr);
    });
    body.appendChild(sec);
  }

  function renderFleetTab(d) {
    var body = $("tabBody");
    body.textContent = "";
    if (!d || !d.ok) { renderTabErr(body, (d && d.err) || "后端没给编队数据"); return; }

    var dev = d.devices || { total: 0, online: 0, offline: 0, rows: [] };
    var jobs = d.jobs || {};
    var art = d.artifacts || {};
    var phone = d.phone || {};

    /* ---- 四格指标 ---- */
    var grid = el("div", "fl-grid");
    function kpi(label, value, sub, tone) {
      var c = el("div", "fl-kpi" + (tone ? " " + tone : ""));
      c.appendChild(el("div", "fl-k", label));
      c.appendChild(el("div", "fl-v", String(value)));
      c.appendChild(el("div", "fl-s", sub || ""));
      grid.appendChild(c);
    }
    kpi("设备在线", dev.online + " / " + dev.total,
        dev.offline ? dev.offline + " 台不可用" : "全部可用",
        dev.offline ? "warn" : "good");
    var pending = (jobs.running || 0) + (jobs.queued || 0);
    kpi("在跑 / 排队", (jobs.running || 0) + " / " + (jobs.queued || 0),
        pending ? "共 " + pending + " 个活没结" : "手上没活",
        jobs.stale ? "warn" : "");
    kpi("作业成功率",
        jobs.success_rate === null || jobs.success_rate === undefined
          ? "未知" : Math.round(jobs.success_rate * 100) + "%",
        (jobs.success_rate === null || jobs.success_rate === undefined)
          ? "还没跑过作业" : "成 " + jobs.done + " · 败 " + jobs.failed,
        (jobs.success_rate !== null && jobs.success_rate !== undefined
         && jobs.success_rate < 0.5) ? "bad" : "");
    kpi("产物", art.count || 0,
        fmtBytes(art.bytes) + (art.kinds && Object.keys(art.kinds).length
          ? " · " + Object.keys(art.kinds).join("/") : ""), "");
    body.appendChild(grid);

    /* ---- 手机 ---- */
    var ph = el("div", "dp-sec");
    ph.appendChild(el("div", "dp-sech", "手机"));
    var phRow = el("div", "drow");
    phRow.appendChild(el("span", phone.available ? "dot-ok" : "dot-off"));
    phRow.appendChild(el("span", "grow",
      phone.available
        ? (phone.device || "") + " · " + (phone.brief || "")
        : (phone.note || "没有手机类设备")));
    ph.appendChild(phRow);
    (phone.anomalies || []).forEach(function (a) {
      ph.appendChild(el("div", "dpnote", "⚠ " + a));
    });
    body.appendChild(ph);

    /* ---- 网关：一律显示「上次探测」，并说清额度会自己刷新 ---- */
    if (d.gateways) {
      var gw = el("div", "dp-sec");
      gw.appendChild(el("div", "dp-sech", "本机免费网关（缓存 · 额度会自己刷新）"));
      Object.keys(d.gateways).forEach(function (name) {
        var g = d.gateways[name] || {};
        var row = el("div", "drow");
        row.appendChild(el("span", g.live ? "dot-ok" : "dot-bad"));
        var txt = name + " · 活 " + (g.live || 0) + "/" + (g.total || 0);
        if (g.quota_waiting) {
          txt += " · " + g.quota_waiting + " 条等额度刷新";
          if (g.next_reprobe_in !== null && g.next_reprobe_in !== undefined) {
            txt += "（最快 " + g.next_reprobe_in + "s 后自动重探）";
          }
        }
        if (g.probed_at) { txt += " · 上次探测 " + fmtAgo(g.probed_at); }
        row.appendChild(el("span", "grow", txt));
        gw.appendChild(row);
      });
      body.appendChild(gw);
    }

    /* ---- 提示：出问题的地方必须能一眼看到 ---- */
    (d.notes || []).forEach(function (n) {
      body.appendChild(el("div", "dpnote", "⚠ " + n));
    });

    /* ---- 设备表：离线的排前面（有问题的先看见） ---- */
    var dv = el("div", "dp-sec");
    dv.appendChild(el("div", "dp-sech", "设备 · " + dev.total + " 台"));
    (dev.rows || []).forEach(function (r) {
      var row = el("div", "drow");
      row.appendChild(el("span", r.ok ? "dot-ok" : "dot-off"));
      row.appendChild(el("span", "grow", r.name));
      var tags = el("span", "tags");
      tags.appendChild(el("span", "badge", r.caps + " 项"));
      if (!r.ok && r.reason) {
        var b = el("span", "badge deny", r.reason.slice(0, 28));
        tags.appendChild(b);
      }
      row.appendChild(tags);
      dv.appendChild(row);
    });
    if (!(dev.rows || []).length) { dv.appendChild(el("div", "empty", "注册表里一台设备都没有")); }
    body.appendChild(dv);

    /* ---- 最近作业 ---- */
    var jb = el("div", "dp-sec");
    jb.appendChild(el("div", "dp-sech", "最近作业"));
    var recent = jobs.recent || [];
    if (!recent.length) {
      jb.appendChild(el("div", "empty", "还没提交过作业"));
    } else {
      recent.forEach(function (j) {
        var row = el("div", "drow");
        row.appendChild(el("span", j.state === "done" ? "dot-ok"
          : (j.state === "failed" ? "dot-bad" : "dot-off")));
        row.appendChild(el("span", "grow",
          j.cap + " · " + j.id + (j.node ? " @" + j.node : "")));
        var t = el("span", "tags");
        t.appendChild(el("span", "badge", j.state));
        if (j.attempts > 1) { t.appendChild(el("span", "badge", "试 " + j.attempts + "/" + j.max_attempts)); }
        if (j.artifacts) { t.appendChild(el("span", "badge", j.artifacts + " 产物")); }
        if (j.stale) { t.appendChild(el("span", "badge deny", "租约过期")); }
        row.appendChild(t);
        jb.appendChild(row);
        if (j.err) { jb.appendChild(el("div", "dpnote", j.id + "：" + j.err)); }
      });
    }
    body.appendChild(jb);
  }

  function renderTabErr(body, why) {    body.textContent = "";
    body.appendChild(el("div", "why", why));
  }

  /* ---------------- 专家（角色卡） ----------------
     两段：
       ① 我的卡库 —— cards/ 下已有的卡，**可以随时切换**（切 = 她是谁变了）
       ② 卡库商店 —— 联网搜卡、下载入库（写卡 = 副作用 ⇒ 会过审批闸门）

     🔴 切换不走 /api/call：切卡是**主人自己的决定**，不是「能力调用」，
        不该弹审批框。后端 /api/card/use 也不绕闸门（它改的是 active.json，
        不是外部世界）。但**下载会改 cards/ 目录**，那是往磁盘写东西 ⇒ 走闸门。
  */

  function renderCardsTab(d) {
    var body = $("tabBody");
    body.textContent = "";
    var cards = d.cards || [];

    body.appendChild(el("div", "sec", "我的卡库 · " + cards.length + " 张"));
    if (!cards.length) {
      body.appendChild(el("div", "empty",
        "还没有角色卡。往下从商店下几张，或把卡文件放进 cards/ 目录。"));
    } else {
      var wall = el("div", "wall");
      cards.forEach(function (c) {
        var card = el("div", "card");
        var top = el("div", "ctop");
        top.appendChild(el("span", c.ok ? "dot-ok" : "dot-bad"));
        top.appendChild(el("div", "cname", c.name || c.file));
        if (c.spec) { top.appendChild(el("span", "badge-role", c.spec)); }
        card.appendChild(top);
        if (c.ok) {
          card.appendChild(el("div", "cdesc", "角色卡 " + c.file + "（" + c.spec + " 格式）"));
        } else {
          card.appendChild(el("div", "creason err", c.error || "这张卡读不出来"));
        }
        var meta = el("div", "cmeta");
        meta.appendChild(el("span", "", c.file));
        if (c.ok) {
          var use = el("button", "hbtn go", "切换到这张");
          use.addEventListener("click", function () { useCard(c.file, c.name || c.file); });
          meta.appendChild(use);
        }
        card.appendChild(meta);
        wall.appendChild(card);
      });
      body.appendChild(wall);
    }

    body.appendChild(el("div", "sec", "卡库商店 · 联网搜与下载"));
    body.appendChild(renderStorePanel(d));
  }

  /* 切换角色卡：这是主人自己的决定，不走审批。 */
  function useCard(file, name) {
    if (!requireToken()) { return; }
    api("/api/card/use", { method: "POST", body: { file: file } })
      .then(function (r) {
        if (!r.ok) { toast("切卡失败：" + (r.err || "不知道原因"), true); return; }
        toast("已切到「" + (name || file) + "」");
        ASSET_CACHE.cards = null;
        loadAssetTab("cards");
        if (typeof refreshPersona === "function") { refreshPersona(); }
      })
      .catch(function (e) { toast("切卡失败：" + e.message, true); });
  }

  /* ---------------- 卡库商店面板 ---------------- */

  var STORE_STATE = { source: "aicc", query: "", items: [], total: 0, busy: false };

  function renderStorePanel(d) {
    var box = el("div", "store");

    // ---- 源选择 + 搜索框 ----
    var bar = el("div", "storebar");
    var srcs = d.sources || [];
    if (srcs.length) {
      var sel = el("select", "storesel");
      srcs.forEach(function (s) {
        var o = el("option", "", s.name + (s.note ? "　（" + s.note + "）" : ""));
        o.value = s.id;
        if (s.id === STORE_STATE.source) { o.selected = true; }
        sel.appendChild(o);
      });
      sel.addEventListener("change", function () {
        STORE_STATE.source = sel.value;
        STORE_STATE.query = "";
        doStoreSearch("");
      });
      bar.appendChild(sel);
    } else {
      bar.appendChild(el("div", "why", "没有可用的卡源（注册表里 card.sources 没答上来）"));
    }

    var inp = el("input", "storeinput");
    inp.type = "search";
    inp.placeholder = "搜卡（标题 / 作者 / 标签）· 回车搜索";
    inp.value = STORE_STATE.query;
    inp.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { doStoreSearch(inp.value); }
    });
    bar.appendChild(inp);
    var go = el("button", "hbtn go", "搜索");
    go.addEventListener("click", function () { doStoreSearch(inp.value); });
    bar.appendChild(go);
    box.appendChild(bar);

    // ---- 投递口（手动下载的卡）----
    var inbox = el("div", "storeinbox");
    inbox.appendChild(el("span", "", "手动下载的卡放这里 → " + (d.inbox || "(未指定)")));
    var ing = el("button", "hbtn go", "体检并入库");
    ing.addEventListener("click", doIngest);
    inbox.appendChild(ing);
    box.appendChild(inbox);

    // ---- 结果区 ----
    var res = el("div", "storeres");
    if (STORE_STATE.busy) {
      res.appendChild(el("div", "why", "正在搜…"));
    } else if (STORE_STATE.items.length) {
      res.appendChild(el("div", "why", "共 " + STORE_STATE.total + " 张，显示 "
        + STORE_STATE.items.length + " 张"));
      var wall = el("div", "wall");
      STORE_STATE.items.forEach(function (it) {
        wall.appendChild(storeItem(it));
      });
      res.appendChild(wall);
    } else {
      res.appendChild(el("div", "empty",
        "搜点什么吧。上面输入关键词回车，或直接点「搜索」看全部。"));
    }
    box.appendChild(res);
    return box;
  }

  function storeItem(it) {
    var card = el("div", "card");
    var top = el("div", "ctop");
    top.appendChild(el("span", "dot-off"));
    top.appendChild(el("div", "cname", it.title || "(无名)"));
    if (it.nsfw) { top.appendChild(el("span", "badge deny", "NSFW")); }
    card.appendChild(top);
    if (it.excerpt) { card.appendChild(el("div", "cdesc", it.excerpt)); }
    var meta = el("div", "cmeta");
    if (it.author) { meta.appendChild(el("span", "", "作者 " + it.author)); }
    if (it.token_count) { meta.appendChild(el("span", "", it.token_count + " tok")); }
    (it.tags || []).slice(0, 4).forEach(function (t) {
      meta.appendChild(el("span", "badge-role", t));
    });
    var dl = el("button", "hbtn go", "下载");
    dl.addEventListener("click", function () { doStoreFetch(it, dl); });
    meta.appendChild(dl);
    card.appendChild(meta);
    return card;
  }

  function doStoreSearch(q) {
    if (!requireToken()) { return; }
    STORE_STATE.query = q || "";
    STORE_STATE.busy = true;
    var body = $("tabBody");
    var keep = body.textContent;   // 重绘整段 Tab
    void keep;
    api("/api/cardstore/search?source=" + encodeURIComponent(STORE_STATE.source)
        + "&query=" + encodeURIComponent(STORE_STATE.query) + "&limit=24")
      .then(function (r) {
        STORE_STATE.busy = false;
        if (!r.ok) {
          STORE_STATE.items = [];
          STORE_STATE.total = 0;
          toast("搜卡失败：" + (r.err || "不知道原因"), true);
        } else {
          STORE_STATE.items = r.items || [];
          STORE_STATE.total = r.total || 0;
        }
        loadAssetTab("cards");
      })
      .catch(function (e) {
        STORE_STATE.busy = false;
        STORE_STATE.items = [];
        toast("搜卡失败：" + e.message, true);
        loadAssetTab("cards");
      });
  }

  function doStoreFetch(it, btn) {
    if (!requireToken()) { return; }
    btn.disabled = true;
    btn.textContent = "下载中…";
    // 走 /api/call → registry.call → 副作用能力 ⇒ 会挂起等审批
    // 主人点确认后这次调用才继续，答复回来时卡已经入库了。
    fetchCardCap(it, btn);
  }

  /* 下载一张卡（走能力调用，副作用 ⇒ 要审批）。
     审批通过 ⇒ 后端把卡下到 cards/ ⇒ 刷新专家 Tab。 */
  function fetchCardCap(it, btn) {
    if (!requireToken()) { return; }
    APPR_TAG += 1;
    var tag = "cardfetch-" + APPR_TAG;
    var body = { cap: "card.fetch", device: "cards:store",
                 args: { source: STORE_STATE.source, id: String(it.id) },
                 tag: tag, session: SESSION };
    api("/api/call", { method: "POST", body: body }).then(function (res) {
      showResult(res);
      pollApprovals();
      if (res && res.ok) {
        var data = res.data || {};
        var lint = data.lint || {};
        toast("「" + (it.title || it.id) + "」已入库"
          + (lint.score !== undefined ? "（体检 " + lint.score + " 分）" : "")
          + "　切过去用「切换到这张」");
        ASSET_CACHE.cards = null;
        loadAssetTab("cards");
      } else if (res && res.code === "denied") {
        toast("没下载：" + (res.err || "被拒绝"), true);
      }
      if (btn) { btn.disabled = false; btn.textContent = "下载"; }
    }).catch(function (e) {
      showResult({ ok: false, cap: "card.fetch", code: "error", err: e.message });
      pollApprovals();
      if (btn) { btn.disabled = false; btn.textContent = "下载"; }
    });
  }

  function doIngest() {
    if (!requireToken()) { return; }
    APPR_TAG += 1;
    var tag = "cardingest-" + APPR_TAG;
    var body = { cap: "card.ingest", device: "cards:store", args: {},
                 tag: tag, session: SESSION };
    api("/api/call", { method: "POST", body: body }).then(function (res) {
      showResult(res);
      pollApprovals();
      if (res && res.ok) {
        var items = (res.data && res.data.items) || [];
        var good = items.filter(function (i) { return i.ok; }).length;
        toast("投递口入库完成：" + good + "/" + items.length + " 张通过体检");
        ASSET_CACHE.cards = null;
        loadAssetTab("cards");
      }
    }).catch(function (e) {
      showResult({ ok: false, cap: "card.ingest", code: "error", err: e.message });
      pollApprovals();
    });
  }

  /* ---------------- 技能（SKILL.md） ---------------- */

  function renderSkillsTab(d, ev) {
    var body = $("tabBody");
    body.textContent = "";
    if (d.error) { body.appendChild(el("div", "why", d.error)); }
    var skills = d.skills || [];
    ev = ev || { skills: [] };
    var evolved = ev.skills || [];

    // 按来源分组：仓内技能在前，用户级在后，自写最后（那边带动作）。
    var repo = skills.filter(function (s) { return s.source === "repo"; });
    var user = skills.filter(function (s) { return s.source !== "repo"; });

    body.appendChild(el("div", "sec",
      "技能 · " + skills.length + " 个（SKILL.md） + 自写 " + evolved.length + " 个"));
    if (!skills.length && !evolved.length && !ev.error) {
      body.appendChild(el("div", "empty",
        "还没读到任何技能。技能是带 frontmatter 的 <b>SKILL.md</b> 文件，"
        + "放在仓内 <b>skills/</b>、用户级 <b>~/.workbuddy/skills/</b> 目录下；"
        + "自写技能长在 <b>skills_evolved/</b>（见本页下方）。"));
      return;
    }
    if (repo.length) { body.appendChild(el("div", "sec", "本仓")); body.appendChild(skillWall(repo)); }
    if (user.length) { body.appendChild(el("div", "sec", "用户级")); body.appendChild(skillWall(user)); }
    renderEvolvedSkills(body, ev);
  }

  /* ---------------- 自写技能（skills_evolved/） ----------------
     与上面两区的区别：这边能**动作**。
       选用 —— 打点（note_use）：只证明「有人调用了这张卡」，
               不证明「被照着执行了」，后者本仓观测不到。
       记分 —— skill_stats：使用次数 / 逐能力影子成功率 / 证据充分性。
               「证据不足，分数不可信」是后端原话，照显，不粉饰。
       提交 —— 两步：先预览四闸（不落盘），人看了再确认落盘；
               服务端落盘前**重新过一遍闸**，前端只带 (trace,name,desc)。
       回滚 —— git 快照回到上一个**已提交**版本；自验三样
               （能加载/审计 clean/四闸过）在服务端做，不过就改回原样。
     四个动作全走 requireToken + 服务端裁决，前端不判定任何事。
  */
  function renderEvolvedSkills(body, ev) {
    var list = (ev && ev.skills) || [];
    body.appendChild(el("div", "sec",
      "自写 · " + list.length + " 个（从跑通过的执行里长出来，可提交/回滚）"));
    if (ev && ev.error) {
      body.appendChild(el("div", "why", "自写技能清单读不到：" + ev.error
        + "（上面两区不受影响；自写区在 skills_evolved/，"
        + "读不到通常是目录不存在或里有坏 SKILL.md）"));
    }
    if (!list.length && !(ev && ev.error)) {
      body.appendChild(el("div", "empty",
        "还没有自写技能。用下面这个面板从一条**跑通过的执行链**里长："
        + "填 trace 和名字 → 预览四闸 → 确认才落盘。"));
    }
    body.appendChild(evolvedForm());
    list.forEach(function (s) { body.appendChild(evolvedCard(s)); });
  }

  function evolvedForm() {
    var wrap = el("div", "esform");
    wrap.appendChild(el("div", "esform-t", "从执行链长出新技能（两步：预览四闸 → 确认落盘）"));

    var row1 = el("div", "esrow");
    var fTrace = el("input"); fTrace.id = "esTrace"; fTrace.type = "text";
    fTrace.placeholder = "trace，如 exec:巡检";
    fTrace.setAttribute("aria-label", "执行链 trace");
    var fName = el("input"); fName.id = "esName"; fName.type = "text";
    fName.placeholder = "技能名，如 wifi-reconnect";
    fName.setAttribute("aria-label", "新技能名");
    row1.appendChild(fTrace); row1.appendChild(fName);

    var row2 = el("div", "esrow");
    var fDesc = el("input"); fDesc.id = "esDesc"; fDesc.type = "text";
    fDesc.placeholder = "一句话说明（可空，留空按 trace 自动生成）";
    fDesc.setAttribute("aria-label", "技能说明");
    row2.appendChild(fDesc);

    var row3 = el("div", "esrow");
    var bPrev = el("button", "hbtn go", "预览四闸");
    bPrev.id = "esPreview";
    var bProm = el("button", "hbtn go", "确认落盘");
    bProm.id = "esPromote";
    bProm.disabled = true;
    bProm.title = "先预览，四闸全过才可点";
    row3.appendChild(bPrev); row3.appendChild(bProm);

    wrap.appendChild(row1); wrap.appendChild(row2); wrap.appendChild(row3);

    var gates = el("div", "esgates");
    gates.id = "esGates";
    wrap.appendChild(gates);

    bPrev.addEventListener("click", function () { doProposeSkill(bPrev); });
    bProm.addEventListener("click", function () { doPromoteSkill(bProm); });
    return wrap;
  }

  function showGates(d) {
    var box = $("esGates");
    var bProm = $("esPromote");
    box.textContent = "";
    if (d.err) {
      box.appendChild(el("div", "gate no", "✕ " + d.err));
      bProm.disabled = true;
      return;
    }
    (d.gates || []).forEach(function (g) {
      var row = el("div", "gate " + (g.ok ? "ok" : "no"),
        (g.ok ? "✓ " : "✕ ") + g.gate + " :: " + g.detail);
      box.appendChild(row);
    });
    var caps = el("div", "esnote",
      " caps " + (d.caps || []).length + " 个："
      + (d.caps || []).join("、") + "｜证据 " + (d.evolved_from || []).length + " 条");
    box.appendChild(caps);
    bProm.disabled = !d.ok;
    bProm.textContent = d.ok ? "确认落盘" : "四闸未过，不能落盘";
  }

  function doProposeSkill(btn) {
    if (!requireToken()) { return; }
    var trace = $("esTrace").value.trim();
    var name = $("esName").value.trim();
    var desc = $("esDesc").value.trim();
    if (!trace || !name) { toast("trace 和技能名都要填", true); return; }
    btn.disabled = true;
    btn.textContent = "过闸中…";
    api("/api/skill/propose", {
      method: "POST", body: { trace: trace, name: name, desc: desc }
    }).then(function (d) {
      showGates(d);
      if (d.err) { toast("预览没成：" + d.err, true); }
    }).catch(function (e) {
      toast("预览失败：" + e.message, true);
    }).then(function () {
      btn.disabled = false;
      btn.textContent = "预览四闸";
    });
  }

  function doPromoteSkill(btn, overwrite) {
    if (!requireToken()) { return; }
    var trace = $("esTrace").value.trim();
    var name = $("esName").value.trim();
    var desc = $("esDesc").value.trim();
    if (!trace || !name) { toast("trace 和技能名都要填", true); return; }
    btn.disabled = true;
    btn.textContent = overwrite ? "发布新版本中…" : "落盘中…";
    api("/api/skill/promote", {
      method: "POST",
      body: { trace: trace, name: name, desc: desc, overwrite: !!overwrite }
    }).then(function (d) {
      if (d.ok) {
        // git 提交是「版本=git」的一半：没提交就明说回滚不可用，不装作有快照。
        toast(d.git
          ? ("已落盘并提交 " + String(d.git).slice(0, 8) + "（可回滚）")
          : ("已落盘，但未提交：" + (d.commit_note || "?") + "——暂不能回滚"), !d.git);
        loadAssetTab("skills");
      } else if (d.can_overwrite && !overwrite) {
        // 同名已存在。**不自动覆盖**：覆盖是发布新版本，得人明确点头。
        // （allow_overwrite 只放开同名守卫，四闸服务端照样先跑。）
        btn.disabled = false;
        btn.textContent = "确认落盘";
        if (confirm("「" + name + "」已经存在。\n"
          + "把它发布为**新版本**（覆盖）？\n"
          + "四闸仍会在服务端重跑，不过就拒绝。")) {
          doPromoteSkill(btn, true);
        }
      } else {
        toast("落盘被拒：" + (d.err || "四闸不过"), true);
        showGates(d);
      }
    }).catch(function (e) {
      toast("落盘失败：" + e.message, true);
    }).then(function () {
      btn.disabled = false;
      btn.textContent = "确认落盘";
    });
  }

  function evolvedCard(s) {
    var card = el("div", "card eskill");
    var top = el("div", "ctop");
    top.appendChild(el("span", s.error ? "dot-bad" : "dot-ok"));
    top.appendChild(el("div", "cname", s.name));
    if (s.audit && s.audit !== "?") {
      top.appendChild(el("span", "badge-role " + (s.audit === "clean" ? "" : "err"),
        "审计 " + s.audit));
    }
    card.appendChild(top);
    if (s.error) {
      card.appendChild(el("div", "creason err", s.error));
      return card;
    }
    card.appendChild(el("div", "cdesc", s.description || s.heading || ""));

    var meta = el("div", "cmeta");
    meta.appendChild(el("span", "", "caps " + (s.caps || []).length));
    meta.appendChild(el("span", "", "证据 " + (s.evolved_from || []).length + " 条"));
    var last = s.ledger_last || {};
    meta.appendChild(el("span", "badge-role",
      (last.op || "?") + (last.ts ? " · " + String(last.ts).slice(5, 16) : "")));
    card.appendChild(meta);

    // 记分区（点了才拉：每张卡一次 stats，懒得每次都全量拉）
    var stat = el("div", "sstat");
    stat.id = "stat-" + s.name;
    card.appendChild(stat);

    var ops = el("div", "cops");
    var bOk = el("button", "hbtn go", "选用 ✓");
    var bNo = el("button", "hbtn go", "选用 ✗");
    var bSt = el("button", "hbtn go", "记分");
    var bRb = el("button", "hbtn go danger", "回滚");
    bOk.title = "记一条「这张技能被用了且跑成了」（打点，不是审批）";
    bNo.title = "记一条「用了但没成」——失败也是数据";
    bSt.title = "使用次数 / 逐能力影子成功率 / 证据充分性";
    bRb.title = "回到上一个已提交版本（git 快照）；服务端会重过四闸，不过就改回原样";
    bOk.addEventListener("click", function () { doUseSkill(s.name, true, bOk); });
    bNo.addEventListener("click", function () { doUseSkill(s.name, false, bNo); });
    bSt.addEventListener("click", function () { doSkillStats(s.name, stat, bSt); });
    bRb.addEventListener("click", function () { doRollbackSkill(s.name, bRb); });
    ops.appendChild(bOk); ops.appendChild(bNo); ops.appendChild(bSt); ops.appendChild(bRb);
    card.appendChild(ops);
    return card;
  }

  function doUseSkill(name, ok, btn) {
    if (!requireToken()) { return; }
    btn.disabled = true;
    api("/api/skill/use", {
      method: "POST", body: { name: name, ok: ok }
    }).then(function (d) {
      if (d.ok) {
        toast("已记：#" + d.event_id + " " + name + (ok ? " 用成" : " 没用成"));
      } else {
        toast("记不上：" + (d.err || "?"), true);
      }
    }).catch(function (e) {
      toast("记不上：" + e.message, true);
    }).then(function () { btn.disabled = false; });
  }

  function doSkillStats(name, box, btn) {
    if (!requireToken()) { return; }
    if (box._loaded) { box._loaded = false; box.textContent = ""; btn.textContent = "记分"; return; }
    btn.disabled = true;
    btn.textContent = "算…";
    api("/api/skill/stats?name=" + encodeURIComponent(name)).then(function (d) {
      box.textContent = "";
      if (!d.ok) {
        box.appendChild(el("div", "err", d.err || "算不出"));
        return;
      }
      // 🔴 found=false 必须和「0 次使用」分开说：后端对查无此技能返回
      //    ok=true + found=false（查询成功了，只是没找到）。直接显示
      //    「使用 0 次」会冒充一张存在的技能——那是界面骗人。
      if (d.found === false) {
        box._loaded = true;
        btn.textContent = "收起记分";
        box.appendChild(el("div", "err", "没有这张自写技能：" + name));
        return;
      }
      box._loaded = true;
      btn.textContent = "收起记分";
      box.appendChild(el("div", "",
        "使用 " + d.used + " 次（其中成功 " + d.used_done + "）"));
      (d.shadow || []).forEach(function (r) {
        var rate = r.rate === null || r.rate === undefined
          ? "证据不足" : Math.round(r.rate * 100) + "%";
        box.appendChild(el("div", "srow",
          "  " + r.cap + "（done " + r.done + " / open " + r.open + "）"));
        var v = el("span", r.rate === null || r.rate === undefined ? "" : "srate", rate);
        box.lastChild.appendChild(v);
      });
      // 「证据不足，分数不可信」是后端原话——照显，不许包装成百分比。
      box.appendChild(el("div", "sverdict", d.verdict || ""));
    }).catch(function (e) {
      box.appendChild(el("div", "err", "算不出：" + e.message));
    }).then(function () { btn.disabled = false; });
  }

  function doRollbackSkill(name, btn) {
    if (!requireToken()) { return; }
    // 回滚会改工作区文件，先问一句（界面侧的确认；服务端还有三道自验）。
    if (!confirm("回滚 " + name + " 到上一个已提交版本？\n"
      + "服务端会重过四闸，任何一项不过都会把工作区改回原样。")) {
      return;
    }
    btn.disabled = true;
    btn.textContent = "回滚中…";
    api("/api/skill/rollback", {
      method: "POST", body: { name: name }
    }).then(function (d) {
      if (d.ok) {
        toast("已回滚到 " + String(d.rolled_to || "").slice(0, 8));
        loadAssetTab("skills");
      } else {
        toast("回滚被拒：" + (d.err || "?"), true);
      }
    }).catch(function (e) {
      toast("回滚失败：" + e.message, true);
    }).then(function () { btn.disabled = false; btn.textContent = "回滚"; });
  }

  function skillWall(list) {
    var wall = el("div", "wall");
    list.forEach(function (s) {
      var card = el("div", "card");
      var top = el("div", "ctop");
      top.appendChild(el("span", s.error ? "dot-bad" : "dot-ok"));
      top.appendChild(el("div", "cname", s.name));
      card.appendChild(top);
      if (s.error) {
        card.appendChild(el("div", "creason err", s.error));
      } else {
        card.appendChild(el("div", "cdesc", s.description || s.heading || ""));
      }
      var meta = el("div", "cmeta");
      meta.appendChild(el("span", "", s.file || "SKILL.md"));
      if (s.body_len) { meta.appendChild(el("span", "", s.body_len + " 字符")); }
      if (s.agent_created) { meta.appendChild(el("span", "badge-role", "agent_created")); }
      card.appendChild(meta);
      wall.appendChild(card);
    });
    return wall;
  }

  /* ---------------- 声音（代码生成音频） ----------------
     这里的每个声音都是**代码算出来的波形**，不是采样库回放。
     合成会写 WAV 文件 ⇒ 走 /api/call ⇒ 会弹审批（闸门在服务端，绕不过去）。
  */

  function renderAudioTab(d) {
    var body = $("tabBody");
    body.textContent = "";
    if (d.error) { body.appendChild(el("div", "why", d.error)); }
    var list = d.presets || [];

    body.appendChild(el("div", "sec", "代码生成音频 · " + list.length + " 个预设"));
    body.appendChild(el("div", "why",
      "波形由代码算出来（纯标准库，零第三方依赖，RTF≈0.009）——"
      + "同一个种子逐字节可复现，不同设备用不同种子就有自己的声音。"
      + "这是**功能性声音**（上线/告警/确认），不是人声（人声走「语音」那条线）。"));

    if (!list.length) {
      body.appendChild(el("div", "empty", "没有可用的音频预设。"));
      return;
    }

    var wall = el("div", "wall");
    list.forEach(function (p) {
      var card = el("div", "card");
      var top = el("div", "ctop");
      top.appendChild(el("span", "dot-ok"));
      top.appendChild(el("div", "cname", p.id));
      card.appendChild(top);
      card.appendChild(el("div", "cdesc", p.desc || ""));
      var meta = el("div", "cmeta");
      var seed = el("input", "seedin");
      seed.type = "text";
      seed.value = "fleet";
      seed.title = "种子：决定音色。相同种子 = 同一条声音（重启后也一样）；换种子 = 换音色";
      seed.setAttribute("aria-label", "合成种子");
      var btn = el("button", "hbtn go", "合成");
      btn.addEventListener("click", function () { doSynth(p, btn, seed.value); });
      meta.appendChild(seed);
      meta.appendChild(btn);
      meta.appendChild(el("span", "", p.id));
      card.appendChild(meta);
      wall.appendChild(card);
    });
    body.appendChild(wall);
  }

  function doSynth(p, btn, seed) {
    if (!requireToken()) { return; }
    APPR_TAG += 1;
    var tag = "audio-" + p.id + "-" + APPR_TAG;
    btn.disabled = true;
    btn.textContent = "合成中…";
    api("/api/audio/synth", {
      method: "POST",
      // 🔴 seed 从卡片输入框来（默认 fleet）。空种子回落到 "fleet"，
      //    后端按 spec+seed 命名文件：同种子覆盖同路径（可复现），换种子换新文件。
      body: { spec: p.id, seed: (seed || "fleet").trim() || "fleet",
              tag: tag, session: SESSION }
    }).then(function (res) {
      showResult(res);
      pollApprovals();
      if (res && res.ok) {
        var d = res.data || {};
        toast("「" + p.id + "」合成好了：" + (d.seconds || "?") + " 秒 / "
          + (d.bytes || 0) + " 字节 → " + (d.path || ""));
      } else {
        // 🔴 不只判 denied：超时、闸门拒绝、能力报 error 都要显示原因。
        // 失败分支后端现在带 code/err（完整结果信封），所以这里取 res.err；
        // 兜底给一句明话，不许显示空字符串（静默失败最阴）。
        toast("没合成：" + ((res && res.err) || "原因未返回"), true);
      }
      btn.disabled = false;
      btn.textContent = "合成";
    }).catch(function (e) {
      showResult({ ok: false, cap: "audio.synth", code: "error", err: e.message });
      pollApprovals();
      btn.disabled = false;
      btn.textContent = "合成";
    });
  }

  /* ---------------- 连接器（MCP server 清单） ---------------- */

  function renderConnsTab(d) {
    var body = $("tabBody");
    body.textContent = "";
    if (d.error) { body.appendChild(el("div", "why", d.error)); }
    if (d.note) { body.appendChild(el("div", "why", d.note)); }

    var rows = d.connectors || [];
    var builtin = rows.filter(function (r) { return r.source === "builtin"; });
    var external = rows.filter(function (r) { return r.source === "external"; });

    body.appendChild(el("div", "sec",
      "连接器 · " + rows.length + " 个（内建 " + (d.builtin || builtin.length)
      + " / 外接 " + (d.external || external.length) + "）"));
    body.appendChild(el("div", "why",
      "设备也是 MCP：内建每一台设备都是一个 MCP server，"
      + "外接的是你自己挂的第三方 MCP server。它们在协议层是同一件事，"
      + "所以放在同一张表里。"));

    if (builtin.length) {
      body.appendChild(el("div", "sec", "内建（本仓设备）"));
      body.appendChild(connWall(builtin));
    }
    if (external.length) {
      body.appendChild(el("div", "sec", "外接（第三方 MCP server）"));
      body.appendChild(connWall(external));
    }
  }

  function connWall(list) {
    var wall = el("div", "wall");
    list.forEach(function (r) {
      var card = el("div", "card");
      var top = el("div", "ctop");
      top.appendChild(el("span", r.ok ? "dot-ok" : (r.reason ? "dot-bad" : "dot-off")));
      top.appendChild(el("div", "cname", r.name));
      top.appendChild(el("span", "badge-src" + (r.source === "external" ? " ext" : ""),
        r.source === "external" ? "external" : "builtin"));
      card.appendChild(top);
      var meta = el("div", "cmeta");
      if (r.kind) { meta.appendChild(el("span", "", r.kind)); }
      if (r.via) { meta.appendChild(el("span", "", r.via)); }
      meta.appendChild(el("span", "", r.tools + " 个只读工具"));
      card.appendChild(meta);
      if (r.reason) {
        card.appendChild(el("div", "creason" + (r.ok ? "" : " err"), r.reason));
      }
      wall.appendChild(card);
    });
    return wall;
  }

  /* ------------------------------------------------------------------ 聊天（SSE） */
  // curBot：当前正在灌流的那个 bot 气泡（同时只允许一个流，串行）。
  // 声明放在使用之前，免得读代码时要在文件里上上下下找。
  var curBot = null;

  function sendChat(prompt) {
    if (BUSY) { return; }
    if (!requireToken()) { return; }
    BUSY = true;
    $("sendBtn").disabled = true;
    $("sendBtn").textContent = "生成中…";

    addUserMsg(prompt);
    pushSession(prompt.slice(0, 18));
    var bot = addBotMsg();
    bot.lines = [];
    curBot = bot;
    renderLines(bot.txt, bot.lines);

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fleet-Token": TOKEN },
      body: JSON.stringify({ prompt: prompt }),
      cache: "no-store"
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (txt) {
          var why = txt;
          try { why = JSON.parse(txt).err || txt; } catch (e) { /* 原样带出 */ }
          throw new Error(why || ("HTTP " + r.status));
        });
      }
      // 手动读流：EventSource 只能 GET，这里是 POST，所以自己解 SSE 帧。
      var reader = r.body.getReader();
      var dec = new TextDecoder("utf-8");
      var buf = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) { return; }
          buf += dec.decode(chunk.value, { stream: true });
          var parts = buf.split("\n\n");
          buf = parts.pop();
          parts.forEach(handleFrame);
          scrollBottom();
          return pump();
        });
      }
      return pump().then(function () { handleFrame(buf); });
    }).then(function () {
      renderLines(bot.txt, bot.lines);
    }).catch(function (e) {
      bot.txt.textContent = "";
      bot.txt.appendChild(el("p", "err", "对话失败：" + e.message));
      toast("对话失败：" + e.message, true);
    }).then(function () {
      BUSY = false;
      curBot = null;
      $("sendBtn").disabled = !$("input").value.trim();
      $("sendBtn").textContent = "发送";
    });
  }

  function handleFrame(frame) {
    if (!frame) { return; }
    frame.split("\n").forEach(function (line) {
      if (line.indexOf("data:") !== 0) { return; }
      var payload = line.slice(5).trim();
      if (!payload) { return; }
      var ev;
      try { ev = JSON.parse(payload); } catch (e) { return; }
      if (ev.type === "line") {
        if (curBot) { curBot.lines.push(ev.text); renderLines(curBot.txt, curBot.lines); }
      } else if (ev.type === "web") {
        // 联网进度。搜索十几秒，不提示的话界面上就是一句话卡着不动。
        var wd = ev.data || {};
        if (wd.phase === "searching") {
          toast("正在联网查…");
        } else if (wd.used) {
          var n = (wd.sources && wd.sources.length) || 0;
          toast("已联网 · 引用了 " + n + " 个来源");
        } else if (wd.reason) {
          toast(wd.reason);
        }
      } else if (ev.type === "error") {
        toast(ev.err || "后端报了错但没给原因", true);
      } else if (ev.type === "done") {
        showResult(ev.result);
      }
    });
  }

  // handleFrame 需要知道当前是哪一个 bot 气泡：curBot 就是它（见上面的声明）。

  /* ------------------------------------------------------------------ 结果条 */
  function showResult(res) {
    if (!res) { return; }
    var head = res.ok ? "✓" : "✕";
    var txt = head + " " + (res.cap || "") + " · " + (res.code || "") +
      " · " + (res.ms !== undefined ? res.ms + "ms" : "");
    if (res.err) { txt += " · " + res.err; }
    if (res.via) { txt += " · via " + res.via; }
    // 🔴 更新所有结果条（聊天视图的 #resBar 与资产视图的 #resBarAssets）。
    // 每条所在视图不可见时是 display:none，那条即使 unhide 也看不见，
    // 所以「不重复显示」靠父容器可见性自动解决，这里统一写就行。
    var bars = document.querySelectorAll(".resbar");
    for (var i = 0; i < bars.length; i++) {
      var b = bars[i];
      b.hidden = false;
      b.classList.toggle("err", !res.ok);
      b.textContent = txt;
    }
  }

  /* ------------------------------------------------------------------ 审批（W2） */
  // 这一段只做一件事：把「主人点了确认/拒绝」这个信号回传给后端。
  //
  // **前端不判定要不要审批**——那是注册表层的活（side_effect / approval=ask
  // / destructive）。前端连「这项能力危不危险」都不自己算：
  // 后端把待审批清单推过来，前端原样显示，点确认就回传 request_id。
  // 这样换个前端、手敲一条 HTTP，也绕不过「要审批」的判定。
  var SESSION = "";        // 本页面会话标识（只在内存里；刷新即换）
  var APPR_TAG = 0;        // 自增标签：把待审批认领回自己发起的那次调用
  var APPR_STEP = {};      // request_id -> 破坏性操作已点过第一次确认
  var APPR_TIMER = null;   // 轮询待审批清单的定时器
  var APPR_ROWS = [];      // 最近一次拉到的待审批清单

  function newSession() {
    // 只是「这次页面会话」的标识，用来拒绝别的会话来回应我们的审批。
    // 它不是凭据（凭据是 X-Fleet-Token），也不落盘。
    return "ui-" + Math.random().toString(36).slice(2) +
      "-" + Date.now().toString(36);
  }

  function renderApprovals(payload) {
    var rows = (payload && payload.pending) || [];
    APPR_ROWS = rows;
    var list = $("apprList");
    var flag = $("apprFlag");
    var n = rows.length;
    $("apprCount").textContent = String(n);
    $("apprCount2").textContent = String(n);
    flag.hidden = n === 0;
    $("apprMask").hidden = n === 0;
    list.textContent = "";
    if (!n) {
      list.appendChild(el("div", "apprnone", "（没有能力在等审批）"));
      return;
    }
    rows.forEach(function (r) {
      var row = el("div", "apprrow");
      var head = el("div", "ah");
      head.appendChild(el("span", "nm", r.cap || "?"));
      if (r.title) { head.appendChild(el("span", "tt", r.title)); }
      if (r.level >= 2) { head.appendChild(el("span", "badge deny", "二级审批")); }
      if (r.destructive) { head.appendChild(el("span", "badge deny", "不可逆")); }
      if (r.device) { head.appendChild(el("span", "badge", r.device)); }
      if (r.session && r.session !== SESSION) {
        head.appendChild(el("span", "badge warn", "接管"));
      }
      row.appendChild(head);
      if (r.args_preview) { row.appendChild(el("div", "ap", r.args_preview)); }
      row.appendChild(el("div", "ap",
        "剩余 " + Math.max(0, Math.round(r.expires_in || 0)) + " 秒"));

      var acts = el("div", "acts");
      var no = el("button", "hbtn no", "拒绝");
      no.addEventListener("click", function () { respondApproval(r, false); });
      var ok = el("button", "hbtn yes",
        (r.destructive && !APPR_STEP[r.request_id]) ? "确认（不可逆）" : "确认执行");
      ok.addEventListener("click", function () { respondApproval(r, true); });
      acts.appendChild(no);
      acts.appendChild(ok);
      row.appendChild(acts);
      list.appendChild(row);
    });
    renderApprovalHistory((payload && payload.history) || []);
  }

  function respondApproval(r, approve) {
    if (!requireToken()) { return; }
    var double = false;
    // 这条审批不是本页面会话发起的（比如另一个窗口/页面发起的），
    // 带 takeover=true 让后端允许同 token 下的接管——收口 W2 已知限制 #2。
    var takeover = !!(r.session && r.session !== SESSION);
    if (approve && r.destructive) {
      if (!APPR_STEP[r.request_id]) {
        // 不可逆动作要点两次：第一次只是「我知道你要做什么」，
        // 第二次才真发出去。判定仍在后端（后端也要求 double=true），
        // 这里只是不把两次点击压成一次。
        APPR_STEP[r.request_id] = true;
        toast("这是不可逆动作，再点一次「确认执行」才生效");
        renderApprovals({ pending: APPR_ROWS });
        return;
      }
      double = true;
    }
    api("/api/approval/respond", {
      method: "POST",
      body: { request_id: r.request_id, approve: approve, double: double,
              takeover: takeover,
              session: SESSION, reason: approve ? "" : "主人拒绝了这次操作" }
    }).then(function () {
      delete APPR_STEP[r.request_id];
      toast(approve ? "已确认：这次调用继续往下走" : "已拒绝：这次调用不做了");
      pollApprovals();
    }).catch(function (e) {
      toast("回应没送达：" + e.message, true);
      pollApprovals();
    });
  }

  function renderApprovalHistory(hist) {
    var box = $("apprHistList");
    if (!box) { return; }
    box.textContent = "";
    if (!hist || !hist.length) {
      box.appendChild(el("div", "apprnone", "（还没有审批记录）"));
      return;
    }
    hist.forEach(function (h) {
      var row = el("div", "ahrrow");
      var dec = h.decision || "timeout";
      var cls = dec === "approved" ? "yes" : (dec === "denied" ? "no" : "warn");
      row.appendChild(el("span", "badge " + cls,
        dec === "approved" ? "已批准" : (dec === "denied" ? "已拒绝" : "超时")));
      row.appendChild(el("span", "nm", h.cap || "?"));
      if (h.title) { row.appendChild(el("span", "tt", h.title)); }
      if (h.device) { row.appendChild(el("span", "badge", h.device)); }
      if (h.args_preview) { row.appendChild(el("div", "ap", h.args_preview)); }
      if (h.closed_reason) { row.appendChild(el("div", "ap dim", h.closed_reason)); }
      box.appendChild(row);
    });
  }

  function pollApprovals() {
    if (!TOKEN) { return; }
    api("/api/approvals").then(function (d) {
      renderApprovals(d);
    }).catch(function (e) {
      // 拉不到清单要说清楚，但不许刷屏（轮询会一直重试）。
      if (!APPR_TIMER) { toast("读不到待审批清单：" + e.message, true); }
    });
    APPR_TIMER = setTimeout(pollApprovals, 1000);
  }

  // 发起一次能力调用。后端会在闸门上挂起（等审批），所以这里的 promise
  // 要过一会儿才 resolve —— 中间用户看到的是待审批弹窗。
  function runCap(cap, args, device) {
    if (!requireToken()) { return; }
    APPR_TAG += 1;
    var tag = "call-" + APPR_TAG;
    var body = { cap: cap, args: args || {}, device: device || "",
                 tag: tag, session: SESSION };
    api("/api/call", { method: "POST", body: body }).then(function (res) {
      showResult(res);
      pollApprovals();
    }).catch(function (e) {
      showResult({ ok: false, cap: cap, code: "error", err: e.message });
      pollApprovals();
    });
  }

  /* ------------------------------------------------------------------ 引导 */
  function bootstrap() {
    var info = readTokenFromHash();
    TOKEN = info.token;
    SESSION = newSession();
    if (info.theme === "dark" || info.theme === "light") { applyTheme(info.theme); }
    if (!TOKEN) {
      toast("这个页面必须从设备编队的窗口里打开（缺令牌）", true);
    }

    api("/api/health").then(function (h) {
      $("regStat").textContent = "registry: " + h.devices + " devices / " + h.caps_count + " caps";
      if (h.backend) {
        $("modelName").textContent = h.backend.title || h.backend.id || "(未命名后端)";
        $("modelId").textContent = h.backend.model || h.backend.protocol || "";
      } else {
        $("modelName").textContent = "无可用后端";
        $("modelId").textContent = h.backend_reason ? "原因：" + h.backend_reason : "";
      }
      var p = h.persona || {};
      if (p.card_loaded && p.card && p.card.name) {
        $("personaName").textContent = p.card.name;
        $("personaAva").textContent = (p.card.name || "编").slice(0, 1);
        $("verline").textContent = "v0.1 · W1 · " + (p.card.spec || "");
      } else {
        $("personaName").textContent = "无角色卡";
        $("personaAva").textContent = "编";
      }
    }).catch(function (e) {
      $("regStat").textContent = "registry: 取不到（" + e.message + "）";
      toast("读取后端状态失败：" + e.message, true);
    });

    api("/api/devices").then(function (d) {
      renderDevices(d.devices || []);
    }).catch(function (e) {
      toast("读取设备列表失败：" + e.message, true);
    });

    api("/api/caps").then(renderCaps).catch(function (e) {
      toast("读取能力清单失败：" + e.message, true);
    });

    // W3 设备页
    loadHost();

    // 待审批清单：轮询。这是「界面上能看出哪些能力在等审批」的实现。
    pollApprovals();

    renderSessions();
  }

  /* ------------------------------------------------------------------ 事件绑定 */
  function bind() {
    var input = $("input");

    // 回车发送 / Shift+Enter 换行
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        doSend();
      }
    });
    input.addEventListener("input", function () {
      $("sendBtn").disabled = !input.value.trim() || BUSY;
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 160) + "px";
    });

    $("sendBtn").addEventListener("click", doSend);

    // 麦克风：W1 只占位，明确告诉用户还没接
    $("micBtn").addEventListener("click", function () {
      $("voiceBar").hidden = !$("voiceBar").hidden;
      $("micBtn").classList.toggle("on", !$("voiceBar").hidden);
      if (!$("voiceBar").hidden) {
        $("voiceText").textContent = "语音还没接上（W4 才录）—— 现在只是界面占位";
      }
    });

    // 主题切换
    $("themeBtn").addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
    });

    // 能力面板开合
    $("capBtn").addEventListener("click", function () {
      var p = $("capPanel");
      p.classList.toggle("open");
      $("capBtn").classList.toggle("on", p.classList.contains("open"));
    });
    $("capClose").addEventListener("click", function () {
      $("capPanel").classList.remove("open");
      $("capBtn").classList.remove("on");
    });

    // 模型选择器：W1 只读，后端清单在 /api/health 里
    $("modelPick").addEventListener("click", function () {
      api("/api/health").then(function (h) {
        var list = (h.backends || []).map(function (b) {
          return (b.title || b.id) + (b.model ? " (" + b.model + ")" : "");
        });
        toast(list.length ? "当前后端：" + list.join(" · ") : "没有可用后端：" + (h.backend_reason || "未知"));
      }).catch(function (e) { toast("取后端清单失败：" + e.message, true); });
    });

    // 桌宠：点一下看看人格状态
    $("pet").addEventListener("click", function () {
      api("/api/persona").then(function (p) {
        var st = p.state || {};
        var txt = "角色卡：" + ((p.card && p.card.name) || "无") +
          " · 情绪 " + (st.mood || "?") +
          (st.catchphrase ? " · 口癖「" + st.catchphrase + "」" : "");
        toast(txt);
      }).catch(function (e) { toast("读取人格状态失败：" + e.message, true); });
    });

    // ---- W3 设备页 ----
    $("dpRefresh").addEventListener("click", function () {
      // 🔴 点击必须**立刻**有反馈：loadHost() 是异步的，
      // 等它回来才 toast 的话，中间那几百毫秒页面毫无变化 ⇒
      // 真浏览器点审计会判它「点了页面无任何变化」（死链）。
      // 所以先转圈（同步可见变化），回来再落定。
      var btn = $("dpRefresh");
      btn.disabled = true;
      btn.textContent = "…";
      loadHost().then(function () {
        btn.disabled = false;
        btn.textContent = "↻";
        toast("设备页已刷新");
      }).catch(function (e) {
        btn.disabled = false;
        btn.textContent = "↻";
        toast("刷新失败：" + e.message, true);
      });
    });
    $("dpShotBtn").addEventListener("click", function () {
      $("dpShotBtn").disabled = true;
      $("dpShotBtn").textContent = "截图中…";
      loadHost().then(function () {
        $("dpShotBtn").disabled = false;
        $("dpShotBtn").textContent = "截屏";
      });
    });

    // ---- 左栏导航：切视图 ----
    // 规矩：每个导航项点进去必须**有反应**——要么切到真视图，
    // 要么明确说「这个还没做」。摆着不响应就是界面骗人。
    Array.prototype.forEach.call(document.querySelectorAll(".sb-item"), function (item) {
      item.addEventListener("click", function () {
        var view = item.getAttribute("data-view");
        var act = item.getAttribute("data-act");
        Array.prototype.forEach.call(document.querySelectorAll(".sb-item"), function (o) {
          o.classList.toggle("on", o === item);
        });
        if (view === "voice") {
          showView("voice", "语音对话");
          vvHint(VV.hands ? "免手已开" : "");
          return;
        }
        if (view === "assets") {
          showView("assets", "专家 · 技能 · 连接器");
          setAssetTab(ASSET_TAB);
          return;
        }
        if (view === "crew") {
          showView("crew", "助理 · 角色编队");
          loadCrew();
          return;
        }
        if (view === "chat") {
          showView("chat", "设备编队");
          if (act === "new") { newTask(); }
          return;
        }
        // 还没建的视图：明说，不假装
        showView("chat", "设备编队");
        toast("「" + item.textContent.trim() + "」这一页还没做（不假装）", true);
      });
    });

    // ---- 助理（角色编队） ----
    $("crewRefresh").addEventListener("click", function () {
      loadCrew();
      toast("已刷新名册");
    });

    // ---- 新建任务：清场，不是假按钮 ----
    // 「新建任务」和「助理」以前都指向 chat ⇒ 点了没区别（死链）。
    // 现在「新建任务」真的清空当前会话并聚焦输入框。
    function newTask() {
      var box = $("streamIn");
      if (box) { box.textContent = ""; }
      addUserMsg("（新任务开始）");
      var inp = $("input");
      if (inp) { inp.focus(); inp.value = ""; }
      toast("新任务已开（上一段对话清掉了）");
    }

    // ---- 三 Tab ----
    Array.prototype.forEach.call(document.querySelectorAll("#tabBar .tab"), function (b) {
      b.addEventListener("click", function () {
        setAssetTab(b.getAttribute("data-tab"));
      });
    });
    $("assetsRefresh").addEventListener("click", function () {
      loadAssetTab(ASSET_TAB);
      toast("已刷新");
    });

    // ---- 顶栏按钮 ----
    // 搜索：以前用 window.prompt —— headless 下浏览器会自动 dismiss 它，
    // 于是真浏览器点一遍判它「没反应」。而且 prompt 也谈不上好交互。
    // 现在改成**内联输入框**（真元素、真能输入、真出结果）。
    $("searchBtn").addEventListener("click", function () {
      var box = $("searchInput");
      box.hidden = !box.hidden;
      if (!box.hidden) { box.focus(); box.value = ""; runSearch(""); }
      else { $("searchResults").hidden = true; }
    });
    $("searchInput").addEventListener("input", function () {
      runSearch(this.value);
    });
    $("searchInput").addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        this.hidden = true;
        $("searchResults").hidden = true;
      }
      if (e.key === "Enter") { e.preventDefault(); runSearch(this.value); }
    });
    $("shareBtn").addEventListener("click", function () {
      var st = $("regStat").textContent || "";
      toast("本机状态：" + st + "（导出功能还没做，不假装）", true);
    });
    $("attachBtn").addEventListener("click", function () {
      toast("附件上传还没做（不假装）", true);
    });
    $("permBtn").addEventListener("click", function () {
      toast("权限设置还没做（不假装）", true);
    });

    // ---- 人格抽屉（X2） ----
    $("personaFoot").addEventListener("click", openDrawer);
    $("personaFoot").addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(); }
    });
    $("drawerClose").addEventListener("click", closeDrawer);
    $("drawerRefresh").addEventListener("click", function () {
      loadDrawer();
      toast("已刷新");
    });
    $("drawerMask").addEventListener("click", function (e) {
      if (e.target === this) { closeDrawer(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("drawerMask").hidden) { closeDrawer(); }
    });

    // ---- 窗口三键：给真反应，不许摆着 ----
    // 以前这三个是纯装饰的死链。现在：
    //   最小化 = 收起左栏（真布局变化）
    //   最大化 = 真全屏（requestFullscreen，真 API）
    //   关闭   = 关不掉浏览器页 ⇒ 明说，不假装
    function bindWinBtn(id, fn) {
      var b = $(id);
      b.addEventListener("click", fn);
      b.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); }
      });
    }
    bindWinBtn("tbMin", function () {
      var side = document.querySelector(".sidebar");
      if (!side) { return; }
      var hidden = side.classList.toggle("collapsed");
      toast(hidden ? "左栏已收起（再点一次展开）" : "左栏已展开");
    });
    bindWinBtn("tbMax", function () {
      if (document.fullscreenElement) {
        if (document.exitFullscreen) { document.exitFullscreen(); }
        toast("已退出全屏");
        return;
      }
      var el = document.documentElement;
      if (el.requestFullscreen) {
        el.requestFullscreen().then(function () { toast("已全屏"); })
          .catch(function (e) { toast("进不了全屏：" + e.message, true); });
      } else {
        toast("这个浏览器不支持全屏（不假装）", true);
      }
    });
    bindWinBtn("tbClose", function () {
      toast("浏览器里关不掉这一页（关标签页才行）——真桌面版才有真关闭", true);
    });
  }

  /* ==================================================================
     助理 · 角色编队

     真读 `cards/` 下的角色卡名册（/api/roster），
     当前激活的那个高亮。**零硬编码**——角色名来自后端。
     ================================================================== */

  function loadCrew() {
    var body = $("crewBody");
    body.textContent = "";
    body.appendChild(el("div", "empty", "读取中…"));
    api("/api/roster").then(function (d) {
      renderCrew(d);
    }).catch(function (e) {
      body.textContent = "";
      body.appendChild(el("div", "why", "读不到名册：" + e.message));
    });
  }

  function renderCrew(d) {
    var body = $("crewBody");
    body.textContent = "";
    if (d.error) { body.appendChild(el("div", "why", d.error)); }
    var roles = d.roles || [];
    body.appendChild(el("div", "sec", "名册 · " + roles.length + " 个角色"));
    if (!roles.length) {
      body.appendChild(el("div", "empty",
        "cards/ 目录下还没有角色卡。放 JSON 进去（chara_card_v3 格式）就有。"));
      return;
    }
    body.appendChild(el("div", "why",
      "当前激活：" + (d.active || "（读不到）")
      + (d.reason ? "　—— " + d.reason : "")
      + "。激活角色就是「谁在跟你说话」；其余是子角色（exec 角色）。"));
    var wall = el("div", "wall");
    roles.forEach(function (r) {
      var card = el("div", "card");
      var top = el("div", "ctop");
      var on = r.name === d.active;
      top.appendChild(el("span", on ? "dot-ok" : "dot-off"));
      top.appendChild(el("div", "cname", r.name));
      if (on) { top.appendChild(el("span", "badge-role", "激活")); }
      card.appendChild(top);
      var meta = el("div", "cmeta");
      meta.appendChild(el("span", "", r.provider || "(默认)"));
      meta.appendChild(el("span", "", "深度 " + (r.max_depth === undefined ? "—" : r.max_depth)));
      meta.appendChild(el("span", "",
        "可用能力 " + (r.cap_allow === "全部" ? "全部" : r.cap_allow + " 项")));
      if (r.source) { meta.appendChild(el("span", "", r.source)); }
      card.appendChild(meta);
      if (r.unknown_caps && r.unknown_caps.length) {
        card.appendChild(el("div", "creason err",
          "这张卡点了不存在的能力：" + r.unknown_caps.join("、")));
      }
      wall.appendChild(card);
    });
    body.appendChild(wall);
  }

  /* ==================================================================
     人格抽屉（X2）

     点侧栏底部的 .sb-foot 弹出来。三块内容，全部真读：
       ① 角色卡名 + 情绪三值（persona_state.json）
       ② 记忆条目（fleet_context.db），**逐条可删**（E-7）
       ③ 名册（哪个角色在跟你说话）
     🔴 拿不到就显示原因，不显示假的 0 或空列表。
     ================================================================== */

  function openDrawer() {
    $("drawerMask").hidden = false;
    loadDrawer();
  }

  function closeDrawer() {
    $("drawerMask").hidden = true;
  }

  function loadDrawer() {
    var body = $("drawerBody");
    body.textContent = "";
    body.appendChild(el("div", "empty", "读取中…"));

    var jobs = [
      api("/api/persona").then(function (d) { return { kind: "persona", d: d }; }),
      api("/api/memory").then(function (d) { return { kind: "memory", d: d }; }),
      api("/api/roster").then(function (d) { return { kind: "roster", d: d }; })
    ];
    Promise.all(jobs).then(function (res) {
      body.textContent = "";
      res.forEach(function (r) {
        if (r.kind === "persona") { drawerPersona(body, r.d); }
        else if (r.kind === "memory") { drawerMemory(body, r.d); }
        else { drawerRoster(body, r.d); }
      });
    }).catch(function (e) {
      body.textContent = "";
      body.appendChild(el("div", "why", "读不到人格信息：" + e.message));
    });
  }

  function drawerPersona(body, d) {
    body.appendChild(el("div", "sec", "她是谁"));
    if (d.error) {
      body.appendChild(el("div", "why", d.error));
      return;
    }
    var card = d.card || {};
    var st = d.state || {};
    body.appendChild(el("div", "drow",
      "角色卡：" + (card.name || "（没加载到）")
      + (card.spec ? "（" + card.spec + "）" : "")));
    if (!d.card_loaded) {
      body.appendChild(el("div", "creason err",
        "没有角色卡：现在是「没有人格的裸助手」，不会假装有设定。"));
    }
    var mood = st.mood || {};
    body.appendChild(el("div", "drow",
      "口癖：" + (st.catchphrase || "（没有）")
      + "　情绪：" + (st.name || "她")));
    body.appendChild(el("dmood"));
    var bars = body.lastChild;
    [["valence", "心情"], ["energy", "精力"], ["cling", "黏人"]].forEach(function (p) {
      var v = mood[p[0]];
      var line = el("div", "dmoodrow");
      line.appendChild(el("span", "dml", p[1]));
      var track = el("span", "dmtrack");
      var bar = el("i");
      var pct = (typeof v === "number") ? Math.max(0, Math.min(1, v)) : 0;
      bar.style.width = (pct * 100).toFixed(0) + "%";
      track.appendChild(bar);
      line.appendChild(track);
      line.appendChild(el("span", "dmv",
        (typeof v === "number") ? v.toFixed(2) : "—"));
      bars.appendChild(line);
    });
    if (st.obsessions && st.obsessions.length) {
      body.appendChild(el("div", "drow", "执念 " + st.obsessions.length + " 条"));
    }
  }

  function drawerMemory(body, d) {
    body.appendChild(el("div", "sec", "她记得什么 · " + (d.count || 0) + " 条"));
    if (d.error) {
      body.appendChild(el("div", "why", d.error));
      return;
    }
    body.appendChild(el("div", "why",
      "记忆是个人数据：只进可寻址可删的记忆层，不进任何被训练的参数。"
      + "每条都能单独删。"));
    var items = d.items || [];
    if (!items.length) {
      body.appendChild(el("div", "empty", "还没有记忆条目。"));
      return;
    }
    items.forEach(function (it) {
      var row = el("div", "drow");
      row.appendChild(el("span", "grow", it.key));
      row.appendChild(el("span", "dmemo", String(it.val || "").slice(0, 60)));
      var del = el("button", "hbtn", "删");
      del.title = "删掉这一条（" + it.ns + "/" + it.key + "）";
      del.addEventListener("click", function () {
        if (!window.confirm("删掉这条记忆？\n" + it.ns + "/" + it.key)) { return; }
        api("/api/memory/delete", { method: "POST", body: { ns: it.ns, key: it.key } })
          .then(function () { toast("已删：" + it.key); loadDrawer(); })
          .catch(function (e) { toast("删不掉：" + e.message, true); });
      });
      row.appendChild(del);
      body.appendChild(row);
    });
  }

  function drawerRoster(body, d) {
    body.appendChild(el("div", "sec", "名册 · " + (d.count || 0) + " 个角色"));
    if (d.error) {
      body.appendChild(el("div", "why", d.error));
      return;
    }
    var roles = d.roles || [];
    if (!roles.length) {
      body.appendChild(el("div", "empty", "cards/ 目录下没有角色卡。"));
      return;
    }
    roles.forEach(function (r) {
      body.appendChild(el("div", "drow",
        (r.name === d.active ? "● " : "○ ") + r.name
        + "（深度 " + r.max_depth + "，能力 "
        + (r.cap_allow === "全部" ? "全部" : r.cap_allow) + "）"));
    });
  }

  /* ================================================================ 语音对话（光球）
     与 chat 视图的分工：chat 是「打字聊天 + 设备工作台」，这里是「张嘴就聊」。
     光球不是装饰，它是状态机本身——语音交互最大的焦虑是「它到底收到没有」，
     呼吸、波纹、脉冲就是在回答这个焦虑。形态照 desktop.py 的独立语音模式。

     后端两个接口（server.py 新增；内部走**仓库根**的 voice.py，不是
     fleet/gui/voice.py 那套命令式语音）：
       POST /api/voice/recognize   裸 wav 进 → {ok, text} 出
       POST /api/voice/say         {text} 进 → 音频出（格式看 X-Voice-Format，音色随当前角色卡）

     录音走 getUserMedia + AudioContext 采 PCM 再自己封 WAV：不用 MediaRecorder，
     是因为它给的是 webm/opus，后端还得 ffmpeg 转一道；能省一次转码就省，
     也少一个失败点。

     🔴 失败一律写进字幕位并说清原因（权限 / 设备 / 超时 / HTTP 码），
        不许「转圈到底」——那是最难查的那种坏。
  */

  var VV = {
    state: "idle",        // idle | listen | think | speak
    hands: false,         // 免手连续
    ac: null, src: null, stream: null, node: null,
    chunks: [], sampleRate: 48000, t0: 0, timer: 0,
    audio: null, raf: 0, busy: false
  };

  var ORB_LABEL = { idle: "空闲", listen: "在听", think: "在想", speak: "在说" };

  function hexA(hex, a) {
    var h = String(hex || "").trim();
    if (h.charAt(0) === "#" && h.length === 4) {
      h = "#" + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2) + h.charAt(3) + h.charAt(3);
    }
    if (h.charAt(0) !== "#" || h.length !== 7) { return "rgba(47,107,216," + a + ")"; }
    var n = parseInt(h.slice(1), 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  function orbAccent() {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue("--accent");
      return (v || "").trim() || "#2f6bd8";
    } catch (e) { return "#2f6bd8"; }
  }

  function orbDraw() {
    var cv = $("orbCanvas");
    if (!cv || !cv.getContext) { return; }
    var ctx = cv.getContext("2d");
    var dpr = window.devicePixelRatio || 1;
    var w = cv.clientWidth || 260, h = cv.clientHeight || 260;
    var pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    // 只在尺寸真变了才重设（重设会清空画布，每帧都设会闪）
    if (cv.width !== pw || cv.height !== ph) { cv.width = pw; cv.height = ph; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    var cx = w / 2, cy = h / 2;
    var base = Math.min(w, h) * 0.24;      // 核心球基准半径
    var t = performance.now() / 1000;
    var col = orbAccent();

    // 底光晕：让球"发光"，不是贴纸
    var glow = ctx.createRadialGradient(cx, cy, base * 0.2, cx, cy, base * 2.6);
    glow.addColorStop(0, hexA(col, 0.28));
    glow.addColorStop(1, hexA(col, 0));
    ctx.fillStyle = glow;
    ctx.beginPath(); ctx.arc(cx, cy, base * 2.6, 0, Math.PI * 2); ctx.fill();

    // 听：波纹一圈圈往外扩，扩到边就淡掉
    if (VV.state === "listen") {
      for (var i = 0; i < 3; i++) {
        var p = (t * 0.55 + i / 3) % 1;
        ctx.strokeStyle = hexA(col, 0.40 * (1 - p));
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(cx, cy, base + p * base * 1.9, 0, Math.PI * 2); ctx.stroke();
      }
    }

    // 想：两段弧反向转
    if (VV.state === "think") {
      for (var k = 0; k < 2; k++) {
        var a0 = t * 2.2 + k * Math.PI;
        ctx.strokeStyle = hexA(col, 0.75 - k * 0.35);
        ctx.lineWidth = 3;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.arc(cx, cy, base + 10 + k * 9, a0, a0 + Math.PI * 0.55);
        ctx.stroke();
      }
    }

    // 核心球：呼吸节奏随状态变（idle 慢而浅 / speak 快而深）
    var amp = 0.06, speed = 0.8;
    if (VV.state === "speak") { amp = 0.20; speed = 5.2; }
    else if (VV.state === "listen") { amp = 0.10; speed = 2.4; }
    else if (VV.state === "think") { amp = 0.04; speed = 1.6; }
    var r0 = base * (1 + Math.sin(t * speed) * amp);

    var core = ctx.createRadialGradient(
      cx - r0 * 0.30, cy - r0 * 0.35, r0 * 0.15, cx, cy, r0);
    core.addColorStop(0, hexA(col, 0.95));
    core.addColorStop(0.65, hexA(col, 0.55));
    core.addColorStop(1, hexA(col, 0.22));
    ctx.fillStyle = core;
    ctx.beginPath(); ctx.arc(cx, cy, r0, 0, Math.PI * 2); ctx.fill();
  }

  function orbLoop() {
    // 视图不可见就不画（rAF 空转开销极小，但画布不白画）
    var v = $("viewVoice");
    if (v && v.classList.contains("on")) {
      // 画布不可用（离线 canvas mock / 极小窗口）时只跳过这一帧，
      // 绝不让光球把整个视图带崩——语音功能不依赖它。
      try { orbDraw(); } catch (e) { /* 跳过本帧 */ }
    }
    // 门禁的模拟 DOM 里没有 requestAnimationFrame：不循环，也不抛
    if (typeof requestAnimationFrame === "function") {
      VV.raf = requestAnimationFrame(orbLoop);
    }
  }

  function setOrbState(s) {
    VV.state = s;
    var hint = $("vvHint");
    if (hint && !hint.dataset.err) { hint.textContent = ORB_LABEL[s] || s; }
  }

  function vvSub(text) {
    var n = $("vvSub");
    if (n) { n.textContent = text || ""; }
  }

  function vvHint(text, isErr) {
    var n = $("vvHint");
    if (!n) { return; }
    n.textContent = text || "";
    n.style.color = isErr ? "var(--danger)" : "";
    if (isErr) { n.dataset.err = "1"; } else { delete n.dataset.err; }
  }

  function vvFail(msg) {
    vvHint("✕ " + msg, true);
    setOrbState("idle");
    $("vvMic").classList.remove("rec");
    VV.busy = false;
  }

  /* ---- 录音 ---- */

  function vvCloseRec() {
    try { if (VV.node) { VV.node.disconnect(); } } catch (e) { /* 已断 */ }
    try { if (VV.src) { VV.src.disconnect(); } } catch (e) { /* 已断 */ }
    try { if (VV.stream) { VV.stream.getTracks().forEach(function (tr) { tr.stop(); }); } } catch (e) { /* 已停 */ }
    try { if (VV.ac && VV.ac.close) { VV.ac.close(); } } catch (e) { /* 已关 */ }
    VV.node = VV.src = VV.stream = VV.ac = null;
  }

  function vvStartRec() {
    if (VV.state === "listen") { return vvStopRec(); }
    if (VV.busy) { return; }
    if (!requireToken()) { return; }
    // 正在说话就打断——语音交互里「不想听了」必须立刻生效
    if (VV.audio) { try { VV.audio.pause(); } catch (e) { /* 已停 */ } VV.audio = null; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return vvFail("这个窗口拿不到麦克风接口（navigator.mediaDevices 缺失）");
    }
    navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
    }).then(function (stream) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) {
        stream.getTracks().forEach(function (tr) { tr.stop(); });
        return vvFail("这个窗口没有 AudioContext，录不了音");
      }
      VV.stream = stream;
      VV.ac = new AC();
      VV.sampleRate = VV.ac.sampleRate || 48000;
      VV.src = VV.ac.createMediaStreamSource(stream);
      VV.node = VV.ac.createScriptProcessor(4096, 1, 1);
      VV.chunks = [];
      VV.node.onaudioprocess = function (ev) {
        VV.chunks.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
      };
      VV.src.connect(VV.node);
      VV.node.connect(VV.ac.destination);
      VV.t0 = Date.now();
      setOrbState("listen");
      vvSub("在听……说完点一下麦克风。");
      vvHint("麦克风已开（采样率 " + VV.sampleRate + " Hz）");
      $("vvMic").classList.add("rec");
      // 60 秒兜底：忘了停就一直录会吃满内存
      clearTimeout(VV.timer);
      VV.timer = setTimeout(function () {
        if (VV.state === "listen") { vvStopRec(); }
      }, 60000);
    }).catch(function (e) {
      var n = (e && e.name) || "Error";
      var why = n === "NotAllowedError"
        ? "麦克风被拒绝：允许后重试（地址栏旁的权限图标）"
        : n === "NotFoundError" ? "系统里找不到麦克风设备"
          : n === "NotReadableError" ? "麦克风被别的程序占着"
            : ((e && e.message) || String(e));
      vvFail("拿不到麦克风：" + why);
    });
  }

  // 48k → 16k：取平均而不是抽点，避免混叠
  function vvDown(buf, from, to) {
    if (!to || to >= from) { return buf; }
    var ratio = from / to;
    var n = Math.floor(buf.length / ratio);
    var out = new Float32Array(n);
    for (var i = 0; i < n; i++) {
      var a = Math.floor(i * ratio);
      var b = Math.min(Math.floor((i + 1) * ratio), buf.length);
      var s = 0;
      for (var j = a; j < b; j++) { s += buf[j]; }
      out[i] = s / Math.max(1, b - a);
    }
    return out;
  }

  function vvEncodeWav(samples, rate) {
    var n = samples.length;
    var ab = new ArrayBuffer(44 + n * 2);
    var dv = new DataView(ab);
    function ws(off, s) {
      for (var i = 0; i < s.length; i++) { dv.setUint8(off + i, s.charCodeAt(i)); }
    }
    ws(0, "RIFF"); dv.setUint32(4, 36 + n * 2, true); ws(8, "WAVE");
    ws(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true);
    dv.setUint16(22, 1, true); dv.setUint32(24, rate, true);
    dv.setUint32(28, rate * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
    ws(36, "data"); dv.setUint32(40, n * 2, true);
    for (var i = 0; i < n; i++) {
      var v = Math.max(-1, Math.min(1, samples[i]));
      dv.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    }
    return new Blob([ab], { type: "audio/wav" });
  }

  function vvStopRec() {
    if (VV.state !== "listen") { return Promise.resolve(null); }
    clearTimeout(VV.timer);
    var total = 0;
    VV.chunks.forEach(function (c) { total += c.length; });
    var sr = VV.sampleRate || 48000;
    var pcm = new Float32Array(total);
    var off = 0;
    VV.chunks.forEach(function (c) { pcm.set(c, off); off += c.length; });
    VV.chunks = [];
    vvCloseRec();
    $("vvMic").classList.remove("rec");
    var durMs = Date.now() - VV.t0;

    if (!total) { vvFail("没录到声音（0 个采样点）：检查系统默认输入设备"); return Promise.resolve(null); }
    if (durMs < 400) { vvFail("录得太短（" + durMs + "ms），再说一次"); return Promise.resolve(null); }

    vvHint("录到 " + (durMs / 1000).toFixed(1) + " 秒，送回本机识别……");
    return Promise.resolve(vvEncodeWav(vvDown(pcm, sr, 16000), 16000)).then(function (blob) {
      return vvRecognize(blob);
    }).then(function (d) {
      var text = (d.text || "").trim();
      if (!text) { throw new Error("识别结果为空（后端没听出字来）"); }
      vvSub(text);
      vvHint("识别：" + (d.meta && d.meta.backend ? d.meta.backend : "whisper"));
      return vvAsk(text);
    }).catch(function (e) {
      vvFail(e.message || String(e));
    });
  }

  /* ---- 后端三趟：识别 → 对话(SSE) → 合成 ---- */

  function vvRecognize(blob) {
    setOrbState("think");
    vvSub("……");
    return fetch("/api/voice/recognize", {
      method: "POST",
      headers: { "X-Fleet-Token": TOKEN, "Content-Type": "audio/wav" },
      body: blob,
      cache: "no-store"
    }).then(function (r) {
      return r.text().then(function (txt) {
        var d = null;
        try { d = JSON.parse(txt); } catch (e) { d = null; }
        if (!r.ok || !d || !d.ok) {
          throw new Error("识别失败（HTTP " + r.status + "）："
            + ((d && d.err) || txt.slice(0, 160) || "无响应体"));
        }
        return d;
      });
    });
  }

  // 收集 SSE 的完整回答文本（不往 chat 视图渲染，两个视图互不污染）
  function vvAsk(prompt) {
    VV.busy = true;
    setOrbState("think");
    return fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fleet-Token": TOKEN },
      body: JSON.stringify({ prompt: prompt }),
      cache: "no-store"
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          var why = t;
          try { why = JSON.parse(t).err || t; } catch (e) { /* 原样带出 */ }
          throw new Error(why || ("HTTP " + r.status));
        });
      }
      var reader = r.body.getReader();
      var dec = new TextDecoder("utf-8");
      var buf = "", text = "", err = "";
      function handle(frame) {
        if (!frame) { return; }
        frame.split("\n").forEach(function (line) {
          if (line.indexOf("data:") !== 0) { return; }
          var payload = line.slice(5).trim();
          if (!payload) { return; }
          var ev = null;
          try { ev = JSON.parse(payload); } catch (e) { return; }
          if (ev.type === "line" && ev.text) {
            text += ev.text;
            vvSub(text);              // 边收边显示，不等全说完
          } else if (ev.type === "error") {
            err = ev.err || "后端报了错但没给原因";
          }
        });
      }
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) { handle(buf); return; }
          buf += dec.decode(chunk.value, { stream: true });
          var parts = buf.split("\n\n");
          buf = parts.pop();
          parts.forEach(handle);
          return pump();
        });
      }
      return pump().then(function () {
        if (err) { throw new Error(err); }
        if (!text.trim()) { throw new Error("模型没返回任何文字"); }
        return text.trim();
      });
    });
  }

  function vvSpeak(text) {
    setOrbState("speak");
    return fetch("/api/voice/say", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fleet-Token": TOKEN },
      body: JSON.stringify({ text: text }),
      cache: "no-store"
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          var why = t;
          try { why = JSON.parse(t).err || t; } catch (e) { /* 原样带出 */ }
          throw new Error("合成失败（HTTP " + r.status + "）：" + (why || "").slice(0, 160));
        });
      }
      var backend = r.headers.get("X-Voice-Backend") || "?";
      var card = r.headers.get("X-Voice-Card") || "默认";
      vvHint("出声：" + backend + " · 卡 " + card);
      return r.blob();
    }).then(function (blob) {
      return new Promise(function (resolve, reject) {
        var url = URL.createObjectURL(blob);
        var a = new Audio(url);
        VV.audio = a;
        a.onended = function () {
          URL.revokeObjectURL(url);
          VV.audio = null;
          resolve(true);
        };
        a.onerror = function () {
          URL.revokeObjectURL(url);
          VV.audio = null;
          reject(new Error("播放失败：音频解不开（格式 " + (blob.type || "未知") + "）"));
        };
        var p = a.play();
        if (p && p.catch) {
          p.catch(function (e) {
            reject(new Error("播放被拦：" + ((e && e.message) || "浏览器要求先有一次用户操作")));
          });
        }
      });
    });
  }

  // 一轮完整对话：文字进 → 文字 + 出声
  function vvTurn(text) {
    if (VV.busy) { return; }
    vvFail("");                    // 清掉上一条错误提示
    vvHint(ORB_LABEL.think || "");
    vvAsk(text).then(function (answer) {
      vvSub(answer);
      return vvSpeak(answer);
    }).then(function () {
      VV.busy = false;
      setOrbState("idle");
      if (VV.hands) {
        // 免手：说完自动回到听
        setTimeout(function () { if (VV.hands && VV.state === "idle") { vvStartRec(); } }, 260);
      }
    }).catch(function (e) {
      vvFail(e.message || String(e));
    });
  }

  function initVoice() {
    if (!$("viewVoice")) { return; }
    $("vvMic").addEventListener("click", function () { vvStartRec(); });
    $("vvSend").addEventListener("click", function () {
      var inp = $("vvInput");
      var t = (inp.value || "").trim();
      if (!t) { vvHint("输入框是空的", true); return; }
      inp.value = "";
      vvSub(t);
      vvTurn(t);
    });
    $("vvInput").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        $("vvSend").click();
      }
    });
    $("vvHands").addEventListener("click", function () {
      VV.hands = !VV.hands;
      $("vvHands").classList.toggle("on", VV.hands);
      vvHint(VV.hands ? "免手已开：说完自动再听" : "免手已关");
      if (VV.hands && VV.state === "idle") { vvStartRec(); }
    });
    orbLoop();
  }

  /* ------------------------------------------------------------------ 视图切换 */
  function showView(name, title) {
    $("viewChat").classList.toggle("on", name === "chat");
    $("viewAssets").classList.toggle("on", name === "assets");
    $("viewCrew").classList.toggle("on", name === "crew");
    if ($("viewVoice")) { $("viewVoice").classList.toggle("on", name === "voice"); }
    if (title) { $("viewTitle").textContent = title; }
  }

  /* ------------------------------------------------------------------ 搜索
     搜四个真实来源：能力、设备、技能、角色卡。
     全部来自后端接口的结果缓存，前端不硬编码任何名字。 */
  function searchAll(kw) {
    var out = [];
    (ASSET_CACHE.caps || []).forEach(function (c) {
      if ((c.name || "").toLowerCase().indexOf(kw) >= 0
        || (c.title || "").toLowerCase().indexOf(kw) >= 0) {
        out.push("能力 " + c.name);
      }
    });
    (ASSET_CACHE.conns && ASSET_CACHE.conns.connectors || []).forEach(function (r) {
      if ((r.name || "").toLowerCase().indexOf(kw) >= 0) { out.push("连接器 " + r.name); }
    });
    (ASSET_CACHE.skills && ASSET_CACHE.skills.skills || []).forEach(function (s) {
      if ((s.name || "").toLowerCase().indexOf(kw) >= 0
        || (s.description || "").toLowerCase().indexOf(kw) >= 0) {
        out.push("技能 " + s.name);
      }
    });
    (ASSET_CACHE.cards && ASSET_CACHE.cards.cards || []).forEach(function (c) {
      if ((c.name || "").toLowerCase().indexOf(kw) >= 0
        || (c.file || "").toLowerCase().indexOf(kw) >= 0) {
        out.push("专家 " + c.name);
      }
    });
    return out;
  }

  /* 搜索结果面板：真输入、真出结果。
     数据可能还没拉（用户没点过三 Tab）⇒ 先把四类源都拉一遍再搜。 */
  function runSearch(raw) {
    var panel = $("searchResults");
    panel.hidden = false;
    panel.textContent = "";
    var kw = (raw || "").trim().toLowerCase();
    var need = [];
    if (!ASSET_CACHE.caps) { need.push(api("/api/caps").then(function (d) { ASSET_CACHE.caps = d.caps || []; })); }
    if (!ASSET_CACHE.conns) { need.push(api("/api/connectors").then(function (d) { ASSET_CACHE.conns = d; })); }
    if (!ASSET_CACHE.skills) { need.push(api("/api/skills").then(function (d) { ASSET_CACHE.skills = d; })); }
    if (!ASSET_CACHE.cards) { need.push(api("/api/cards").then(function (d) { ASSET_CACHE.cards = d; })); }
    if (!need.length) { paintSearch(kw); return; }
    panel.appendChild(el("div", "why", "正在拉取可搜的内容…"));
    Promise.all(need).then(function () { paintSearch(kw); })
      .catch(function (e) {
        panel.textContent = "";
        panel.appendChild(el("div", "why", "搜索失败：" + e.message));
      });
  }

  function paintSearch(kw) {
    var panel = $("searchResults");
    panel.textContent = "";
    if (!kw) {
      panel.appendChild(el("div", "why",
        "搜能力 / 设备 / 技能 / 角色卡。输入即搜。"));
      return;
    }
    var hits = searchAll(kw);
    panel.appendChild(el("div", "why", "命中 " + hits.length + " 条"));
    if (!hits.length) {
      panel.appendChild(el("div", "empty", "没找到「" + kw + "」"));
      return;
    }
    hits.slice(0, 40).forEach(function (h) {
      panel.appendChild(el("div", "srow", h));
    });
  }

  /* ------------------------------------------------------------------ 设备页（W3）
     数据全部来自后端 /api/host（它走 registry.call，闸门在服务端）。
     🔴 界面不许自己算一套状态 —— 「这台设备现在能不能用」只有注册表知道。
     🔴 拿不到就显示原因，不显示假的 0 或空表格。 */
  var PROC_SORT = { key: "cpu", dir: -1 };     // 默认按 CPU 降序
  var PROC_ROWS = [];

  function fmtBytes(n) {
    if (n === undefined || n === null) { return "—"; }
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + " " + u[i];
  }

  function setBar(barId, valId, pct, text) {
    var bar = $(barId);
    if (bar) { bar.style.width = Math.max(0, Math.min(100, pct)) + "%"; }
    if (valId) { $(valId).textContent = text; }
  }

  function renderNotes(notes) {
    var box = $("dpNotes");
    box.textContent = "";
    (notes || []).forEach(function (n) {
      box.appendChild(el("div", "dpnote", n));
    });
  }

  function renderProcs(rows) {
    PROC_ROWS = rows || [];
    var box = $("dpProcs");
    box.textContent = "";
    if (!PROC_ROWS.length) {
      $("dpProcHint").textContent = "（空）";
      box.appendChild(el("div", "hint", "没有进程数据"));
      return;
    }
    var key = PROC_SORT.key, dir = PROC_SORT.dir;
    var sorted = PROC_ROWS.slice().sort(function (a, b) {
      if (key === "name") {
        return String(a.name || "").localeCompare(String(b.name || "")) * dir;
      }
      if (key === "pid") { return (Number(a.pid) - Number(b.pid)) * dir; }
      return (Number(a.cpu) - Number(b.cpu)) * dir;
    });
    $("dpProcHint").textContent = sorted.length + " 个";
    sorted.forEach(function (p) {
      var row = el("div", "prow");
      row.appendChild(el("span", "sortarrow",
        key === "cpu" ? (dir < 0 ? "▼" : "▲") : ""));
      var name = el("span", "pname", p.name || "?");
      name.title = (p.name || "?") + "（pid " + p.pid + "）";
      row.appendChild(name);
      row.appendChild(el("span", "ppid", p.pid));
      row.appendChild(el("span", "pcpu", (p.cpu === undefined ? "" : p.cpu + "%")));
      // 点表头区域排序：点名字按名字，点 CPU 按 CPU
      row.addEventListener("click", function () { procSort("name"); });
      box.appendChild(row);
    });
  }

  function procSort(key) {
    if (PROC_SORT.key === key) {
      PROC_SORT.dir = -PROC_SORT.dir;            // 再点一次换方向
    } else {
      PROC_SORT.key = key; PROC_SORT.dir = (key === "name") ? 1 : -1;
    }
    renderProcs(PROC_ROWS);
  }

  function renderHost(d) {
    $("dpDevice").textContent = d.device || "—";
    renderNotes(d.notes || []);

    var m = d.monitor;
    if (m) {
      setBar("barCpu", "valCpu", m.cpu, (m.cpu === undefined ? "—" : m.cpu + "%"));
      setBar("barMem", "valMem", m.mem,
        (m.mem === undefined ? "—" : m.mem + "% · " + fmtBytes(m.mem_used) + "/" + fmtBytes(m.mem_total)));
      setBar("barDisk", "valDisk", m.disk, (m.disk === undefined ? "—" : m.disk + "%"));
      // 网络：上下行各画一条，宽度按 KB/s 归一（1000KB/s 满格）
      var up = $( "barUp"), down = $("barDown");
      if (up) { up.style.width = Math.min(100, (m.net_up || 0) / 10) + "%"; }
      if (down) { down.style.width = Math.min(100, (m.net_down || 0) / 10) + "%"; }
      $("valNet").textContent = "↑" + (m.net_up || 0) + " ↓" + (m.net_down || 0) + " KB/s";
      renderProcs(m.procs || []);
    } else {
      setBar("barCpu", "valCpu", 0, "—");
      setBar("barMem", "valMem", 0, "—");
      setBar("barDisk", "valDisk", 0, "—");
      setBar("barUp", "valNet", 0, "—");
      renderProcs([]);
    }

    if (d.apps && d.apps.length) {
      // 应用清单（host.apps）；进程表优先用 monitor 里的（带 CPU）
      if (!(m && m.procs && m.procs.length)) {
        renderProcs(d.apps.map(function (a) { return { pid: a.pid, name: a.name }; }));
      }
    }

    var s = d.screen;
    var box = $("dpShotBox");
    if (s && s.shot_id) {
      box.textContent = "";
      var img = new Image();
      img.alt = "屏幕预览";
      // 鉴权头：图片走 <img src> 没法带自定义头 ⇒ 用 fetch + blob
      fetch("/api/shot?id=" + encodeURIComponent(s.shot_id), {
        headers: { "X-Fleet-Token": TOKEN }, cache: "no-store"
      }).then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.blob();
      }).then(function (blob) {
        box.textContent = "";
        var im = new Image();
        im.src = URL.createObjectURL(blob);
        im.alt = "屏幕预览";
        box.appendChild(im);
      }).catch(function (e) {
        box.textContent = "";
        box.appendChild(el("span", "hint", "取图失败：" + e.message));
      });
    } else {
      box.textContent = "";
      box.appendChild(el("span", "hint", "点「截屏」看真图"));
    }
  }

  function loadHost() {
    return api("/api/host").then(renderHost).catch(function (e) {
      $("dpDevice").textContent = "—";
      renderNotes(["设备页取不到数据：" + e.message]);
    });
  }

  function doSend() {
    var input = $("input");
    var text = input.value.trim();
    if (!text) { return; }
    input.value = "";
    input.style.height = "auto";
    $("sendBtn").disabled = true;
    sendChat(text);
  }

  /* ------------------------------------------------------------------ 起 */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      bind(); bootstrap(); initVoice();
    });
  } else {
    bind();
    bootstrap();
    initVoice();
  }
})();
