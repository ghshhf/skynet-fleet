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
const PY = "D:/tools/python/python.exe";

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  if (i < 0) { return dflt; }
  const v = process.argv[i + 1];
  if (!v || v.startsWith("--")) { return true; }
  return v;
}
const THEME = arg("theme", "light");
const SETTLE = Number(arg("settle", 350));
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

const ENUMERATE = () => {
  const sel = "a[href], button, [role=button], [onclick], .hbtn, .cbtn, .sb-item, .sb-foot, .tb-win i";
  const nodes = Array.from(document.querySelectorAll(sel));
  const vw = window.innerWidth, vh = window.innerHeight;
  return nodes.map((n, idx) => {
    const r = n.getBoundingClientRect();
    const cs = getComputedStyle(n);
    // 🔴 「在视口内」也是可见性的一部分。
    // 实测踩出来的坑：能力面板（capwrap）收起时用 transform 移出屏幕右边，
    // 里面的「执行」按钮尺寸仍然是 28×28 ⇒ 只看尺寸会把它算成可点，
    // 于是每个按钮都变成「被遮住，点不到」，白白跳过 70 多个。
    // 移出视口的元素本来就没法点 ⇒ 不该进可点清单。
    const inViewport = r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh;
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== "hidden" &&
                    cs.display !== "none" && cs.opacity !== "0" && inViewport;
    return {
      idx,
      tag: n.tagName.toLowerCase(),
      id: n.id || "",
      cls: (n.className && typeof n.className === "string") ? n.className : "",
      text: (n.textContent || "").trim().slice(0, 24),
      title: n.getAttribute("title") || "",
      visible,
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
 * ------------------------------------------------------------------ */
const PHASES = [
  {
    name: "聊天主视图",
    setup: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.remove("open"));
    },
  },
  {
    name: "专家·技能·连接器",
    setup: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewAssets");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.remove("open"));
    },
  },
  {
    name: "助理·角色编队",
    setup: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewCrew");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.remove("open"));
    },
  },
  {
    name: "能力面板",
    setup: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.add("open"));
    },
  },
  {
    name: "人格抽屉",
    setup: () => {
      document.querySelectorAll(".view").forEach((v) => {
        v.classList.toggle("on", v.id === "viewChat");
      });
      document.querySelectorAll(".capwrap").forEach((c) => c.classList.remove("open"));
      document.querySelectorAll(".apprmask").forEach((m) => { m.hidden = true; });
      const d = document.getElementById("drawerMask");
      if (d) { d.hidden = false; }
    },
  },
];

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
    await page.goto(srv.url, { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);

    const items = await page.evaluate(ENUMERATE);
    console.log("[clickaudit] 枚举到可点元素 " + items.length + " 个（分 " +
                PHASES.length + " 个相位逐层扫）");

    // 🔴 用 ElementHandle 定位，**不用索引**。
    // 实测踩出来的坑：按 `querySelectorAll(sel)[idx]` 重新查，
    // 一旦前面某次点击改变了 DOM（展开面板 / 收起左栏 / 换视图），
    // 索引就漂移到别的元素上 ⇒ 点到的完全是另一个东西，
    // 判出来的「被 titlebar 遮住」是**我们自己点歪了**，不是界面死链。
    // 假绿比假红更坏：它会让「死链清零」这个结论失去意义。
    const handles = await page.$$(CLICKABLE_SEL);

    const phaseStats = [];
    for (const phase of PHASES) {
      // 打开这一层，再枚举
      await page.evaluate(phase.setup);
      await page.waitForTimeout(300);
      const phItems = await page.evaluate(ENUMERATE);
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
        const node = handles[i];
        if (!node) {
          pSkip.push({ ...it, phase: phase.name, why: "元素已不在 DOM" });
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
        const hit = await node.evaluate((node) => {
          node.scrollIntoView({ block: "nearest", inline: "nearest" });
          const r = node.getBoundingClientRect();
          const x = r.x + r.width / 2, y = r.y + r.height / 2;
          const top = document.elementFromPoint(x, y);
          return {
            ok: true, x, y,
            covered: !(top === node || node.contains(top) || (top && top.contains(node))),
            coveredBy: top ? (top.tagName + "." + (typeof top.className === "string" ? top.className : "")) : "",
          };
        });
        if (!hit.covered) {
          const before = await page.evaluate(FINGERPRINT);
          try {
            await page.mouse.click(hit.x, hit.y, { timeout: 1500 });
          } catch (e) {
            pDead.push({ ...it, phase: phase.name, why: "点击报错：" + e.message });
            continue;
          }
          await page.waitForTimeout(SETTLE);
          const after = await page.evaluate(FINGERPRINT);
          if (before.hash !== after.hash) {
            pAlive.push({ ...it, phase: phase.name, note: "有反应" });
          } else {
            pDead.push({ ...it, phase: phase.name, why: "点了页面无任何变化" });
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
        await page.evaluate(RESTORE);
        await page.waitForTimeout(120);
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
    const covered = alive.length + dead.length;
    const enumerated = items.length;
    console.log("[clickaudit] 覆盖：" + covered + " / " + enumerated + " 个可点元素被真点过" +
                "（" + (enumerated ? Math.round(covered / enumerated * 100) : 0) + "%）");
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
          coverage: { enumerated: items.length, clicked: alive.length + dead.length } },
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
