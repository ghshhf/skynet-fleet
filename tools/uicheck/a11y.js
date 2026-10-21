/* ==========================================================================
   图形版的「无障碍 + 对比度」体检 —— 用 axe-core 真扫，不靠肉眼。

   为什么需要它：
     暗色主题、浅灰正文、10~11px 小字，这些是最容易「看着还行、实际看不清」的地方。
     肉眼在 1440×900 的截图上判断对比度极不可靠（尤其亮屏环境下看暗色 UI）。
     axe 会算真实 WCAG 对比度比值，给出数值 —— **能判定的才是验收**。

   用法：
     node tools/uicheck/a11y.js
     node tools/uicheck/a11y.js --theme dark
   退出码：0 无 serious/critical 违规；1 有。
   ========================================================================== */
"use strict";

const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const { chromium } = require("playwright");

const ROOT = path.join(__dirname, "..", "..");
const PY = "D:/tools/python/python.exe";
const AXE = require.resolve("axe-core/axe.min.js");

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  if (i < 0) { return dflt; }
  const v = process.argv[i + 1];
  if (!v || v.startsWith("--")) { return true; }
  return v;
}
const THEME = arg("theme", "light");
const MIN_RATIO = Number(arg("min-ratio", 4.5));

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

(async function main() {
  let srv = null;
  try {
    console.log("[a11y] 起本地服务（theme=" + THEME + "）…");
    srv = await startServer();

    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.goto(srv.url, { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);
    await page.addScriptTag({ path: AXE });

    const res = await page.evaluate(async () => {
      return await window.axe.run(document, {
        runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
      });
    });

    const violations = res.violations || [];
    console.log("[a11y] 扫描完成，违规 " + violations.length + " 项");
    let bad = 0;
    for (const v of violations) {
      const impact = v.impact || "unknown";
      const serious = impact === "serious" || impact === "critical";
      if (serious) { bad++; }
      console.log("\n  [" + impact + "] " + v.id + " — " + v.help);
      (v.nodes || []).slice(0, 6).forEach((n) => {
        console.log("      · " + (n.target || []).join(" "));
        const msg = (n.any || []).map((a) => a.message).filter(Boolean)[0];
        if (msg) { console.log("        " + msg); }
        const data = n.any && n.any[0] && n.any[0].data;
        if (data && data.fgColor && data.bgColor) {
          console.log("        前景 " + data.fgColor + " / 背景 " + data.bgColor +
                      " / 对比度 " + data.contrastRatio +
                      (data.contrastRatio < MIN_RATIO ? "  < " + MIN_RATIO + " 不达标" : ""));
        }
      });
      if ((v.nodes || []).length > 6) {
        console.log("      … 另有 " + ((v.nodes.length) - 6) + " 处");
      }
    }
    if (!violations.length) { console.log("  ok  无违规"); }
    const out = path.join(__dirname, "shots", "a11y-" + THEME + ".json");
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, JSON.stringify(res, null, 2));
    console.log("\n[a11y] 明细已存 " + out);
    await browser.close();
    if (bad) { console.log("[a11y] 有 " + bad + " 项 serious/critical 违规"); process.exitCode = 1; }
  } catch (e) {
    console.log("  x  a11y 失败：" + (e && e.message ? e.message : e));
    process.exitCode = 1;
  } finally {
    if (srv && srv.proc) { try { srv.proc.kill(); } catch (_) {} }
  }
})();
