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
const ENUMERATE = () => {
  const sel = "a[href], button, [role=button], [onclick], .hbtn, .cbtn, .sb-item, .sb-foot, .tb-win i";
  const nodes = Array.from(document.querySelectorAll(sel));
  return nodes.map((n, idx) => {
    const r = n.getBoundingClientRect();
    const cs = getComputedStyle(n);
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== "hidden" &&
                    cs.display !== "none" && cs.opacity !== "0";
    return {
      idx,
      tag: n.tagName.toLowerCase(),
      id: n.id || "",
      cls: (n.className && typeof n.className === "string") ? n.className : "",
      text: (n.textContent || "").trim().slice(0, 24),
      title: n.getAttribute("title") || "",
      visible,
      rect: { x: r.x + r.width / 2, y: r.y + r.height / 2 },
    };
  });
};

/* 页面指纹：任何可见变化都会体现在这里。
   ⚠️ 口径要点：只算「可见」元素的状态变化，但**把 data-theme 也算进去**——
   明暗切换只改 <html> 的 attribute，body.innerHTML 长度不变 ⇒ 漏判。
   同理按钮的 disabled/class 变化要算。 */
const FINGERPRINT = () => {
  const vis = Array.from(document.querySelectorAll("body *")).filter((n) => {
    const r = n.getBoundingClientRect();
    const cs = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && cs.display !== "none" && cs.visibility !== "hidden";
  });
  let textLen = 0;
  vis.forEach((n) => { textLen += (n.textContent || "").length; });
  const states = Array.from(document.querySelectorAll("[disabled], .on, .open, [aria-expanded]"))
    .map((n) => n.className + "|" + n.getAttribute("aria-expanded") + "|" + n.disabled)
    .join(";");
  return {
    count: vis.length,
    textLen,
    html: document.body.innerHTML.length,
    theme: document.documentElement.getAttribute("data-theme") || "",
    states,
    hash: [vis.length, textLen, document.body.innerHTML.length,
           document.documentElement.getAttribute("data-theme") || "", states].join(":"),
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
    console.log("[clickaudit] 枚举到可点元素 " + items.length + " 个");

    for (const it of items) {
      if (!it.visible) { continue; }
      // 点之前：把目标滚进视野，并确认「鼠标那个点上确实命中的是它」——
      // 否则点到的是别的东西（被面板盖住 / 被滚出视口），判出来的「没反应」
      // 是**我们点歪了**，不是界面死链。这种要单独记，不算死链。
      const hit = await page.evaluate((i) => {
        const sel = "a[href], button, [role=button], [onclick], .hbtn, .cbtn, .sb-item, .sb-foot, .tb-win i";
        const node = document.querySelectorAll(sel)[i.idx];
        if (!node) { return { ok: false, why: "元素已不在 DOM" }; }
        node.scrollIntoView({ block: "center", inline: "center" });
        const r = node.getBoundingClientRect();
        const x = r.x + r.width / 2, y = r.y + r.height / 2;
        const top = document.elementFromPoint(x, y);
        return {
          ok: true, x, y,
          covered: !(top === node || node.contains(top) || (top && top.contains(node))),
          coveredBy: top ? (top.tagName + "." + (typeof top.className === "string" ? top.className : "")) : "",
        };
      }, it);
      if (!hit.ok) { skipped.push({ ...it, why: hit.why }); continue; }
      if (hit.covered) {
        // 被别的层盖住 —— 记为「被遮挡（需先关掉覆盖层）」，不算死链。
        skipped.push({ ...it, why: "被 " + hit.coveredBy + " 遮住，点不到" });
        continue;
      }
      const before = await page.evaluate(FINGERPRINT);
      try {
        await page.mouse.click(hit.x, hit.y, { timeout: 1500 });
      } catch (e) {
        dead.push({ ...it, why: "点击报错：" + e.message });
        continue;
      }
      await page.waitForTimeout(SETTLE);
      const after = await page.evaluate(FINGERPRINT);
      const changed = before.hash !== after.hash;
      if (changed) {
        alive.push({ ...it, note: "有反应" });
      } else {
        dead.push({ ...it, why: "点了页面无任何变化" });
      }
    }

    console.log("\n[clickaudit] 有反应 " + alive.length + " 个 / 死链 " + dead.length +
                " 个 / 跳过 " + skipped.length + " 个");
    if (skipped.length) {
      console.log("\n  跳过（不是死链，是点不到/不可见）：");
      skipped.forEach((d) => {
        console.log("  -  " + (d.id ? "#" + d.id : "." + d.cls.split(" ")[0]) +
                   "  <" + d.tag + ">  \"" + d.text + "\"  " + d.why);
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
      fs.writeFileSync(JSON_OUT, JSON.stringify({ alive, dead, skipped }, null, 2));
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
