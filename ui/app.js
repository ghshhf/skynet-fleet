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
    var bar = $("resBar");
    bar.hidden = false;
    bar.classList.toggle("err", !res.ok);
    var head = res.ok ? "✓" : "✕";
    var txt = head + " " + (res.cap || "") + " · " + (res.code || "") +
      " · " + (res.ms !== undefined ? res.ms + "ms" : "");
    if (res.err) { txt += " · " + res.err; }
    if (res.via) { txt += " · via " + res.via; }
    bar.textContent = txt;
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
  }

  function respondApproval(r, approve) {
    if (!requireToken()) { return; }
    var double = false;
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
    document.addEventListener("DOMContentLoaded", function () { bind(); bootstrap(); });
  } else {
    bind();
    bootstrap();
  }
})();
