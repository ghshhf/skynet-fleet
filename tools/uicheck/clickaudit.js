/* ==========================================================================
   图形版的「运行时点击体检」——真点一遍，找出所有点了没反应的元素。

   为什么必须真点（这是本轮实测踩出来的坑）：
     · 计划书原来的口径是「AST 扫 index.html 找带 id 的可点元素，再看 app.js 里
       有没有出现」——**这个口径会同时漏报和误报**：
         - 漏报：6 个 `.sb-item` 导航项**根本没有 id**（纯 class）⇒ 扫 id 抓不到，
           而它们恰恰是最大的死链源；
         - 误报：`barCpu` 这类是 `setBar("barCpu", …)` 字符串传入的，
           并没有独立 id 绑定，差集法会把它当成死链 ⇒ 门禁红一片假的。
     · 真浏览器里点一遍，**有没有反应是二值事实**，不用猜口径。

   判定「有反应」的办法（比看 console 可靠）：
     ① 点之前记 DOM 指纹（关键区域 innerHTML 长度 + 可见元素数 + URL 片段）；
     ② 点；
     ③ 等一小会儿，再记一次；
     ④ 指纹变了 = 有反应；没变 = 死链。

   用法：
     node tools/uicheck/clickaudit.js
     node tools/uicheck/clickaudit.js --json out.json
   退出码：0 无死链；1 有死链（门禁用）。
   ========================================================================== */
"use strict";

const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const { chromium } = require("playwright");

const ROOT = path.join(__dirname, "..", "..");
// 换机即挂的硬编码（原为 D:/tools/python/python.exe）已去掉：
// 优先 FLEET_PY 环境变量，否则用 PATH 里的 python —— 换机不再 ModuleNotFoundError。
const PY = process.env.FLEET_PY || (process.platform === "win32" ? "python" : "python3");

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  if (i < 0) { return dflt; }
  const v = process.argv[i + 1];
  if (!v || v.startsWith("--")) { return true; }
  return v;
}
const THEME = arg("theme", "light");
const SETTLE = Number(arg("settle", 350));
// 🔴 「有反应」最多等多久（毫秒）。点下去是异步动作时（POST /api/call
// 要等审批弹窗出来），固定等一小会儿会误判成死链 ⇒ 轮询到变了就停。
const ASYNC_WAIT = Number(arg("asyncWait", 2000));
const JSON_OUT = arg("json", "");

function startServer() {
  return new Promise((resolve, reject) => {
    const code = [
      "import sys; sys.path.insert(0, r'" + ROOT.replace(/\\/g, "\\\\") + "')",
      "from fleet.gui.server import FleetServer",
      "from fleet.gui.approval import ApprovalBroker",
      "s = FleetServer(approval=ApprovalBroker()); s.start()",
      "print('FLEET_URL=' + s.page_url(r'" + THEME + "'), flush=True)",
      "import time; time.sleep(600)",
    ].join("\n");
    const p = spawn(PY, ["-c", code], { cwd: ROOT });
    let buf = "", done = false;
    const timer = setTimeout(() => { if (!done) reject(new Error("起服务超时")); }, 20000);
    p.stdout.on("data", (d) => {
      buf += d.toString();
      const m = buf.match(/FLEET_URL=(\S+)/);
      if (m && !done) { done = true; clearTimeout(timer); resolve({ url: m[1], proc: p }); }
    });
    p.on("error", reject);
  });
}

/* 在页面里枚举「可点元素」——口径写在页面内，与真实渲染一致。 */
const CLICKABLE_SEL =
  "a[href], button, [role=button], [onclick], .hbtn, .cbtn, .sb-item, .sb-foot, .tb-win i";

const ENUMERATE = (onlyPhase) => {
  const sel = "a[href], button, [role=button], [onclick], .hbtn, .cbtn, .sb-item, .sb-foot, .tb-win i";
  // 🔴 先清掉上一轮打的标记——每轮枚举都要重新分配，
  // 否则同一个元素会带着旧标记，取到的可能是别的元素。
  document.querySelectorAll("[data-audit-id]").forEach((n) => {
    n.removeAttribute("data-audit-id");
  });
  // 🔴 元素归属：谁负责测这个元素。
  //   抽屉是**模态**——打开后背景被遮罩盖住，背景上的元素本来就点不到。
  //   若不限定范围，人格抽屉相位会把侧栏、titlebar 等 30 多个背景元素
  //   全记成「被遮住，点不到」——报告被噪音淹没，覆盖统计算歪，
  //   真正该测的抽屉内元素反而被淹了。
  //   规则（先匹配到的赢）：模态 → view → 其余归聊天主视图。
  //   抽屉里的元素归抽屉相位；背景元素**由聊天相位测**（那时抽屉没开）。
  //   这不是放水——是让每个元素在**它该被测的环境里**被测一次。
  const ownerOf = (n) => {
    const mw = n.closest && n.closest(".apprmask");
    if (mw) {
      return (mw.id === "drawerMask") ? "人格抽屉" : "能力面板";
    }
    if (n.closest && n.closest(".capwrap")) { return "能力面板"; }
    const v = n.closest && n.closest(".view");
    if (v) {
      if (v.id === "viewAssets") { return "专家·技能·连接器"; }
      if (v.id === "viewCrew") { return "助理·角色编队"; }
      return "聊天主视图";
    }
    return "聊天主视图";
  };
  const all = Array.from(document.querySelectorAll(sel));
  const nodes = onlyPhase ? all.filter((n) => ownerOf(n) === onlyPhase) : all;
  const vw = window.innerWidth, vh = window.innerHeight;
  // 🔴 给每个元素打一个**稳定标记**（data-audit-id）。
  // 实测踩出来的坑（门禁骗人的第六种形态）：原先用
  // `page.$$(sel)` 在**初始快照**时一次性取回全部句柄，
  // 之后按**下标** handles[i] 去用。可相位里 DOM 一直在变
  // （展开面板插入新按钮 / 换视图重建卡片 / 列表刷新换节点），
  // 下标就对不上了 ⇒ 拿到的句柄根本不是本次要测的那个元素。
  // 症状很迷惑：`.tab` 被报「被 DIV.titlebar 遮住」、
  // `#drawerRefresh` 被报「元素已不在 DOM」——实测那三个 Tab
  // 明明能点（elementFromPoint 命中自己，y=94 而 titlebar 只到 y=38）。
  // ⇒ 改为：枚举时打标记，之后**每次按标记重新查**。
  // 元素真被删了，查不到就如实记「元素已不在 DOM」，不猜。
  nodes.forEach((n, i) => n.setAttribute("data-audit-id", "a" + i));
  return nodes.map((n, idx) => {
    const r = n.getBoundingClientRect();
    const cs = getComputedStyle(n);
    // 🔴 「在视口内」也是可见性的一部分。
    // 实测踩出来的坑：能力面板（capwrap）收起时用 transform 移出屏幕右边，
    // 里面的「执行」按钮尺寸仍然是 28×28 ⇒ 只看尺寸会把它算成可点，
    // 于是每个按钮都变成「被遮住，点不到」，白白跳过 70 多个。
    // 移出视口的元素本来就没法点 ⇒ 不该进可点清单。
    //
    // 🔴 但「在视口内」要**按滚动容器算**，不能按窗口算。
    // 实测踩出来的坑（这一条是覆盖不足的假绿的第二层）：
    // 能力面板里 76 个「执行」按钮，只有 8 个在窗口视口内，
    // 其余 68 个在面板自己的**滚动容器**里（往下滚就能看到）。
    // 按窗口视口判 ⇒ 那 68 个被当成「不可见」静默跳过
    // ⇒ 报告写着「共枚举 76 个」，实际只测了 8 个 ⇒ **又是假绿**。
    // ⇒ 判据改成：元素在窗口视口内 **或** 在某个可滚动容器内
    //   （可滚动容器 = 祖先里有 overflow 是 auto/scroll 且能滚）。
    //   这样「点之前把它滚进来」才有意义。
    const inWindow = r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh;
    let scrollable = false;
    let p = n.parentElement;
    while (p && p !== document.body) {
      const pcs = getComputedStyle(p);
      if (/(auto|scroll)/.test(pcs.overflowY + " " + pcs.overflowX)) {
        // 容器本身能滚（scrollHeight > clientHeight）才算
        if (p.scrollHeight > p.clientHeight + 2) { scrollable = true; }
        break;
      }
      p = p.parentElement;
    }
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== "hidden" &&
                    cs.display !== "none" && cs.opacity !== "0" &&
                    (inWindow || scrollable);
    return {
      idx,
      auditId: "a" + idx,
      tag: n.tagName.toLowerCase(),
      id: n.id || "",
      cls: (n.className && typeof n.className === "string") ? n.className : "",
      text: (n.textContent || "").trim().slice(0, 24),
      title: n.getAttribute("title") || "",
      visible,
      inWindow,
      scrollable,
      disabled: !!n.disabled || n.getAttribute("aria-disabled") === "true",
      rect: { x: r.x + r.width / 2, y: r.y + r.height / 2 },
    };
  });
};

/* ------------------------------------------------------------------
 * 相位（phase）：界面有多个「层」——聊天视图、专家技能连接器视图、
 * 助理视图、能力面板、人格抽屉。
 *
 * 🔴 实测踩出来的坑（门禁骗人的第五种形态）：
 *   早期版本只扫「默认那一屏」。默认屏之外的东西（能力面板里的
 *   70 多个「执行」按钮、其它视图里的按钮）都是不可见的，
 *   于是被静默跳过 —— 报告写着「枚举 102 个」，实际只测了 20 个，
 *   却报「死链 0」。**覆盖不足的假绿比假红更坏**：它让
 *   「X0 死链清零」这个结论变得没有意义。
 *
 * 所以改成：每个相位把对应的那层**打开**，再扫一遍这一层里的可点元素。
 * 每个相位都会先枚举、再逐个点，点完恢复状态。
 *
 * 🔴 相位还要声明**只管哪一块**（scope）：
 *   抽屉是模态——打开后背景被遮罩盖住，背景上的元素**本来就点不到**
 *   （这不是死链，是模态的正常表现）。若不限定范围，人格抽屉相位
 *   会把侧栏、titlebar 等 30 多个背景元素全记成「被遮住，点不到」，
 *   报告被噪音淹没，还把覆盖统计算歪。
 *   ⇒ scope = 只枚举这一相位**该管**的元素；背景元素归其它相位管。
 *
 * 🔴 setup 必须走**真实入口**（点侧栏 / 点开面板），不能只改 class。
 *   实测踩出来的坑：早期 setup 只 `v.classList.toggle("on", …)`，
 *   于是「专家·技能·连接器」相位里 Tab 内容是**空的**
 *   （真正的内容是点击侧栏后 loadAssetTab() 拉接口渲染的）⇒
 *   那个相位只枚举到 4 个元素，覆盖率从 82% 掉到 31%。
 *   ⇒ 改成点真入口：点 `.sb-item[data-view=assets]` / `[data-view=crew]`，
 *   走 app.js 自己的 showView + setAssetTab + loadCrew。
 *
 * 🔴 setup 与 restore 要分开：setup 是**真入口**（会拉接口重渲染 DOM），
 *   只能在相位开始时做一次；restore 是**轻量复位**（只切 class，不重渲染），
 *   每个元素测完都要做——否则点侧栏切走视图后测不下去，
 *   而重渲染又会把 data-audit-id 标记全冲掉（元素就成「已不在 DOM」）。
 * ------------------------------------------------------------------ */
const PHASES = [
  {
    name: "聊天主视图",
    // 聊天相位管「全局骨架 + 聊天视图里的东西」
    scope: "#viewChat, .sidebar, .titlebar, .pet, .chatwrap, .composer, .topbar",
    setup: () => {
      const item = document.querySelector('.sb-item[data-view="chat"]');
      if (item) { item.click(); return; }
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.remove("open"));
    },
    restore: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
    },
  },
  {
    name: "专家·技能·连接器",
    scope: "#viewAssets",
    setup: () => {
      // 走真入口：点侧栏 ⇒ app.js 会 showView + setAssetTab（拉接口渲染内容）
      const item = document.querySelector('.sb-item[data-view="assets"]');
      if (item) { item.click(); }
      else {
        document.querySelectorAll(".view").forEach((v) => {
          v.classList.toggle("on", v.id === "viewAssets");
        });
      }
      // 🔴 tab 必须复位到「专家」：相位初始态不确定，就不知道本轮到底在测哪个 tab。
      // 实测踩出来的坑：跨相位残留让 ASSET_TAB 停在「连接器」时，
      // 卡片墙（含 16 个「切换到这张」）根本没渲染 ⇒ 覆盖率凭空掉一截。
      const on = document.querySelector("#tabBar .tab.on");
      if (on && on.getAttribute("data-tab") !== "cards") {
        const back = document.querySelector('#tabBar .tab[data-tab="cards"]');
        if (back) { back.click(); }
      }
    },
    restore: () => {
      // 轻量：只切视图显示，**不重渲染**（保住 data-audit-id 标记）
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewAssets");
      });
      // 🔴 例外：tab 也必须复位，否则**点击本身就毁掉了后续测量**。
      // 实测踩出来的坑（覆盖率从 81% 掉到 78% 的真因）：
      // 相位里 .tab（专家/技能/连接器/声音）本身就是要被测的元素，
      // 点「技能」「连接器」「声音」会把 #tabBody 整个换掉，
      // 卡片墙（含 16 个「切换到这张」）随之消失；而 restore 只切 .view，
      // 于是该相位后面的卡片按钮全被记成「元素已不在 DOM」。
      // 只在 tab 真被切走时才点回来（未切则不重渲染，标记仍保住）。
      const on = document.querySelector("#tabBar .tab.on");
      if (on && on.getAttribute("data-tab") !== "cards") {
        const back = document.querySelector('#tabBar .tab[data-tab="cards"]');
        if (back) { back.click(); }
      }
    },
  },
  {
    name: "助理·角色编队",
    scope: "#viewCrew",
    setup: () => {
      const item = document.querySelector('.sb-item[data-view="crew"]');
      if (item) { item.click(); return; }
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewCrew");
      });
    },
    restore: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewCrew");
      });
    },
  },
  {
    name: "能力面板",
    scope: ".capwrap",
    setup: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.add("open"));
    },
    restore: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.add("open"));
    },
  },
  {
    name: "人格抽屉",
    scope: "#drawerMask",
    setup: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.remove("open"));
      document.querySelectorAll(".apprmask").forEach((m) => { m.hidden = true; });
      const d = document.getElementById("drawerMask");
      if (d) { d.hidden = false; }
    },
    restore: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
      document.querySelectorAll(".apprmask").forEach((m) => { m.hidden = true; });
      const d = document.getElementById("drawerMask");
      if (d) { d.hidden = false; }
    },
  },
];

/* 元素归属：谁负责测这个元素。
 *
 * 🔴 为什么要有归属（实测踩出来的坑）：
 *   抽屉是**模态**——打开后背景被遮罩盖住，背景上的元素本来就点不到。
 *   若不限定范围，人格抽屉相位会把侧栏、titlebar 等 30 多个背景元素
 *   全记成「被遮住，点不到」——报告被噪音淹没，覆盖统计算歪，
 *   真正该测的抽屉内元素反而被淹了。
 *
 *   规则（**优先级从下往上，先匹配到的赢**）：
 *   ① 在某个模态里（.apprmask / .capwrap）⇒ 归那个模态；
 *   ② 在某个 view 里（#viewAssets / #viewCrew / #viewChat）⇒ 归那个 view；
 *   ③ 其余（侧栏、titlebar、悬浮球、toast）⇒ 归「聊天主视图」。
 *
 *   抽屉相位只测抽屉里的；背景元素**由聊天相位测**（那时抽屉没开）。
 *   这不是放水——是让每个元素在**它该被测的环境里**被测一次。 */

/* 把页面恢复到「点击前的样子」，供下一个元素测量用。
 *
 * 🔴 只清「点击产生的副作用」，**不动当前相位打开的那一层**。
 * 实测踩出来的坑（又是审计自己造的假象）：早期版本在这里把
 * `.view` 切回 viewChat，于是「专家·技能·连接器」相位里那三个 Tab
 * 明明在该相位是可见的，等到真去点它时已经被切走、变成隐藏 ⇒
 * elementFromPoint 落在 titlebar 上 ⇒ 报「被 titlebar 遮住」。
 * 那不是界面死链，是审计把自己的相位拆了。
 */
const RESTORE = () => {
  document.querySelectorAll(".apprmask").forEach((m) => { m.hidden = true; });
  document.querySelectorAll(".capwrap").forEach((c) => c.classList.remove("open"));
  document.querySelectorAll(".sidebar.collapsed").forEach((s) => {
    s.classList.remove("collapsed");
  });
  const s = document.getElementById("searchInput");
  if (s) { s.hidden = true; }
  const sr = document.getElementById("searchResults");
  if (sr) { sr.hidden = true; }
};

/* 页面指纹：任何可见变化都会体现在这里。
   ⚠️ 口径要点（都是实测踩出来的）：
   ① 只算「可见」元素的状态变化，但**把 data-theme 也算进去**——
      明暗切换只改 <html> 的 attribute，body.innerHTML 长度不变 ⇒ 漏判。
   ② 按钮的 disabled/class 变化要算。
   ③ **必须算可见文本的内容，不能只算长度**——
      实测漏判案例：两个 toast 文字等长（「附件上传还没做（不假装）」与
      「权限设置还没做（不假装）」都是 12 个字），
      第二个 toast 把第一个替换掉时，长度不变、innerHTML 长度不变 ⇒
      被判成「点了没反应」的假死链。加 content 哈希后不再误判。
   ④ **disabled 的按钮要单独放行**：空输入时的「发送」按钮是 disabled，
      点了当然没反应——那是正确行为，不是死链。 */
const FINGERPRINT = () => {
  const vis = Array.from(document.querySelectorAll("body *")).filter((n) => {
    const r = n.getBoundingClientRect();
    const cs = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && cs.display !== "none" && cs.visibility !== "hidden";
  });
  let textLen = 0;
  let textAll = "";
  vis.forEach((n) => {
    // 只取叶子节点的文本，避免祖先把子文本重复累加
    const own = Array.from(n.childNodes)
      .filter((c) => c.nodeType === 3).map((c) => c.nodeValue).join("");
    if (own) { textLen += own.length; textAll += own + "\u0001"; }
  });
  const states = Array.from(document.querySelectorAll("[disabled], .on, .open, [aria-expanded]"))
    .map((n) => n.className + "|" + n.getAttribute("aria-expanded") + "|" + n.disabled)
    .join(";");
  // 文本内容哈希：等长但内容不同 ⇒ 也能看出来
  let th = 5381;
  for (let i = 0; i < textAll.length; i++) {
    th = ((th * 33) ^ textAll.charCodeAt(i)) >>> 0;
  }
  const lastAct = document.body.getAttribute("data-last-act") || "";
  return {
    count: vis.length,
    textLen,
    textHash: th,
    html: document.body.innerHTML.length,
    theme: document.documentElement.getAttribute("data-theme") || "",
    states,
    lastAct,
    hash: [vis.length, textLen, th, document.body.innerHTML.length,
           document.documentElement.getAttribute("data-theme") || "", states, lastAct].join(":"),
  };
};

(async function main() {
  let srv = null;
  const dead = [], alive = [], skipped = [];
  try {
    console.log("[clickaudit] 起本地服务…");
    srv = await startServer();
    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    // 🔴 记录「点了之后有没有真的发请求出去」。
    // 实测踩出来的坑：能力面板里的「执行」按钮点下去是 POST /api/call，
    // 后端要真调一次能力（可能慢，比如探设备）。等固定时长看不到结果
    // 就被判成「点了页面无任何变化」的假死链。
    // ⇒ 用「请求确实发出去了」当独立证据：**点了就说明按钮是活的**，
    // 至于结果慢不慢是另一回事（那是「慢」，不是「死」）。
    let netHits = 0;
    page.on("request", (r) => { if (r.url().indexOf("/api/") >= 0) { netHits += 1; } });
    await page.goto(srv.url, { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);

    const items = await page.evaluate(ENUMERATE);
    console.log("[clickaudit] 枚举到可点元素 " + items.length + " 个（分 " +
                PHASES.length + " 个相位逐层扫）");

    // 🔴 定位元素**按 data-audit-id 标记重新查**，不用初始快照的下标。
    // 实测踩出来的坑：原先 `page.$$(sel)` 一次性取回全部句柄，
    // 之后按 handles[i] 使用。可相位里 DOM 一直在变（展开面板插新按钮 /
    // 换视图重建卡片 / 列表刷新换节点），下标就漂了 ⇒ 拿到的句柄
    // 根本不是本次要测的元素。症状很迷惑：三个 Tab 被报「被 titlebar 遮住」
    // （实测它们明明能点），#drawerRefresh 被报「元素已不在 DOM」。
    // 假绿比假红更坏：它会让「死链清零」这个结论失去意义。
    // 改成按标记重查后，元素真被删就如实记「元素已不在 DOM」。

    const phaseStats = [];
    for (const phase of PHASES) {
      // 打开这一层，再枚举。
      // 🔴 真实入口会**异步拉接口**渲染内容（Tab 卡片、名册、连接器墙），
      // 所以要多等一轮，等内容真的画出来再枚举——否则枚举到的是空壳。
      await page.evaluate(phase.setup);
      await page.waitForTimeout(900);
      const phItems = await page.evaluate(ENUMERATE, phase.name);
      // 🔴 给每个元素标「签名 + 同签名内序号」。
      // 用途：相位内一旦某次点击让列表**重渲染**（换角色卡 / 刷新卡片墙），
      // 先前打的 data-audit-id 全部作废。只按下标或只按 id 都定位不到，
      // 于是同类元素从第 2 个起全被记成「元素已不在 DOM」——
      // 实测「专家·技能·连接器」相位 19 个跳过里 14 个是这种**假跳过**，
      // 覆盖率被压到 81%，「死链 0」的可信度跟着掉。
      // 有了签名+序号，重渲染后仍能按 DOM 顺序把同一个元素找回来。
      const sigOf = (x) => x.tag + "|" + x.cls + "|" + x.text;
      (() => {
        const seen = new Map();
        phItems.forEach((x) => {
          const k = sigOf(x), n = seen.get(k) || 0;
          x.sigKey = k; x.sigOrd = n; seen.set(k, n + 1);
        });
      })();
      const visible = phItems.filter((x) => x.visible);
      const pAlive = [], pDead = [], pSkip = [];
      console.log("\n[clickaudit] 相位「" + phase.name + "」可见可点 " +
                  visible.length + " 个 / 共枚举 " + phItems.length + " 个");

      for (let i = 0; i < phItems.length; i++) {
        const it = phItems[i];
        if (!it.visible) { continue; }
        // disabled 的元素单独放行：它本来就是「现在不能点」，
        // 点了没反应是**正确行为**，不是死链。
        if (it.disabled) {
          pSkip.push({ ...it, phase: phase.name, why: "按钮是 disabled 状态（现在不能点，不是死链）" });
          continue;
        }
        let node = await page.$('[data-audit-id="' + it.auditId + '"]');
        if (!node) {
          // 🔴 重渲染后的补救（而不是直接记「已不在 DOM」）：
          // 先回到相位初始态，重新枚举一遍，再按「签名 + 同签名序号」
          // 把目标元素找回来，并顺手**重建整批标记**——否则后面每个元素
          // 都会因为旧标记作废而连环假跳过。
          // 实测：修前该相位 19 个跳过里 14 个是「切换到这张」，其实全健在可点。
          // 🔴 复位要用 setup 而不是 restore：restore 只是轻量切 class，
          // 而「切换到这张」走的是 useCard → loadAssetTab("cards")，
          // 卡片墙会**重新拉接口异步渲染**。等 300ms 时枚举到的是空壳，
          // 于是 14 个按钮全被判「按签名也找不回」。等够 900ms 才拿得到真列表。
          await page.evaluate(RESTORE);
          await page.evaluate(phase.setup);
          // 重试前确认 tab 已在「专家」（卡片墙所在 tab），否则重枚举注定为空壳
          await page.evaluate(() => {
            const on = document.querySelector("#tabBar .tab.on");
            if (on && on.getAttribute("data-tab") !== "cards") {
              const back = document.querySelector('#tabBar .tab[data-tab="cards"]');
              if (back) { back.click(); }
            }
          });
          // 🔴 「切换到这张」走 useCard → loadAssetTab("cards")，
          // 而 useCard 先把 ASSET_CACHE.cards 置空、再重新拉接口。
          // 固定等 900ms 时接口还没回来 ⇒ 卡片墙是空的（实测重枚举只剩 12 个、
          // 「切换到这张」0 个）⇒ 又被判「按签名也找不回」。
          // ⇒ 改成**轮询到目标签名重新出现**，最多等 6 秒，不猜固定时长。
          const sigBuild = (arr) => {
            const m = new Map();
            arr.forEach((x) => {
              const k = sigOf(x);
              if (!m.has(k)) { m.set(k, []); }
              m.get(k).push(x);
            });
            return m;
          };
          let reItems = [], buckets = sigBuild([]);
          const reDeadline = Date.now() + 6000;
          for (;;) {
            await page.waitForTimeout(500);
            reItems = await page.evaluate(ENUMERATE, phase.name);
            buckets = sigBuild(reItems);
            if (buckets.has(it.sigKey) || Date.now() > reDeadline) { break; }
          }
          let rebuilt = 0;
          phItems.forEach((x) => {
            const arr = buckets.get(x.sigKey) || [];
            const t = arr[x.sigOrd];
            if (t) {
              x.auditId = t.auditId; x.rect = t.rect; x.visible = t.visible;
              x.disabled = t.disabled; x.gone = false; rebuilt++;
            } else {
              x.gone = true;
            }
          });
          if (it.gone || !it.visible) {
            pSkip.push({ ...it, phase: phase.name,
                         why: "元素已不在 DOM（重渲染后按签名也找不回）" });
            continue;
          }
          node = await page.$('[data-audit-id="' + it.auditId + '"]');
          if (!node) {
            pSkip.push({ ...it, phase: phase.name,
                         why: "元素已不在 DOM（重建标记后仍取不到句柄）" });
            continue;
          }
          console.log("  · 重渲染后按签名找回：「" + it.text + "」(重建 " + rebuilt + " 个标记)");
        }
        // 🔴 「已经是当前选中项」的元素点下去**本来就不该有变化**。
        // 实测案例：三 Tab 里「专家」是默认激活的那个（class="tab on"），
        // 点它页面当然不变 ⇒ 被判成死链。**那是正确行为，不是死链。**
        // 判据：带 .on / .active / [aria-selected=true] / [aria-current] 的元素。
        const isCurrent = await node.evaluate((n) => {
          const cs = n.classList || "";
          return /(^|\s)(on|active|selected|current)(\s|$)/.test(cs) ||
                 n.getAttribute("aria-selected") === "true" ||
                 n.getAttribute("aria-current") === "true";
        });
        if (isCurrent) {
          pSkip.push({ ...it, phase: phase.name, why: "已经是当前选中项（点了没变化是正确行为，不是死链）" });
          continue;
        }
        // 点之前：把目标滚进视野，并确认「鼠标那个点上确实命中的是它」——
        // 否则点到的是别的东西（被面板盖住 / 被滚出视口），判出来的「没反应」
        // 是**我们点歪了**，不是界面死链。这种要单独记，不算死链。
        //
        // 🔴 滚动方式要讲究：block:"center" 会把元素顶到视口正中，
        // 而顶部那条 titlebar（高 38px，z-index 21）正好盖在上面
        // ⇒ 明明可见的按钮被判成「被 titlebar 遮住」。
        // 那是**审计工具自己造的假遮挡**，不是界面死链。
        // 改用 block:"nearest"：已经在视野内就不动它。
        //
        // 🔴 但「nearest」对**滚动容器内**的元素不够：它只保证元素在自己
        // 那个滚动容器里露出来，不保证露出来的那部分**没被别的层盖住**。
        // 能力面板的 68 个「执行」按钮就在这种容器里。
        // ⇒ 先把元素滚到**容器可视区的中间**，再判遮挡。
        const hit = await node.evaluate((node) => {
          // 找到最近的可滚动祖先，让元素滚到它可视区的中间
          let sc = null, p = node.parentElement;
          while (p && p !== document.body) {
            const pcs = getComputedStyle(p);
            if (/(auto|scroll)/.test(pcs.overflowY + " " + pcs.overflowX) &&
                p.scrollHeight > p.clientHeight + 2) { sc = p; break; }
            p = p.parentElement;
          }
          if (sc) {
            const nr = node.getBoundingClientRect(), sr = sc.getBoundingClientRect();
            sc.scrollTop += (nr.top + nr.height / 2) - (sr.top + sr.height / 2);
            // 滚动后必须让浏览器把布局算完
            void sc.offsetHeight;
          } else {
            node.scrollIntoView({ block: "nearest", inline: "nearest" });
          }
          const r = node.getBoundingClientRect();
          const x = r.x + r.width / 2, y = r.y + r.height / 2;
          const top = document.elementFromPoint(x, y);
          return {
            ok: true, x, y,
            covered: !(top === node || node.contains(top) || (top && top.contains(node))),
            coveredBy: top ? (top.tagName + "." + (typeof top.className === "string" ? top.className : "")) : "",
            scrolled: !!sc,
          };
        });
        if (!hit.covered) {
          const before = await page.evaluate(FINGERPRINT);
          const netBefore = netHits;
          try {
            await page.mouse.click(hit.x, hit.y, { timeout: 1500 });
          } catch (e) {
            pDead.push({ ...it, phase: phase.name, why: "点击报错：" + e.message });
            continue;
          }
          // 🔴 等「有反应」要**轮询**，不能只等一个固定时长。
          // 实测踩出来的坑：能力面板里的「执行」按钮点下去是
          // POST /api/call（异步）⇒ 审批弹窗要过一会儿才出现。
          // 固定等 350ms 时弹窗还没来 ⇒ 指纹没变 ⇒ 被判成
          // 「点了页面无任何变化」的假死链。
          // 实测：等 1500ms 就能看到 body 长度从 33794 变成 34059。
          // ⇒ 改成：先等一小会儿，之后**轮询到指纹变了就停**，
          // 最多等 ASYNC_WAIT。变了一定算有反应；到点还没变才算死链。
          let changed = false;
          const deadline = Date.now() + ASYNC_WAIT;
          while (Date.now() < deadline) {
            await page.waitForTimeout(SETTLE);
            const after = await page.evaluate(FINGERPRINT);
            if (before.hash !== after.hash) { changed = true; break; }
          }
          // 🔴 「发了请求」也是**独立的活性证据**。
          // 能力面板的「执行」按钮点下去是 POST /api/call，后端要真调一次
          // 能力（探设备可能要好几秒）⇒ 等 2 秒页面还没变。
          // 但**请求确实发出去了** ⇒ 按钮是活的，慢 ≠ 死。
          // 判死链的前提是：既没等到页面变化，也没发出任何请求。
          const netFired = netHits > netBefore;
          if (changed) {
            pAlive.push({ ...it, phase: phase.name, note: "有反应" });
          } else if (netFired) {
            pAlive.push({ ...it, phase: phase.name, note: "有反应（发出请求了，结果还没回来——慢，不是死）" });
          } else {
            pDead.push({ ...it, phase: phase.name, why: "点了页面无任何变化（等了 " +
                        ASYNC_WAIT + "ms 也没变，也没发出任何请求）" });
          }
        } else {
          // 被别的层盖住 —— 记为「被遮挡（需先关掉覆盖层）」，不算死链。
          pSkip.push({ ...it, phase: phase.name, why: "被 " + hit.coveredBy + " 遮住，点不到" });
        }
        // 🔴 点完把页面恢复到初始状态，再测下一个。
        // 实测踩出来的坑：点「收起左栏」之后左栏整个隐藏，
        // 后面每一个元素都变成「点不到」⇒ 审计把 80 多个元素全记成
        // 「被遮住」，最后报「死链 0」——那是**假绿**。
        // 一次点击污染后续所有测量，比漏判更坏。
        //
        // 🔴 恢复要**两步**：清副作用 + 重新打开相位那一层。
        // 实测踩出来的坑（这一条比上一条更阴）：RESTORE 只清了抽屉/面板/左栏，
        // **没把视图切回本相位的那一层**。而相位里枚举的可点元素包含侧栏
        // `.sb-item`——点它会真的切视图（切到聊天/助理/定时任务…），
        // 于是该相位后续所有元素都在**错误的视图**里测：
        // 「专家·技能·连接器」的三个 Tab 于是变成不可见，
        // elementFromPoint 落在 titlebar 上 ⇒ 报「被 titlebar 遮住」。
        // 实测证明那是假象：Tab 的 y=94，titlebar 只到 y=38，命中的是它自己。
        await page.evaluate(RESTORE);
        await page.evaluate(phase.restore);
        await page.waitForTimeout(150);
      }
      alive.push(...pAlive);
      dead.push(...pDead);
      skipped.push(...pSkip);
      phaseStats.push({ name: phase.name, total: phItems.length, visible: visible.length,
                        alive: pAlive.length, dead: pDead.length, skipped: pSkip.length });
      // 相位结束，回到默认态
      await page.evaluate(RESTORE);
      await page.waitForTimeout(150);
    }

    console.log("\n[clickaudit] 有反应 " + alive.length + " 个 / 死链 " + dead.length +
                " 个 / 跳过 " + skipped.length + " 个");
    console.log("\n[clickaudit] 分相位覆盖：");
    phaseStats.forEach((p) => {
      console.log("  - " + p.name.padEnd(22) + " 可见 " + String(p.visible).padStart(3) +
                  "  有反应 " + String(p.alive).padStart(3) +
                  "  死链 " + String(p.dead).padStart(3) +
                  "  跳过 " + String(p.skipped).padStart(3));
    });
    // 🔴 覆盖度必须一起报出来。
    // 「死链 0」如果是在只测了 20/102 的情况下得出的，那是假绿。
    // 门禁红绿要能一眼看出「测全了没有」。
    //
    // 🔴 分母用「各相位枚举到的元素数之和」，**不是**首次那个全量 items.length。
    // 实测踩出来的坑：加了「元素归属」后，同一个元素只由一个相位负责，
    // 各相位的 phItems 互不重叠 ⇒ 相加才是真正该测的总数。
    // 用全量 items.length 当分母会算出一个**永远到不了**的覆盖率
    // （归属过滤后只测 32 个，分母却是 103 ⇒ 31%，看着像漏测一大半）。
    const covered = alive.length + dead.length;
    const enumerated = phaseStats.reduce((a, p) => a + p.total, 0);
    const pct = enumerated ? Math.round(covered / enumerated * 100) : 0;
    console.log("[clickaudit] 覆盖：" + covered + " / " + enumerated +
                " 个该测元素被真点过（" + pct + "%）");
    // 🔴 覆盖率不到 90% 要**如实说**，不能默默报「死链 0」让人以为测全了。
    if (pct < 90) {
      console.log("[clickaudit] ⚠️ 覆盖率 " + pct + "% 不到 90% —— 「死链 0」只覆盖了这些，" +
                  "其余没测到的不能算「没问题」");
    }
    if (skipped.length) {
      console.log("\n  跳过（不是死链，是点不到/不可见）：");
      skipped.forEach((d) => {
        console.log("  -  " + (d.id ? "#" + d.id : "." + d.cls.split(" ")[0]) +
                   "  <" + d.tag + ">  \"" + d.text + "\"  [" + (d.phase || "") + "]  " + d.why);
      });
    }
    if (dead.length) {
      console.log("\n  死链清单：");
      dead.forEach((d) => {
        console.log("  x  " + (d.id ? "#" + d.id : "." + d.cls.split(" ")[0]) +
                   "  <" + d.tag + ">  \"" + d.text + "\"  " + d.title +
                   "  —— " + d.why);
      });
    }
    if (JSON_OUT) {
      fs.writeFileSync(JSON_OUT, JSON.stringify(
        { alive, dead, skipped, phases: phaseStats,
          coverage: { enumerated, clicked: covered, pct } },
        null, 2));
      console.log("\n[clickaudit] 明细已存 " + JSON_OUT);
    }
    await browser.close();
    if (dead.length) { process.exitCode = 1; }
  } catch (e) {
    console.log("  x  clickaudit 失败：" + (e && e.message ? e.message : e));
    process.exitCode = 1;
  } finally {
    if (srv && srv.proc) { try { srv.proc.kill(); } catch (_) {} }
  }
})();
