/* ==========================================================================
   角色卡商店的 UI 实测：真浏览器点一遍，看「有没有反应」。

   为什么单独写一个而不复用 clickaudit.js：
     clickaudit 查的是**死链**（点了没反应）。
     这里查的是**新功能真能用**：搜卡 → 出结果 → 点下载 → 审批 → 入库 → 能切。
     而且下载要过审批闸门，得**自己点确认**才能走完——clickaudit 不会替人批。

   跑法（仓库根）：
     node tools/uicheck/cardstore_ui.js
     node tools/uicheck/cardstore_ui.js --shots     # 顺便存截图

   退出码：0 全过；1 有失败项。
   ========================================================================== */
"use strict";

const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const { chromium } = require("playwright");

const ROOT = path.join(__dirname, "..", "..");
const PY = "D:/tools/python/python.exe";
const SHOTS = path.join(__dirname, "shots");
const WANT_SHOTS = process.argv.includes("--shots");

let pass = 0, fail = 0;
const failures = [];

function ok(cond, label) {
  if (cond) { pass++; console.log("  ok  " + label); }
  else { fail++; failures.push(label); console.log("  FAIL " + label); }
}
function say(s) { console.log(s); }

/* 起本地服务（127.0.0.1 + 随机端口 + 一次性令牌） */
function startServer() {
  return new Promise((resolve, reject) => {
    const p = spawn(PY, ["-m", "fleet.gui.app", "--no-window", "--port", "0"], {
      cwd: ROOT, env: Object.assign({}, process.env, { PYTHONIOENCODING: "utf-8" })
    });
    let buf = "";
    const timer = setTimeout(() => reject(new Error("服务 20 秒没起来")), 20000);
    p.stdout.on("data", (d) => {
      buf += d.toString();
      const m = buf.match(/页面地址（片段里是一次性令牌）：(\S+)/);
      if (m) { clearTimeout(timer); resolve({ proc: p, url: m[1] }); }
    });
    p.stderr.on("data", (d) => process.stderr.write("[server] " + d.toString()));
    p.on("exit", (c) => { clearTimeout(timer); reject(new Error("服务退出 rc=" + c)); });
  });
}

(async () => {
  const srv = await startServer();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  try {
    await page.goto(srv.url, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);

    /* ---- ① 先切到「专家」视图，再点专家 Tab ----
       🔴 左栏的 .sb-item 才是视图入口，#tabBar 在 viewAssets 里，
          默认不可见 —— 不先切视图就点 Tab，Playwright 会等元素可见直到超时。 */
    const nav = await page.$('.sb-item[data-view="assets"]');
    ok(!!nav, "左栏有「专家」导航项");
    if (nav) { await nav.click(); await page.waitForTimeout(700); }

    const tabs = await page.$$("#tabBar .tab");
    let storePanel = null;
    for (const t of tabs) {
      const label = (await t.textContent()) || "";
      if (label.indexOf("专家") >= 0) { await t.click(); break; }
    }
    await page.waitForTimeout(1200);
    storePanel = await page.$(".store");
    ok(!!storePanel, "专家 Tab 里有卡库商店面板");

    /* ---- ② 本地卡库有「切换到这张」按钮 ---- */
    const useBtns = await page.$$(".card .cmeta .hbtn.go");
    ok(useBtns.length >= 1, "本地卡库有「切换到这张」按钮（" + useBtns.length + " 个）");

    /* ---- ③ 搜索框在，源下拉在 ---- */
    const inp = await page.$(".storebar .storeinput");
    const sel = await page.$(".storebar .storesel");
    ok(!!inp, "有搜索框");
    ok(!!sel, "有卡源下拉");

    /* ---- ④ 真搜一次（联网），出结果 ---- */
    let items = [];
    if (inp) {
      await inp.fill("sister");
      await inp.press("Enter");
      await page.waitForTimeout(4000);
      items = await page.$$(".storeres .wall .card");
      ok(items.length > 0, "搜「sister」出结果（" + items.length + " 张）");
    }

    /* ---- ⑤ 点下载 → 应当弹审批（副作用能力要主人点头）---- */
    let approved = false;
    if (items.length) {
      const dl = await items[0].$(".cmeta .hbtn.go");
      if (dl) {
        await dl.click();
        await page.waitForTimeout(1500);
        // 审批面板里点「同意」
        const yes = await page.$(".apprrow .hbtn.yes");
        ok(!!yes, "点下载后弹出了审批（副作用能力要主人点头）");
        if (yes) {
          await yes.click();
          // 🔴 审批通过后后端会重绘专家 Tab ⇒ 旧的 elementHandle 全部失效。
          //    后续查询必须**重新拿**，不能复用旧引用（"Element is not
          //    attached to the DOM"）。
          await page.waitForTimeout(12000);   // 下载 1MB 左右要一会儿
          approved = true;
        }
      } else {
        ok(false, "结果卡片上有下载按钮");
      }
    }

    /* ---- ⑥ 下载后库里多了一张卡 ---- */
    if (approved) {
      await page.waitForTimeout(2000);
      const cards = await page.$$(".wall .card");
      const texts = [];
      for (const c of cards) { texts.push((await c.textContent()) || ""); }
      const joined = texts.join(" | ");
      ok(/切换到这张/.test(joined), "下载后本地库里出现新卡（带切换按钮）");
      ok(/store_/.test(joined), "新卡的文件名带 store_ 前缀（外来的卡，可被 git 忽略）");
    }

    /* ---- ⑦ 截图（可选）---- */
    if (WANT_SHOTS) {
      if (!fs.existsSync(SHOTS)) { fs.mkdirSync(SHOTS, { recursive: true }); }
      await page.screenshot({ path: path.join(SHOTS, "cardstore.png"), fullPage: false });
      say("  截图：tools/uicheck/shots/cardstore.png");
    }
  } catch (e) {
    fail++; failures.push("异常：" + e.message);
    say("  异常：" + e.stack);
  } finally {
    await browser.close();
    srv.proc.kill();
  }

  say("");
  say("=== 卡库 UI 实测：" + pass + " 通过 / " + fail + " 失败 ===");
  if (failures.length) { say("失败项："); failures.forEach((f) => say("  - " + f)); }
  process.exit(fail ? 1 : 0);
})();
