/* ==========================================================================
   图形版的「真截图」工具 —— 把「界面长什么样」从描述变成可比对的证据。

   为什么需要它（这是本轮实测出来的真缺口）：
     · 以前验收界面靠人眼描述 + 一张拍出来的图，**没法判定「改好了没有」**；
     · 改完 UI 只能「感觉比上次好」，没有回归基线 ⇒ 改 A 弄坏 B 查不出来；
     · 截图靠手工开窗口 + 截图工具，**不可重复**，也没法进 CI。

   这个脚本干三件事：
     ① 起本地服务（127.0.0.1 + 随机端口 + 一次性令牌），用 Playwright 的
        Chromium 以 1440×900 真渲染（和 pywebview 窗口同尺寸）；
     ② 截图存到 tools/uicheck/baseline/ 或 tools/uicheck/current/；
     ③ 可选：与基线做**像素级**对比（pixelmatch），输出差异图 + 差异百分比。

   用法（在本目录或仓库根）：
     node tools/uicheck/shot.js --out current
     node tools/uicheck/shot.js --out current --diff      # 与 baseline 比
     node tools/uicheck/shot.js --theme dark --out current

   退出码：0 全过；1 有失败项（对比超阈值 / 截图失败）。
   ========================================================================== */
"use strict";

const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const { chromium } = require("playwright");

const ROOT = path.join(__dirname, "..", "..");
const OUTDIR = path.join(__dirname, process.env.FLEET_SHOT_DIR || "shots");
const PY = "D:/tools/python/python.exe";   // 本机可用的 python（系统 python 是存根）

/* ------------------------------------------------------------------ 参数 */
function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  if (i < 0) { return dflt; }
  const v = process.argv[i + 1];
  if (!v || v.startsWith("--")) { return true; }
  return v;
}
const THEME = arg("theme", "light");
const OUTNAME = arg("out", "current");
const DO_DIFF = !!arg("diff", false);
const THRESHOLD = Number(arg("threshold", 0.02));   // 差异像素占比上限
const WIDTH = Number(arg("width", 1440));
const HEIGHT = Number(arg("height", 900));

/* ------------------------------------------------------------------ 输出 */
function say(s) { console.log(s); }
function fail(msg) {
  console.log("  x  " + msg);
  process.exitCode = 1;
}

/* ------------------------------------------- 起服务：拿 URL（含一次性令牌） */
function startServer() {
  return new Promise((resolve, reject) => {
    const code = [
      "import sys; sys.path.insert(0, r'" + ROOT.replace(/\\/g, "\\\\") + "')",
      "from fleet.gui.server import FleetServer",
      "from fleet.gui.approval import ApprovalBroker",
      "s = FleetServer(approval=ApprovalBroker()); s.start()",
      "print('FLEET_URL=' + s.page_url(r'" + THEME + "'), flush=True)",
      "import time",
      "time.sleep(600)",
    ].join("\n");
    const p = spawn(PY, ["-c", code], { cwd: ROOT });
    let buf = "";
    let done = false;
    const timer = setTimeout(() => {
      if (!done) { reject(new Error("起服务超时（20 秒没等到 URL）")); }
    }, 20000);
    p.stdout.on("data", (d) => {
      buf += d.toString();
      const m = buf.match(/FLEET_URL=(\S+)/);
      if (m && !done) {
        done = true;
        clearTimeout(timer);
        resolve({ url: m[1], proc: p });
      }
    });
    p.stderr.on("data", (d) => {
      const t = d.toString();
      if (/Traceback|Error/.test(t)) { say("  (服务 stderr) " + t.split("\n")[0]); }
    });
    p.on("error", (e) => { clearTimeout(timer); reject(e); });
  });
}

/* ------------------------------------------------------------------ 主流程 */
(async function main() {
  const { chromium: _c } = require("playwright");
  let srv = null;
  try {
    say("[uicheck] 起本地服务…");
    srv = await startServer();
    say("[uicheck] " + srv.url.split("#")[0] + "（令牌在片段里）");

    const browser = await chromium.launch();
    const ctx = await browser.newContext({
      viewport: { width: WIDTH, height: HEIGHT },
      deviceScaleFactor: 1,
    });
    const page = await ctx.newPage();

    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error") { errors.push("console.error: " + m.text()); }
    });

    await page.goto(srv.url, { waitUntil: "networkidle" });
    // 等首屏数据到位（能力清单/设备列表是真接口来的，得等它回来）
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(OUTDIR, OUTNAME + ".png") });
    say("[uicheck] 截图已存 " + path.join(OUTDIR, OUTNAME + ".png"));

    if (errors.length) {
      errors.forEach((e) => fail("页面报错：" + e));
    } else {
      say("  ok  页面无 JS 错误");
    }

    /* --- 像素级对比（可选） --- */
    if (DO_DIFF) {
      const basePath = path.join(OUTDIR, "baseline.png");
      if (!fs.existsSync(basePath)) {
        fail("没有基线图 " + basePath + "（先跑一次 --out baseline）");
      } else {
        const pixelmatch = require("pixelmatch");
        const { PNG } = require("pngjs");
        const a = PNG.sync.read(fs.readFileSync(basePath));
        const b = PNG.sync.read(fs.readFileSync(path.join(OUTDIR, OUTNAME + ".png")));
        if (a.width !== b.width || a.height !== b.height) {
          fail("尺寸不同：基线 " + a.width + "x" + a.height +
               " vs 当前 " + b.width + "x" + b.height);
        } else {
          const diff = new PNG({ width: a.width, height: a.height });
          const n = pixelmatch(a.data, b.data, diff.data, a.width, a.height,
                              { threshold: 0.1 });
          const total = a.width * a.height;
          const ratio = n / total;
          fs.writeFileSync(path.join(OUTDIR, "diff.png"), PNG.sync.write(diff));
          say("[uicheck] 差异像素 " + n + " / " + total +
              " = " + (ratio * 100).toFixed(3) + "%（阈值 " +
              (THRESHOLD * 100).toFixed(2) + "%）");
          if (ratio > THRESHOLD) {
            fail("差异超阈值，diff.png 已存 " + path.join(OUTDIR, "diff.png"));
          } else {
            say("  ok  在阈值内");
          }
        }
      }
    }

    await browser.close();
  } catch (e) {
    fail("uicheck 失败：" + (e && e.message ? e.message : e));
  } finally {
    if (srv && srv.proc) { try { srv.proc.kill(); } catch (_) {} }
  }
})();
