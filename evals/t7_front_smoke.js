/* 门禁 t7 的前端运行时冒烟：用假 DOM 真跑一遍 ui/app.js。
 *
 * 为什么需要它：node --check 只查语法，查不出「页面打开就是一片白」这类坏。
 * 这里把 document / location / fetch 换成假的，真的 require 一次 app.js，
 * 然后看三件事：
 *   ① 加载过程不抛异常（抛了 = 窗口一打开就白屏）；
 *   ② 令牌真的从 URL 片段流进了 X-Fleet-Token 请求头（安全机制是活的，
 *      不是只写在代码里好看）；
 *   ③ 启动时会真去拉 /api/health、/api/devices、/api/caps（前端与后端接上了）。
 *
 * 退出码：0 全过，1 有失败项。
 */
"use strict";

const path = require("path");
const fs = require("fs");
const mini = require("./minidom.js");

const UI = path.join(__dirname, "..", "ui");
const TOKEN = "t7-smoke-token";
const FAIL = [];

function check(cond, msg) {
  if (cond) { console.log("  ok", msg); } else { console.log("  x ", msg); FAIL.push(msg); }
}

/* ---------------- 假 DOM（真 DOM 树 + 真选择器） ----------------
 * 用真 index.html 建树，而不是手搓几个假元素：
 * app.js 里有 `querySelectorAll(".sb-item")` 这类按选择器找元素的代码，
 * 假 DOM 只会 getElementById 的话这些代码永远返回空 ⇒ 门禁测不到它们。
 */
const html = fs.readFileSync(path.join(UI, "index.html"), "utf-8");
const domRoot = mini.parseHTML(html);
global.__domRoot = domRoot;

const els = {};
// 真 DOM 里存在的元素，getElementById 要能找到它（不许凭空造一个替身）
(function indexIds(node) {
  for (const c of node.children) {
    if (c.id) { els[c.id] = c; }
    indexIds(c);
  }
})(domRoot);

function makeEl(id) {
  const el = mini.makeEl(id || "div", {});
  el.id = id || "";
  return el;
}

global.document = {
  readyState: "complete",
  documentElement: { getAttribute: () => "light", setAttribute() { }, requestFullscreen: () => Promise.resolve() },
  getElementById(id) { if (!els[id]) { els[id] = makeEl(id); } return els[id]; },
  createElement(tag) { return makeEl(tag); },
  querySelector(sel) { return global.__docQ(sel)[0] || null; },
  querySelectorAll(sel) { return global.__docQ(sel); },
  addEventListener() { },
  fullscreenElement: null,
  exitFullscreen() { },
};
global.__docQ = (sel) => mini.queryAllDoc(domRoot, sel);
global.location = { hash: "#t=" + TOKEN + "&theme=dark" };
global.setTimeout = setTimeout;
global.clearTimeout = clearTimeout;

/* ---------------- 假 fetch ---------------- */
const seen = [];
function fakeFetch(url, opts) {
  seen.push({ url: url, headers: (opts && opts.headers) || {}, body: (opts && opts.body) || "" });
  if (url === "/api/chat") {
    // 假 SSE 流：分两块吐，中间带 \n\n 帧界
    const enc = new TextEncoder();
    const frames = [
      'data: {"type":"line","text":"[t7] 第一行"}\n\n',
      'data: {"type":"line","text":"[t7] 第二行"}\n\n',
      'data: {"type":"done","result":{"ok":true,"cap":"ai.chat","code":"ok"}}\n\n'
    ];
    const parts = [enc.encode(frames[0] + frames[1]), enc.encode(frames[2])];
    let i = 0;
    return Promise.resolve({
      ok: true, status: 200,
      body: {
        getReader() {
          return {
            read() {
              if (i < parts.length) { return Promise.resolve({ done: false, value: parts[i++] }); }
              return Promise.resolve({ done: true, value: undefined });
            }
          };
        }
      },
      text: () => Promise.resolve("")
    });
  }
  const body = JSON.stringify({ ok: true, devices: 1, caps_count: 2, backend: null, backends: [], backend_reason: "无", persona: { card_loaded: false, card: {}, state: {} }, count: 0, caps: [], domains: {}, devices: [] });
  return Promise.resolve({
    ok: true, status: 200,
    text: () => Promise.resolve(body)
  });
}
global.fetch = fakeFetch;

/* ---------------- 真跑 app.js ---------------- */
let loadErr = null;
try {
  const src = fs.readFileSync(path.join(UI, "app.js"), "utf-8");
  // eslint-disable-next-line no-new-func
  new Function(src)();
} catch (e) {
  loadErr = e;
}
check(!loadErr, "app.js 加载不抛异常" + (loadErr ? "：" + loadErr.message : ""));

// 启动是异步的（bootstrap 里用 Promise），等一拍让 fetch 走完
setTimeout(() => {
  const health = seen.find((s) => s.url === "/api/health");
  check(!!health, "启动时会拉 /api/health");
  check(!!seen.find((s) => s.url === "/api/devices"), "启动时会拉 /api/devices");
  check(!!seen.find((s) => s.url === "/api/caps"), "启动时会拉 /api/caps");
  if (health) {
    check(health.headers["X-Fleet-Token"] === TOKEN,
      "令牌从 URL 片段流进了 X-Fleet-Token 头（实际 " + health.headers["X-Fleet-Token"] + "）");
  }

  /* ---- 走一遍发送：回车 → SSE → 消息流里长出内容 ---- */
  const input = els["input"];
  input.value = "你好";
  // 真 DOM 的 addEventListener 允许绑多个 handler，所以存的是数组。
  // 这里取第一个来触发（app.js 对 input 只绑一个 keydown）。
  const kd = input._h && input._h.keydown;
  const keydown = Array.isArray(kd) ? kd[0] : kd;
  check(!!keydown, "输入框绑了回车处理");
  if (keydown) {
    keydown({ key: "Enter", shiftKey: false, isComposing: false, preventDefault() { } });
  }

  /* ---- 选择器能力：app.js 大量用 querySelectorAll 找元素，
         假 DOM 若不支持，这些代码就永远返回空 ⇒ 门禁测不到 ⇒
         页面真坏时门禁照样绿。所以这里把「选择器找到了东西」钉死。 */
  const sbItems = document.querySelectorAll(".sb-item");
  check(sbItems.length >= 5, "选择器 .sb-item 找得到左栏导航项（实际 " + sbItems.length + " 个）");
  const tabs = document.querySelectorAll("#tabBar .tab");
  check(tabs.length === 3, "后代选择器 #tabBar .tab 找得到三个 Tab（实际 " + tabs.length + " 个）");
  const sidebar = document.querySelector(".sidebar");
  check(!!sidebar, "querySelector('.sidebar') 找得到左栏");
  const crewView = document.getElementById("viewCrew");
  check(!!crewView, "getElementById 找得到 viewCrew（真 DOM 树里解析出来的）");
  check(!!crewView && crewView.classList.contains("view-crew"),
    "viewCrew 带着 view-crew 类（属性解析对了）");
  // 左栏「助理」那项的 data-view 必须是 crew（导航接线对不对就靠这个）
  const crewItem = sbItems.filter((x) => x.getAttribute("data-view") === "crew")[0];
  check(!!crewItem, "左栏有 data-view=crew 的项");
  check(!!crewItem && !!crewItem._h.click, "助理项绑了点击（不是死链）");
  setTimeout(() => {
    const chat = seen.find((s) => s.url === "/api/chat");
    check(!!chat, "回车会 POST /api/chat");
    if (chat) {
      check(chat.headers["X-Fleet-Token"] === TOKEN, "聊天请求也带令牌");
      check(chat.body && chat.body.indexOf("你好") >= 0, "聊天请求带上了 prompt");
    }
    // 消息流里应该已经有气泡了：用户 1 条 + bot 1 条
    const streamIn = els["streamIn"];
    const kids = (streamIn && streamIn.children) || [];
    check(kids.length >= 2, "消息流里有用户气泡和 bot 气泡（实际 " + kids.length + " 个）");
    // bot 气泡结构：div.msg.bot > [ava, body]；body > [who, txt, actbar]
    const bot = kids[kids.length - 1];
    const body = bot && bot.children[1];
    const txt = body && body.children[1];
    const rendered = txt ? txt.children.map((p) => p.textContent).join("|") : "";
    check(rendered.indexOf("第一行") >= 0 && rendered.indexOf("第二行") >= 0,
      "SSE 的两行都渲染进 bot 气泡了（实际「" + rendered + "」）");
    const resBar = els["resBar"];
    check(resBar && resBar.hidden === false && String(resBar.textContent).indexOf("ai.chat") >= 0,
      "done 事件把真实结果写进了结果条（实际「" + (resBar ? resBar.textContent : "") + "」）");

    console.log("t7 front smoke:", FAIL.length ? "FAIL (" + FAIL.length + " 项)" : "PASS");
    process.exit(FAIL.length ? 1 : 0);
  }, 120);
}, 50);
