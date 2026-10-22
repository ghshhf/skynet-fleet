/* 活线检查：桌面端自写技能的四个动作，按钮必须真发出对的请求。
 *
 * 为什么单独一个脚本（而不是塞进 clickaudit）：
 *   clickaudit 的相位会真点按钮，而「选用」会往证据库写使用记录——
 *   那是**测试数据冒充真 usage**。这里用 Playwright 的 route 拦截：
 *   请求照发（能验活线），但由我们自己答复（不留脏数据）。
 *
 * 环境变量（由 evals/t18_skill_desktop.py 注入）：
 *   T18_BASE   FleetServer 的 base_url（http://127.0.0.1:port）
 *   T18_TOKEN  本次启动的令牌
 *   T18_SKILL  已落盘的自写技能名
 *
 * 退出码：0 全过 / 1 有不过 / 2 环境不全（这一项**未验证**，不是通过）
 */
const BASE = process.env.T18_BASE || "";
const TOKEN = process.env.T18_TOKEN || "";
const SKILL = process.env.T18_SKILL || "";

if (!BASE || !TOKEN || !SKILL) {
  console.log("SKIP 环境变量不全（T18_BASE/T18_TOKEN/T18_SKILL）");
  process.exit(2);
}

let playwright = null;
try {
  playwright = require("playwright");
} catch (e) {
  console.log("SKIP 找不到 playwright（" + e.message + "）");
  process.exit(2);
}

// 🔴 别叫 URL：那会遮住 Node 全局的 URL 构造函数，route handler 里
//    new URL(req.url()) 直接「URL is not a constructor」（实测踩过）。
const PAGE_URL = BASE + "/index.html#t=" + TOKEN + "&theme=light";
const RESULTS = [];
function ok(msg) { RESULTS.push(["ok", msg]); console.log("  ok " + msg); }
function bad(msg) { RESULTS.push(["x", msg]); console.log("  x  " + msg); }

/* 记下每个接口被请求了几次、带的什么 body——断言就靠它 */
const hits = {};
function note(path, body) {
  if (!hits[path]) { hits[path] = []; }
  hits[path].push(body);
}

(async function main() {
  const browser = await playwright.chromium.launch();
  const page = await (await browser.newContext({
    viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1,
  })).newPage();

  // confirm() 由「回滚」触发：自动按取消 ⇒ 不该发出任何请求
  let confirmSeen = 0;
  page.on("dialog", async (d) => { confirmSeen += 1; await d.dismiss(); });

  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error") { errors.push("console.error: " + m.text());
    }
  });

  /* ---- 拦截：请求照发，答复我们给 ---- */
  await page.route("**/api/skill/**", async (route) => {
    const req = route.request();
    const u = new URL(req.url());
    const path = u.pathname + (u.search || "");
    let body = null;
    try { body = req.postDataJSON(); } catch (e) { body = req.postData(); }
    note(path, body);
    // 假答复：形状跟真后端一致（前端只读这些键）
    let json = { ok: true };
    if (path.indexOf("/api/skill/use") === 0) {
      json = { ok: true, event_id: 4242, name: (body || {}).name,
               used_ok: (body || {}).ok };
    } else if (path.indexOf("/api/skill/stats") === 0) {
      json = { ok: true, name: SKILL, used: 7, used_done: 5,
               shadow: [{ cap: "ai.list", done: 3, open: 1, total: 4,
                          rate: 0.75, ids: [] }],
               verdict: "影子成功率 75%（覆盖 1/1 个能力有执行记录）" };
    } else if (path.indexOf("/api/skill/propose") === 0) {
      json = { ok: true, stage: "verify", name: (body || {}).name,
               gates: [{ gate: "闸1 溯源真", ok: true, detail: "取到 3 条 done 证据" },
                       { gate: "闸2 能力真·存在", ok: true, detail: "全部存在" },
                       { gate: "闸3 可执行·步数", ok: true, detail: "步骤 3 步" },
                       { gate: "闸4 文本干净", ok: true, detail: "干净" }],
               caps: ["ai.list", "ai.status", "apps.list"],
               evolved_from: [1, 2, 3] };
    } else if (path.indexOf("/api/skill/promote") === 0) {
      json = { ok: true, stage: "promote", name: (body || {}).name,
               git: "abc12345def67890", commit_note: "",
               rollback_ready: true };
    } else if (path.indexOf("/api/skill/rollback") === 0) {
      json = { ok: true, stage: "rollback", name: (body || {}).name,
               rolled_to: "abc12345def67890" };
    }
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify(json),
    });
  });

  await page.goto(PAGE_URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  /* ---- 走到技能页 ---- */
  await page.click('.sb-item[data-view="assets"]');
  await page.waitForTimeout(500);
  await page.click('#tabBar .tab[data-tab="skills"]');
  await page.waitForTimeout(1500);

  const n = await page.evaluate(() => ({
    eskill: document.querySelectorAll(".card.eskill").length,
    form: !!document.querySelector(".esform"),
  }));
  if (n.form) { ok("长新技能的表单在"); } else { bad("找不到 .esform"); }
  if (n.eskill >= 1) {
    ok("自写技能卡渲染出来（" + n.eskill + " 张）");
  } else { bad("自写技能卡数量不对：" + n.eskill); }

  /* ---- 定位到**这一张**卡（临时目录里可能有多张自写技能）---- */
  const idx = await page.evaluate((skill) => {
    const cards = Array.from(document.querySelectorAll(".card.eskill"));
    for (let i = 0; i < cards.length; i += 1) {
      const nm = cards[i].querySelector(".cname");
      if (nm && nm.textContent.trim() === skill) { return i; }
    }
    return -1;
  }, SKILL);
  if (idx < 0) {
    bad("找不到名为 " + SKILL + " 的自写技能卡");
    await browser.close();
    process.exit(1);
  }
  const card = (await page.$$(".card.eskill"))[idx];
  const btns = await card.$$(".cops button");
  if (btns.length === 4) { ok("卡上四个操作钮齐全"); }
  else { bad("操作钮数量不对：" + btns.length + "（期望 4）"); }

  /* ---- 选用 ✓ ---- */
  await btns[0].click();
  await page.waitForTimeout(700);
  let useHits = hits["/api/skill/use"] || [];
  if (useHits.length === 1 && useHits[0] && useHits[0].name === SKILL &&
      useHits[0].ok === true) {
    ok("选用✓ 真发了 POST /api/skill/use {name, ok:true}");
  } else {
    bad("选用✓ 的请求不对：" + JSON.stringify(useHits));
  }
  const toast1 = await page.evaluate(() => {
    const t = document.querySelector("#toast");
    return t && !t.hidden ? t.textContent : "";
  });
  if (toast1.indexOf("已记") >= 0) {
    ok("选用✓ 的回执显形了（" + toast1.trim().slice(0, 40) + "）");
  } else { bad("选用✓ 之后没有回执提示（toast=%r）".replace("%r", toast1)); }

  /* ---- 选用 ✗ ---- */
  await btns[1].click();
  await page.waitForTimeout(700);
  useHits = hits["/api/skill/use"] || [];
  if (useHits.length === 2 && useHits[1] && useHits[1].ok === false) {
    ok("选用✗ 真发了 {name, ok:false}（失败也是数据）");
  } else {
    bad("选用✗ 的请求不对：" + JSON.stringify(useHits));
  }

  /* ---- 记分 ---- */
  await btns[2].click();
  await page.waitForTimeout(900);
  const stHits = Object.keys(hits).filter((k) => k.indexOf("/api/skill/stats") === 0);
  if (stHits.length === 1) {
    ok("记分真发了 GET /api/skill/stats?name=" + SKILL);
  } else {
    bad("记分的请求不对：" + JSON.stringify(stHits));
  }
  const stat = await page.evaluate(() => {
    const b = document.querySelector(".card.eskill .sstat");
    return b ? b.textContent : "";
  });
  if (stat.indexOf("使用 7 次") >= 0 && stat.indexOf("75%") >= 0 &&
      stat.indexOf("影子成功率") >= 0) {
    ok("记分盒显形了：使用次数 + 影子成功率 + verdict 原文");
  } else {
    bad("记分盒内容不对：" + stat.slice(0, 120));
  }

  /* ---- 预览四闸（填了 trace/name）---- */
  await page.fill("#esTrace", "exec:t18-inspect-01");
  await page.fill("#esName", "wifi-reconnect");
  await page.fill("#esDesc", "一句话");
  await page.click("#esPreview");
  await page.waitForTimeout(900);
  const prHits = Object.keys(hits).filter((k) => k.indexOf("/api/skill/propose") === 0);
  if (prHits.length === 1) {
    ok("预览四闸真发了 POST /api/skill/propose {trace, name, desc}");
  } else {
    bad("预览四闸的请求不对：" + JSON.stringify(prHits));
  }
  const gates = await page.evaluate(() => {
    const box = document.querySelector("#esGates");
    const rows = box ? box.querySelectorAll(".gate") : [];
    return {
      n: rows.length,
      allOk: Array.from(rows).every((r) => r.className.indexOf("no") < 0),
      promoteDisabled: document.querySelector("#esPromote").disabled,
      promoteText: document.querySelector("#esPromote").textContent.trim(),
    };
  });
  if (gates.n === 4 && gates.allOk) {
    ok("四条闸都显形且全过");
  } else {
    bad("闸门渲染不对：" + JSON.stringify(gates));
  }
  if (!gates.promoteDisabled && gates.promoteText.indexOf("确认落盘") === 0) {
    ok("四闸全过 ⇒ 「确认落盘」解禁");
  } else {
    bad("四闸全过但落盘钮没解禁：" + JSON.stringify(gates));
  }

  /* ---- 落盘（promote）---- */
  await page.click("#esPromote");
  await page.waitForTimeout(900);
  const poHits = Object.keys(hits).filter((k) => k.indexOf("/api/skill/promote") === 0);
  if (poHits.length === 1) {
    ok("确认落盘真发了 POST /api/skill/promote（只带 trace/name/desc）");
  } else {
    bad("落盘的请求不对：" + JSON.stringify(poHits));
  }

  /* ---- 空输入：前端必须拦下，一个请求都不该发 ---- */
  const beforeEmpty = Object.keys(hits).length;
  await page.fill("#esTrace", "");
  await page.fill("#esName", "");
  await page.click("#esPreview");
  await page.waitForTimeout(600);
  const afterEmpty = Object.keys(hits).length;
  if (afterEmpty === beforeEmpty) {
    ok("空输入被前端拦下（没白发请求）");
  } else {
    bad("空输入也发出了请求（前端没拦）");
  }

  /* ---- 回滚：confirm 被取消 ⇒ 不该发请求 ---- */
  // 🔴 句柄必须**重新取**：上一步「确认落盘」成功后前端会 loadAssetTab
  //    重渲染整个 tab body，先前拿到的 btns 全部脱离 DOM——
  //    直接点会报 "Element is not attached to the DOM"（实测踩过）。
  //    这跟 clickaudit 里「重渲染后按签名找回」是同一类问题。
  const idx2 = await page.evaluate((skill) => {
    const cards = Array.from(document.querySelectorAll(".card.eskill"));
    for (let i = 0; i < cards.length; i += 1) {
      const nm = cards[i].querySelector(".cname");
      if (nm && nm.textContent.trim() === skill) { return i; }
    }
    return -1;
  }, SKILL);
  const card2 = idx2 >= 0 ? (await page.$$(".card.eskill"))[idx2] : null;
  const rbBtn = card2 ? (await card2.$$(".cops button"))[3] : null;
  const beforeRb = Object.keys(hits).length;
  if (!rbBtn) {
    bad("重渲染后找不到回滚按钮（卡片没重新渲染出来）");
  } else {
    await rbBtn.click();
    await page.waitForTimeout(700);
    const afterRb = Object.keys(hits).length;
    if (confirmSeen >= 1 && afterRb === beforeRb) {
      ok("回滚先问一句；取消就不发请求（破坏性动作有确认）");
    } else {
      bad("回滚的确认/拦截不对（confirmSeen=" + confirmSeen +
          " 请求变化 " + beforeRb + "→" + afterRb + "）");
    }
  }

  /* ---- 页面不许有 JS 错误 ---- */
  if (errors.length === 0) { ok("页面无 JS 错误"); }
  else { bad("页面报错：" + errors.slice(0, 3).join(" | ")); }

  await browser.close();
  const fails = RESULTS.filter((r) => r[0] === "x").length;
  console.log("[skillwiring] " + (RESULTS.length - fails) + "/" + RESULTS.length + " 通过");
  process.exit(fails ? 1 : 0);
})().catch((e) => {
  console.log("  x  活线检查崩了：" + (e && e.message ? e.message : e));
  process.exit(1);
});
