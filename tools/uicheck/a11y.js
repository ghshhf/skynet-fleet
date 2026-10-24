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
// 🔴 不写死解释器路径：换机即挂（原值 D:/tools/python/python.exe 在本机就不存在）。
// 优先级：FLEET_PY 环境变量 → PATH 里的 python。
const PY = process.env.FLEET_PY || (process.platform === "win32" ? "python" : "python3");
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

/* 逐元素算对比度 —— 补 axe 的盲区。
   🔴 为什么必须自己算：
     axe 的 color-contrast 规则遇到**图标字形**（单个符号、纯非文字字符）会直接
     判 `incomplete` 而不是 pass/fail，实测本页面 16 个 `.ic` / `#themeBtn` /
     `#tbClose` 全落在这个盲区里。于是「违规 0 项」是个**假绿**——
     真正看不清的恰恰是那 16 个浅灰图标。
   ⇒ 自己算：#1c7f6f 这类前景 vs 层层上溯的真背景，用 WCAG 相对亮度公式。
     文字按 4.5（大字号 3.0），图标字形按 3.0（WCAG 1.4.11 非文本对比度）。 */
const CONTRAST = (opts) => {
  const { textMin, iconMin } = opts;

  function parse(c) {
    const m = /rgba?\(([^)]+)\)/.exec(c || "");
    if (!m) { return null; }
    const p = m[1].split(",").map((x) => parseFloat(x));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }

  function lum(c) {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }

  function blend(top, bot) {
    const a = top.a;
    return {
      r: top.r * a + bot.r * (1 - a),
      g: top.g * a + bot.g * (1 - a),
      b: top.b * a + bot.b * (1 - a),
      a: 1,
    };
  }

  // 层层上溯找「真的不透明背景」；遇到渐变就把**所有色标**都当候选背景
  // （最差的那个才决定可读性），遇到图片就当背景不可判定，单列出来。
  function stopsOf(img) {
    const out = [];
    const re = /rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}/g;
    let m;
    while ((m = re.exec(img || "")) !== null) {
      let c = null;
      if (m[0][0] === "#") {
        const h = m[0].slice(1);
        const hex = h.length === 3 ? h.split("").map((x) => x + x).join("") : h.slice(0, 6);
        c = { r: parseInt(hex.slice(0, 2), 16), g: parseInt(hex.slice(2, 4), 16),
              b: parseInt(hex.slice(4, 6), 16), a: 1 };
      } else {
        c = parse(m[0]);
      }
      if (c && !(c.r === 0 && c.g === 0 && c.b === 0 && /transparent|00000000/.test(m[0]))) {
        out.push(c);
      }
    }
    return out;
  }

  function bgOf(node) {
    let cur = node;
    let acc = null, unknown = "", grad = null;
    while (cur && cur !== document.documentElement) {
      const cs = getComputedStyle(cur);
      const img = cs.backgroundImage || "";
      if (!grad && /gradient/.test(img)) { grad = stopsOf(img); }
      if (!unknown && /url\(/.test(img)) { unknown = "图片背景"; }
      const c = parse(cs.backgroundColor);
      if (c && c.a > 0.001) {
        acc = acc ? blend(acc, c) : c;
        if (acc.a >= 0.999) { break; }
      }
      cur = cur.parentElement;
    }
    const root = parse(getComputedStyle(document.body).backgroundColor) ||
                 { r: 255, g: 255, b: 255, a: 1 };
    const base = acc ? (acc.a >= 0.999 ? acc : blend(acc, root)) : root;
    // 渐变层：每个色标叠在实底上，一起作为候选
    const cands = grad ? grad.map((g) => blend(g, base)) : [];
    return { bg: base, cands, unknown };
  }

  function path(node) {
    let s = node.tagName.toLowerCase();
    if (node.id) { return "#" + node.id; }
    if (node.className && typeof node.className === "string") {
      const c = node.className.trim().split(/\s+/).slice(0, 2).join(".");
      if (c) { s += "." + c; }
    }
    return s;
  }

  const out = [];
  document.querySelectorAll("body *").forEach((n) => {
    if (!(n instanceof HTMLElement)) { return; }
    if (n.hidden || n.getClientRects().length === 0) { return; }
    const cs = getComputedStyle(n);
    if (cs.visibility === "hidden" || cs.display === "none") { return; }
    if (parseFloat(cs.opacity) < 0.1) { return; }
    // 只看「自己的文字/字形」，子元素各算各的
    let own = "";
    n.childNodes.forEach((c) => { if (c.nodeType === 3) { own += c.textContent; } });
    own = own.trim();
    if (!own) { return; }
    const r = n.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) { return; }
    const fg = parse(cs.color);
    if (!fg) { return; }
    const { bg, cands, unknown } = bgOf(n);
    const fgr = fg.a >= 0.999 ? fg : blend(fg, bg);
    const l1 = lum(fgr);
    // 渐变背景：取**最差**的那个色标（这才决定这块字到底能不能看清）
    const pool = cands.length ? cands : [bg];
    let worst = null;
    pool.forEach((b) => {
      const l2 = lum(b);
      const rt = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      if (!worst || rt < worst.ratio) { worst = { ratio: rt, bg: b }; }
    });
    const ratio = worst.ratio;
    const fs = parseFloat(cs.fontSize) || 12;
    const bold = (parseInt(cs.fontWeight, 10) || 400) >= 700;
    const icon = own.length <= 2;
    const min = icon ? iconMin : ((fs >= 24 || (bold && fs >= 18.66)) ? 3.0 : textMin);
    out.push({
      sel: path(n), text: own.slice(0, 14), ratio: Math.round(ratio * 100) / 100,
      fg: "rgb(" + [fg.r, fg.g, fg.b].map(Math.round).join(",") + ")",
      bg: "rgb(" + [worst.bg.r, worst.bg.g, worst.bg.b].map(Math.round).join(",") + ")",
      size: fs, icon, min, unknown,
      ok: ratio + 1e-6 >= min,
    });
  });
  return out;
};

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

    // 🔴 只扫首屏是不够的：侧栏 6 个视图 + 专家页 3 个 Tab 的配色各不相同，
    // 而「浅色主题下 32 处看得吃力」绝大多数在非首屏。逐层切过去扫。
    const views = await page.evaluate(() =>
      Array.from(document.querySelectorAll("[data-view]")).map((n) => n.getAttribute("data-view")));
    const tabs = ["skills", "connectors", "experts"];
    const phases = ["默认"].concat(views.map((v) => "view:" + v)).concat(tabs.map((t) => "tab:" + t));

    const allViolations = [];
    const offenders = new Map();   // sel|size → 最差一条
    let checked = 0;

    for (const ph of phases) {
      if (ph.startsWith("view:")) {
        await page.evaluate((v) => {
          const el = document.querySelector('[data-view="' + v + '"]');
          if (el) { el.click(); }
        }, ph.slice(5));
        await page.waitForTimeout(700);
      } else if (ph.startsWith("tab:")) {
        await page.evaluate((t) => {
          const back = document.querySelector('[data-view="assets"]');
          if (back) { back.click(); }
          const tab = document.querySelector('#tabBar .tab[data-tab="' + t + '"]');
          if (tab) { tab.click(); }
        }, ph.slice(4));
        await page.waitForTimeout(900);
      }

      const res = await page.evaluate(async () => {
        return await window.axe.run(document, {
          runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
        });
      });
      (res.violations || []).forEach((v) => { allViolations.push({ phase: ph, v }); });

      const rows = await page.evaluate(CONTRAST, { textMin: MIN_RATIO, iconMin: 3.0 });
      checked += rows.length;
      rows.filter((r) => !r.ok).forEach((r) => {
        const k = r.sel + "|" + r.size + "|" + r.icon;
        const prev = offenders.get(k);
        if (!prev || r.ratio < prev.ratio) { offenders.set(k, Object.assign({ phase: ph }, r)); }
      });
      console.log("[a11y] 相位「" + ph + "」文本节点 " + rows.length +
                  " 个，低于门槛 " + rows.filter((r) => !r.ok).length + " 个");
    }

    console.log("\n[a11y] axe 违规 " + allViolations.length + " 项 / 自算对比度节点 " + checked + " 个");
    let bad = 0;
    for (const { phase, v } of allViolations) {
      const impact = v.impact || "unknown";
      const serious = impact === "serious" || impact === "critical";
      if (serious) { bad++; }
      console.log("\n  [" + impact + "] " + v.id + " — " + v.help + "（" + phase + "）");
      (v.nodes || []).slice(0, 6).forEach((n) => {
        console.log("      · " + (n.target || []).join(" "));
        const msg = (n.any || []).map((a) => a.message).filter(Boolean)[0];
        if (msg) { console.log("        " + msg); }
      });
      if ((v.nodes || []).length > 6) {
        console.log("      … 另有 " + ((v.nodes.length) - 6) + " 处");
      }
    }

    // 自算结果按「差多少」排序，先修最差的
    const list = Array.from(offenders.values()).sort((a, b) => a.ratio - b.ratio);
    if (list.length) {
      console.log("\n[a11y] 对比度不达标 " + list.length + " 处（自算，含图标字形）：");
      list.slice(0, 40).forEach((r) => {
        console.log("  " + String(r.ratio).padEnd(5) + " < " + r.min + "  " + r.sel +
                    "  「" + r.text + "」  " + r.size + "px  " + r.fg + " / " + r.bg +
                    (r.unknown ? "  ⚠ " + r.unknown : ""));
      });
      if (list.length > 40) { console.log("  … 另有 " + (list.length - 40) + " 处"); }
    } else {
      console.log("\n  ok  对比度全部达标（文字 ≥" + MIN_RATIO + "，图标 ≥3.0）");
    }

    const out = path.join(__dirname, "shots", "a11y-" + THEME + ".json");
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, JSON.stringify({
      theme: THEME, axe: allViolations.map((x) => ({ phase: x.phase, id: x.v.id,
        impact: x.v.impact, nodes: x.v.nodes })),
      contrast: list, checked,
    }, null, 2));
    console.log("\n[a11y] 明细已存 " + out);
    await browser.close();
    if (bad || list.length) {
      console.log("[a11y] 不合格：" + bad + " 项 axe serious/critical + " +
                  list.length + " 处对比度不达标");
      process.exitCode = 1;
    }
  } catch (e) {
    console.log("  x  a11y 失败：" + (e && e.message ? e.message : e));
    process.exitCode = 1;
  } finally {
    if (srv && srv.proc) { try { srv.proc.kill(); } catch (_) {} }
  }
})();
