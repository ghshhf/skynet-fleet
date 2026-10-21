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

  var ASSET_TAB = "cards";
  var ASSET_CACHE = { cards: null, skills: null, conns: null, caps: null, audio: null };

  function setAssetTab(tab) {
    ASSET_TAB = tab;
    Array.prototype.forEach.call(document.querySelectorAll("#tabBar .tab"), function (b) {
      b.classList.toggle("on", b.getAttribute("data-tab") === tab);
    });
    loadAssetTab(tab);
  }

  function loadAssetTab(tab) {
    var body = $("tabBody");
    body.textContent = "";
    body.appendChild(el("div", "empty", "读取中…"));

    if (tab === "cards") {
      api("/api/cardstore").then(function (d) {
        ASSET_CACHE.cards = d;
        renderCardsTab(d);
      }).catch(function (e) { renderTabErr(body, "读不到角色卡：" + e.message); });
      return;
    }
    if (tab === "skills") {
      api("/api/skills").then(function (d) {
        ASSET_CACHE.skills = d;
        renderSkillsTab(d);
      }).catch(function (e) { renderTabErr(body, "读不到技能清单：" + e.message); });
      return;
    }
    if (tab === "audio") {
      api("/api/audio/presets").then(function (d) {
        ASSET_CACHE.audio = d;
        renderAudioTab(d);
      }).catch(function (e) { renderTabErr(body, "读不到音频预设：" + e.message); });
      return;
    }
    api("/api/connectors").then(function (d) {
      ASSET_CACHE.conns = d;
      renderConnsTab(d);
    }).catch(function (e) { renderTabErr(body, "读不到连接器清单：" + e.message); });
  }

  function renderTabErr(body, why) {
    body.textContent = "";
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

  function renderSkillsTab(d) {
    var body = $("tabBody");
    body.textContent = "";
    if (d.error) { body.appendChild(el("div", "why", d.error)); }
    var skills = d.skills || [];

    // 按来源分组：仓内技能在前，用户级在后。
    var repo = skills.filter(function (s) { return s.source === "repo"; });
    var user = skills.filter(function (s) { return s.source !== "repo"; });

    body.appendChild(el("div", "sec", "技能 · " + skills.length + " 个（SKILL.md）"));
    if (!skills.length) {
      body.appendChild(el("div", "empty",
        "还没读到任何技能。技能是带 frontmatter 的 <b>SKILL.md</b> 文件，"
        + "放在仓内 <b>skills/</b> 或用户级 <b>~/.workbuddy/skills/</b> 目录下。"));
      return;
    }
    if (repo.length) { body.appendChild(el("div", "sec", "本仓")); body.appendChild(skillWall(repo)); }
    if (user.length) { body.appendChild(el("div", "sec", "用户级")); body.appendChild(skillWall(user)); }
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
