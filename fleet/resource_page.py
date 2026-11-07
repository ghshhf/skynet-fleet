# -*- coding: utf-8 -*-
"""resource_page —— 资源流的单页前端（手机优先、随版本更新下发）。

执行依据：2026-11-07 主人的即时指令，这是最高标准；计划书只是素材，不是裁决依据。
  · 以 A（本机版本）为本体，吸收 B 的能力，主攻手机版 UI；
  · 做资源分类界面 + 学推特的信息流交互；现状 UI 很差，要「每天愿意打开」；
  · 单用户、只为主人服务，不为任何人服务，一切以主人为标准；
  · 设备是两台左右手机、会换机 ⇒ 全部自适应，不绑定任何具体设备；
  · 安装通过版本更新；不开发新技术 = 不另起新架构/新链路，不是禁止外部素材；
  · 外部库/素材可以用，条件是随包自带、装完就有、不靠运行时联网去取；
  · 加功能要说出真实使用场景与理由（计划书 P26 第一条）。

本版逐条给理由：
  1. 推特式卡片 / 互动条 / 无限滚动 / 底部导航 / 双 tab
     理由：主人「UI 很差、学推特」；场景：手机上像刷社交信息流一样浏览跨域资源。
  2. 分类按标签 / 类型组织，来源只作筛选项（新增 kind/domain 两个可选参数）
     理由：主人「资源分类界面」；场景：按内容找，不按站点找。
  3. 关注主题存服务端 SQLite，不存手机本地
     理由：主人「两台手机、会换机、自适应」；场景：换机后关注不丢。
     复用既有 /api/resources/act 与 stats，不新增接口。
  4. 本地生图产物压缩内嵌
     理由：主人「F 盘生图可用 / 外部素材随包含进 App」；场景：分类页头图离线也在。
  5. 响应式 + 深色 + 安全区 + 特性检测
     理由：主人「自适应、不假定手机不换」；场景：不同屏宽 / 横竖屏 / 新旧设备都能开。
  6. 不做权限闸门 / 二次确认
     理由：主人「完全权限、不防御、一切以我为标准」，即时指令高于计划书 P21；
     场景：一个人用，不给自己上锁。

形态：单文件 HTML/CSS/JS 内联下发，便于版本更新直接覆盖；图片素材以 base64
随同目录文件打进包内。这是本版不需要引框架，不是「不许用外部依赖」。

吸收 B（tianwang_app/ResourcesScreen.kt）的交互能力，不搬它的技术栈：
  · 标签从已加载卡片累积成筛选 chips（原 FilterChip 思路）；
  · 显式 busy / err / info 状态、空态与重试，替代 prompt()/alert()；
  · 评论用自定义输入面板（原 AlertDialog）、详情用底部抽屉（原详情面板）；
  · 常驻 session、底部单页导航。
A 侧已有而 B 没有的：卡片直接带 liked / faved / comments / why / score，
互动状态以服务端为准，不在本地伪造。
"""
import os

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<meta name="app-version" content="2026.11.07.2">
<title>天网 · 资源流</title>
<style>
:root{--bg:#fff;--surface:#fff;--soft:#f7f9fa;--line:#e6ecf0;--txt:#0f1419;
  --dim:#536471;--acc:#1d9bf0;--acc-soft:#e8f5fe;--like:#f91880;--ok:#00ba7c;
  --shadow:0 8px 28px rgba(15,20,25,.16)}
@media (prefers-color-scheme:dark){:root{--bg:#000;--surface:#16181c;--soft:#202327;
  --line:#2f3336;--txt:#e7e9ea;--dim:#8b98a5;--acc:#1d9bf0;--acc-soft:#0a2338;
  --like:#f91880;--ok:#00ba7c;--shadow:0 8px 28px rgba(0,0,0,.6)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
button,input,textarea{font:inherit;color:inherit}
button{cursor:pointer}
a{color:inherit}
.wrap{max-width:640px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;background:var(--surface);border-inline:1px solid var(--line)}
header{position:sticky;top:0;z-index:20;background:var(--surface);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:center;gap:10px;padding:12px 16px 8px}
.logo{font-size:18px;font-weight:600;letter-spacing:.3px;flex:1}
.logo span{color:var(--dim);font-weight:400;font-size:14px;margin-left:2px}
.bar-actions{display:flex;gap:4px}
.icon-btn{width:36px;height:36px;display:grid;place-items:center;background:none;border:0;border-radius:50%;color:var(--dim)}
.icon-btn:hover{background:var(--soft);color:var(--acc)}
.icon-btn svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.searchrow{display:flex;align-items:center;gap:8px;padding:2px 16px 10px}
.searchrow input{flex:1;background:var(--soft);border:1px solid var(--line);border-radius:999px;padding:9px 14px;outline:none;font-size:14px}
.searchrow input:focus{border-color:var(--acc);background:var(--surface)}
.tabs{display:flex;border-bottom:1px solid var(--line)}
.tabs button{flex:1;background:none;border:0;padding:12px 0 13px;color:var(--dim);font-size:15px;font-weight:500;position:relative}
.tabs button.on{color:var(--txt)}
.tabs button.on::after{content:"";position:absolute;bottom:0;left:50%;transform:translateX(-50%);width:56px;height:4px;border-radius:4px;background:var(--acc)}
.chips{display:flex;gap:7px;overflow-x:auto;padding:9px 12px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{white-space:nowrap;border:1px solid var(--line);background:var(--surface);color:var(--dim);border-radius:999px;padding:5px 12px;font-size:13px}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
.chip:hover{border-color:var(--acc)}
main{flex:1}
.pane{padding:16px}
.card{display:flex;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--surface)}
.card:active{background:var(--soft)}
.avatar{width:42px;height:42px;flex:0 0 42px;border-radius:50%;display:grid;place-items:center;color:#fff;font-size:17px;font-weight:600;user-select:none}
.body{flex:1;min-width:0}
.who{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:14px}
.who b{font-weight:600;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.kind{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:0 7px;color:var(--dim)}
.time{color:var(--dim);font-size:13px}
.more{margin-left:auto;background:none;border:0;color:var(--dim);font-size:18px;line-height:1;padding:0 4px;letter-spacing:1px}
.title{display:block;font-size:16px;font-weight:600;line-height:1.42;margin:7px 0 4px;text-decoration:none;word-break:break-word}
.title:hover{color:var(--acc)}
.sum{font-size:14px;color:var(--txt);margin:0 0 8px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.tag{border:0;background:var(--acc-soft);color:var(--acc);border-radius:999px;padding:3px 10px;font-size:12px}
.why{font-size:12px;color:var(--dim);margin-bottom:6px}
.acts{display:flex;align-items:center;justify-content:space-between;max-width:330px;margin-top:2px}
.act{display:flex;align-items:center;gap:5px;background:none;border:0;color:var(--dim);font-size:12.5px;text-decoration:none;padding:5px 7px;border-radius:999px}
.act:hover{background:var(--acc-soft);color:var(--acc)}
.act svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.act.like.on{color:var(--like)}.act.like:hover{color:var(--like)}
.act.fav.on{color:var(--acc)}
.act.like.on svg,.act.fav.on svg{fill:currentColor}
.tail{padding:18px 16px 28px;text-align:center;color:var(--dim);font-size:13px}
.tail button{background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:8px 18px;color:var(--txt)}
.state{padding:56px 24px;text-align:center;color:var(--dim);font-size:14px;line-height:1.8}
.state b{display:block;color:var(--txt);font-size:16px;margin-bottom:6px}
.state button{margin-top:12px;background:var(--acc);border:0;border-radius:999px;padding:9px 20px;color:#fff}
.skeleton{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px}
.skeleton i{display:block;background:var(--soft);border-radius:8px}
.skeleton .a{width:42px;height:42px;border-radius:50%;flex:0 0 42px}
.skeleton .b{flex:1}
.skeleton .b i{height:12px;margin-bottom:9px}
.skeleton .b i:nth-child(2){width:82%;height:16px}
.skeleton .b i:nth-child(3){width:56%}
.bottom{position:sticky;bottom:0;z-index:15;display:flex;background:var(--surface);border-top:1px solid var(--line);padding:6px 0 max(6px,env(safe-area-inset-bottom))}
.bottom button{flex:1;background:none;border:0;color:var(--dim);display:flex;flex-direction:column;align-items:center;gap:2px;font-size:11px;padding:5px 0}
.bottom button.on{color:var(--acc)}
.bottom svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.bottom button.on svg{stroke-width:2.1}
.sheet-mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:30}
.sheet{position:fixed;left:50%;transform:translateX(-50%);bottom:0;width:min(640px,100%);max-height:86vh;overflow:auto;z-index:31;background:var(--surface);border-radius:18px 18px 0 0;box-shadow:var(--shadow);padding:20px 20px max(20px,env(safe-area-inset-bottom))}
.sheet h2{font-size:18px;line-height:1.4;margin:6px 44px 10px 0}
.sheet .close,.composer .close{position:absolute;right:14px;top:14px;background:var(--soft);border:0;border-radius:50%;width:32px;height:32px;color:var(--dim)}
.sheet .close svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round}
.sheet .origin,.sheet .actlog{font-size:12.5px;color:var(--dim);word-break:break-all;margin:4px 0}
.sheet .origin a{color:var(--acc)}
.sheet .sec{border-top:1px solid var(--line);margin-top:14px;padding-top:12px}
.sheet .sec h3{font-size:13px;color:var(--dim);font-weight:500;margin:0 0 8px}
.sheet .out{display:inline-block;background:var(--acc);color:#fff;text-decoration:none;border-radius:999px;padding:9px 18px;font-size:14px;margin-top:8px}
.composer{position:fixed;left:50%;transform:translateX(-50%);bottom:0;width:min(640px,100%);z-index:32;background:var(--surface);border-radius:18px 18px 0 0;box-shadow:var(--shadow);padding:18px 18px max(18px,env(safe-area-inset-bottom))}
.composer h3{margin:0 0 12px;font-size:15px;font-weight:600}
.composer textarea{width:100%;resize:vertical;background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:12px;outline:none;font-size:14px}
.composer textarea:focus{border-color:var(--acc)}
.composer-foot{display:flex;align-items:center;justify-content:space-between;margin-top:10px;gap:10px;color:var(--dim);font-size:12px}
.primary{background:var(--acc);border:0;border-radius:999px;padding:9px 22px;color:#fff;font-size:14px}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:78px;z-index:40;background:var(--txt);color:var(--surface);border-radius:999px;padding:10px 20px;font-size:13.5px;opacity:0;pointer-events:none;transition:opacity .22s;max-width:88vw;text-align:center}
.toast.show{opacity:1}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-bottom:22px}
.gitem{border:1px solid var(--line);background:var(--surface);border-radius:12px;padding:11px 12px;text-align:left;display:flex;align-items:center;gap:8px;min-width:0}
.gitem:hover{border-color:var(--acc)}
.gitem span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13.5px}
.gitem em{font-style:normal;color:var(--dim);font-size:12px}
.gitem .fav-btn{border:1px solid var(--line);background:none;color:var(--dim);border-radius:999px;width:26px;height:26px;line-height:1;flex:0 0 26px;font-size:15px;padding:0}
.gitem .fav-btn.on{color:var(--acc);border-color:var(--acc)}
.sect{font-size:15px;font-weight:600;margin:0 0 4px}
.note{font-size:12.5px;color:var(--dim);margin:0 0 12px;line-height:1.6}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-bottom:20px}
.stat{border:1px solid var(--line);border-radius:12px;padding:14px}
.stat b{display:block;font-size:22px;font-weight:600}
.stat span{font-size:12px;color:var(--dim)}
.bar-row{display:flex;align-items:center;gap:10px;margin:9px 0;font-size:13px}
.bar-row span{width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-row i{flex:1;height:7px;background:var(--soft);border-radius:999px;overflow:hidden;font-style:normal}
.bar-row i b{display:block;height:100%;background:var(--acc);border-radius:999px}
.ver{font-size:11.5px;color:var(--dim);text-align:center;padding:18px 0 6px}
.hero{display:block;width:100%;height:140px;object-fit:cover;object-position:center 34%;border-radius:14px;margin-bottom:16px}
@media (max-width:380px){
  .grid,.stats{grid-template-columns:1fr}
  .card{padding:12px 11px;gap:9px}
  .avatar{width:36px;height:36px;flex-basis:36px;font-size:15px}
  .title{font-size:15px}
  .acts{max-width:100%}
  .hero{height:112px}
}
@media (min-width:700px){
  .wrap{box-shadow:0 0 0 1px var(--line),0 18px 50px rgba(15,20,25,.08)}
  .grid{grid-template-columns:repeat(3,minmax(0,1fr))}
  .hero{height:180px}
}
@media (orientation:landscape) and (max-height:520px){
  .sheet{max-height:94vh}
  .bar{padding-top:8px;padding-bottom:6px}
  .hero{height:100px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <header id="top">
    <div class="bar">
      <div class="logo">天网<span>·资源</span></div>
      <div class="bar-actions">
        <button class="icon-btn" id="searchBtn" aria-label="搜索"></button>
        <button class="icon-btn" id="refreshBtn" aria-label="刷新"></button>
      </div>
    </div>
    <div class="searchrow" id="searchRow" hidden>
      <input id="q" type="search" placeholder="搜标题、摘要或标签" autocomplete="off" aria-label="搜索资源">
    </div>
    <div class="tabs" role="tablist">
      <button class="on" data-stream="all" role="tab" aria-selected="true">为你推荐</button>
      <button data-stream="focus" role="tab" aria-selected="false">我的关注</button>
    </div>
    <div class="chips" id="chips" role="group" aria-label="资源分类"></div>
  </header>

  <main>
    <section id="view-feed">
      <div id="feed" role="feed"></div>
      <div class="tail" id="tail"></div>
    </section>
    <section id="view-explore" class="pane" hidden>__HERO_IMG__<div id="exploreBody"></div></section>
  </main>

  <nav class="bottom" aria-label="主导航">
    <button class="on" data-view="feed" aria-label="信息流"></button>
    <button data-view="explore" aria-label="分类"></button>
  </nav>
</div>

<div class="sheet-mask" id="mask" hidden></div>
<aside class="sheet" id="detail" hidden aria-label="资源详情"></aside>
<div class="composer" id="composer" hidden>
  <button class="close" id="closeComposer" aria-label="关闭"></button>
  <h3>写一句评论</h3>
  <textarea id="commentText" rows="3" placeholder="当时的想法、问题、判断、补充…"></textarea>
  <div class="composer-foot"><span id="commentHint"></span><button class="primary" id="sendComment">记下</button></div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const UI_VERSION = "2026.11.07.2";
const TOKEN = (location.hash.match(/t=([^&]+)/) || [])[1] || localStorage.fleet_token || "";
if (TOKEN) localStorage.fleet_token = TOKEN;
const H = TOKEN ? {"X-Fleet-Token": TOKEN} : {};
const SESSION = "web-" + (localStorage.fleet_session || (localStorage.fleet_session = Date.now().toString(36)));
const KINDS = {article:"文章",video:"视频",paper:"论文",repo:"开源项目",model:"模型",
  tutorial:"教程",discussion:"讨论",tool:"工具",site:"站点",image:"图片",audio:"音频",book:"书籍"};
const P = {
  comment:'<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.7 9.7 0 0 1-3.9-.8L3 21l1.9-5.1A8.4 8.4 0 0 1 12 3.1a8.4 8.4 0 0 1 9 8.4z"/>',
  fav:'<path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"/>',
  like:'<path d="M12 21s-8-4.9-8-11a4.5 4.5 0 0 1 8-2.8A4.5 4.5 0 0 1 20 10c0 6.1-8 11-8 11z"/>',
  link:'<path d="M14 4h6v6M20 4l-9 9M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  refresh:'<path d="M20 11a8 8 0 1 0-2 6M20 5v6h-6"/>',
  home:'<path d="M3 10l9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  chart:'<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  close:'<path d="M6 6l12 12M18 6L6 18"/>'};
const svg = n => '<svg viewBox="0 0 24 24" aria-hidden="true">' + P[n] + '</svg>';

function localFocus(){
  try { return JSON.parse(localStorage.fleet_focus || "[]"); } catch (e) { return []; }
}
const S = {view:"feed", stream:"all", topic:"", kind:"", domain:"", next:null,
  loading:false, items:[], tags:new Map(), kinds:new Map(), domains:new Map(),
  focus:localFocus(), prefs:[], activeId:""};

const $ = id => document.getElementById(id);
function esc(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
  {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }
function safeUrl(u){ const s = String(u || ""); return /^https?:\/\//i.test(s) ? s : "#"; }
function kindLabel(k){ return KINDS[k] || (k || "资源"); }
function relTime(s){
  if (!s) return "";
  const d = new Date(String(s).replace(/-/g, "/"));
  if (isNaN(d.getTime())) return s;
  const x = (Date.now() - d.getTime()) / 1000;
  if (x < 60) return "刚刚";
  if (x < 3600) return Math.floor(x/60) + " 分钟前";
  if (x < 86400) return Math.floor(x/3600) + " 小时前";
  if (x < 604800) return Math.floor(x/86400) + " 天前";
  return String(s).slice(0, 10);
}
function avatarColor(s){
  let h = 0;
  for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return "hsl(" + (h % 360) + ",52%,45%)";
}
function toast(msg){
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._tm); t._tm = setTimeout(() => t.classList.remove("show"), 2100);
}
async function api(path, opt){
  try {
    const r = await fetch(path, Object.assign({headers:H}, opt || {}));
    return await r.json().catch(() => ({ok:false, err:"返回不是 JSON"}));
  } catch (e) { return {ok:false, err:"连不上服务：" + e.message}; }
}
function post(path, body){
  return api(path, {method:"POST", headers:Object.assign({"Content-Type":"application/json"}, H),
    body:JSON.stringify(body || {})});
}
async function syncFocus(){
  const j = await api("/api/resources/stats");
  if (!j.ok) return false;
  if (Array.isArray(j.focus)) {
    S.focus = j.focus;
    localStorage.fleet_focus = JSON.stringify(S.focus);
  }
  S.prefs = j.prefs || [];
  return true;
}
async function toggleFocus(v){
  const j = await post("/api/resources/act", {id:"", action:"focus", text:v});
  if (j.ok && Array.isArray(j.focus)) {
    S.focus = j.focus;
    localStorage.fleet_focus = JSON.stringify(S.focus);
    toast(j.added ? "已关注「" + v + "」" : "已取消关注「" + v + "」");
  } else {
    const i = S.focus.indexOf(v);
    if (i >= 0) S.focus.splice(i, 1); else S.focus.push(v);
    localStorage.fleet_focus = JSON.stringify(S.focus);
    toast("离线暂存在本机，联网后以服务端为准");
  }
  if (S.view === "explore") renderExplore();
  renderChips();
}

function feedUrl(cursor){
  const p = new URLSearchParams();
  if (S.topic) p.set("topic", S.topic);
  if (S.kind) p.set("kind", S.kind);
  if (S.domain) p.set("domain", S.domain);
  p.set("limit", "12"); p.set("cursor", String(cursor)); p.set("session", SESSION);
  return "/api/resources?" + p.toString();
}

function card(x){
  const d = x.domain || "未知来源";
  const tags = (x.tags || []).slice(0, 4).map(t =>
    '<button class="tag" data-act="tag" data-value="' + esc(t) + '">' + esc(t) + '</button>').join("");
  const url = safeUrl(x.url);
  return '<article class="card" data-id="' + esc(x.id) + '">' +
    '<div class="avatar" style="background:' + avatarColor(d) + '">' +
      esc(d.replace(/^www\./, "").charAt(0).toUpperCase()) + '</div>' +
    '<div class="body">' +
      '<div class="who"><b>' + esc(d) + '</b>' +
        '<span class="kind">' + esc(kindLabel(x.kind)) + '</span>' +
        '<span class="time">· ' + esc(relTime(x.published_at)) + '</span>' +
        '<button class="more" data-act="detail" aria-label="详情">···</button></div>' +
      '<a class="title" href="' + esc(url) + '" target="_blank" rel="noreferrer">' + esc(x.title) + '</a>' +
      (x.summary ? '<p class="sum">' + esc(x.summary) + '</p>' : "") +
      (tags ? '<div class="tags">' + tags + '</div>' : "") +
      '<div class="why">' + esc(x.why || "随机看看") +
        (x.score == null ? "" : " · " + Number(x.score).toFixed(2)) + '</div>' +
      '<div class="acts">' +
        '<button class="act" data-act="comment" aria-label="评论">' + svg("comment") +
          '<span>' + (x.comments ? esc(x.comments) : "") + '</span></button>' +
        '<button class="act fav' + (x.faved ? " on" : "") + '" data-act="favorite" aria-label="收藏">' +
          svg("fav") + '<span>' + (x.faved ? "已收藏" : "") + '</span></button>' +
        '<button class="act like' + (x.liked ? " on" : "") + '" data-act="like" aria-label="赞">' +
          svg("like") + '<span>' + (x.liked ? "已赞" : "") + '</span></button>' +
        '<a class="act" href="' + esc(url) + '" target="_blank" rel="noreferrer" aria-label="打开原文">' +
          svg("link") + '</a>' +
      '</div></div></article>';
}

function skeleton(){
  return '<div class="skeleton"><i class="a"></i><div class="b"><i></i><i></i><i></i></div></div>';
}
function state(title, body, retry){
  return '<div class="state"><b>' + esc(title) + '</b>' + esc(body) +
    (retry ? '<br><button data-act="retry">重试</button>' : "") + '</div>';
}

function renderChips(){
  let list;
  if (S.stream === "focus") {
    list = S.focus.map(t => ({v:t, label:t}));
    if (!list.length) list = [{v:"", label:"还没有关注的主题"}];
  } else {
    list = [{v:"", label:"全部"}];
    const top = Array.from(S.tags.entries()).sort((a,b) => b[1]-a[1]).slice(0, 12);
    for (const [t, n] of top) list.push({v:t, label:t + " " + n});
  }
  const cur = S.kind || S.domain || S.topic;
  $("chips").innerHTML = list.map(c => {
    const on = (!c.v && !cur) || (c.v && c.v === cur) ? " on" : "";
    return '<button class="chip' + on + '" data-act="chip" data-value="' + esc(c.v) +
      '">' + esc(c.label) + '</button>';
  }).join("");
  if (cur) $("chips").insertAdjacentHTML("beforeend",
    '<button class="chip" data-act="clear">清除筛选</button>');
}

function remember(items){
  for (const x of items) {
    for (const t of (x.tags || [])) S.tags.set(t, (S.tags.get(t) || 0) + 1);
    if (x.kind) S.kinds.set(x.kind, (S.kinds.get(x.kind) || 0) + 1);
    if (x.domain) S.domains.set(x.domain, (S.domains.get(x.domain) || 0) + 1);
  }
}

async function load(reset){
  if (S.loading) return;
  if (S.stream === "focus" && !S.focus.length) {
    $("feed").innerHTML = state("还没有关注的主题", "到「分类」页点标签右侧的 +，关注的主题会出现在这里。");
    $("tail").innerHTML = ""; renderChips(); return;
  }
  if (S.stream === "focus" && !S.topic) S.topic = S.focus[0];
  S.loading = true;
  if (reset) {
    S.items = []; S.next = null;
    $("feed").innerHTML = skeleton() + skeleton() + skeleton();
    $("tail").innerHTML = "";
  }
  const j = await api(feedUrl(reset ? 0 : (S.next || 0)));
  if (!j.ok) {
    $("feed").innerHTML = state("读取失败", j.err || "服务没有返回结果", true);
    $("tail").innerHTML = "";
  } else {
    const items = j.items || [];
    remember(items);
    S.items = reset ? items : S.items.concat(items);
    S.next = j.next_cursor;
    if (reset) $("feed").innerHTML = "";
    $("feed").insertAdjacentHTML("beforeend", items.length
      ? items.map(card).join("")
      : state("这里还没有资源", "让采集继续跑，或换个筛选条件看看。"));
    $("tail").innerHTML = (S.next == null)
      ? (items.length ? "已经到底了" : "")
      : '<button data-act="more">加载更多</button>';
    renderChips();
  }
  S.loading = false;
}

function applyFilter(f){
  S.topic = f.topic || ""; S.kind = f.kind || ""; S.domain = f.domain || "";
  S.stream = "all";
  document.querySelectorAll(".tabs button").forEach(b =>
    b.classList.toggle("on", b.dataset.stream === "all"));
  switchView("feed");
  window.scrollTo({top:0, behavior:"smooth"});
  load(true);
}

async function doAct(id, action, btn){
  const j = await post("/api/resources/act", {id:id, action:action});
  if (!j.ok) { toast(j.err || "没记上"); return; }
  if (j.duplicate) toast("已经记过了");
  if (btn) {
    btn.classList.add("on");
    const span = btn.querySelector("span");
    if (span) span.textContent = action === "like" ? "已赞" : "已收藏";
  }
  toast(action === "like" ? "已赞，会抬高这类主题的权重" :
        action === "favorite" ? "已收进默认收藏夹" : "已记下");
}

async function openDetail(id){
  const j = await api("/api/resources/detail?id=" + encodeURIComponent(id));
  if (!j.ok) { toast(j.err || "详情读不到"); return; }
  const x = j.item;
  const tags = (x.tags || []).map(t => '<span class="tag">' + esc(t) + '</span>').join(" ");
  const origins = (x.origins || []).map(o =>
    '<div class="origin">· ' + esc(o.source_id) + ' → <a href="' + esc(safeUrl(o.url)) +
    '" target="_blank" rel="noreferrer">' + esc(o.url) + '</a></div>').join("");
  const acts = (x.actions || []).map(a =>
    '<div class="origin">· ' + esc(a.action) + " " + esc(a.text) + '</div>').join("");
  $("detail").innerHTML =
    '<button class="close" data-act="close" aria-label="关闭">' + svg("close") + '</button>' +
    '<h2>' + esc(x.title) + '</h2>' +
    '<div class="who"><b>' + esc(x.domain) + '</b><span class="kind">' +
      esc(kindLabel(x.kind)) + '</span><span class="time">· ' + esc(x.published_at) + '</span></div>' +
    '<div class="tags" style="margin-top:10px">' + tags + '</div>' +
    (x.summary ? '<p class="sum" style="-webkit-line-clamp:unset">' + esc(x.summary) + '</p>' : "") +
    '<a class="out" href="' + esc(safeUrl(x.url)) + '" target="_blank" rel="noreferrer">打开原文</a>' +
    '<div class="sec"><h3>出处 ' + (x.origins || []).length + ' 个 · 质量 ' +
      Number(x.quality || 0).toFixed(2) + '</h3>' + (origins || '<div class="origin">无</div>') + '</div>' +
    '<div class="sec"><h3>我的行为</h3>' + (acts || '<div class="origin">还没有行为</div>') + '</div>';
  $("detail").hidden = false; $("mask").hidden = false;
}

function openComposer(id){
  S.activeId = id; $("commentText").value = ""; $("commentHint").textContent = "会落库，并调整标签权重";
  $("composer").hidden = false; $("mask").hidden = false;
  setTimeout(() => $("commentText").focus(), 30);
}
async function sendComment(){
  const text = $("commentText").value.trim();
  if (!text) { toast("写一句再记"); return; }
  const j = await post("/api/resources/act", {id:S.activeId, action:"comment", text:text});
  if (!j.ok) { toast(j.err || "没记上"); return; }
  const el = document.querySelector('.card[data-id="' + S.activeId + '"] [data-act="comment"] span');
  if (el) el.textContent = String((parseInt(el.textContent, 10) || 0) + 1);
  closeOverlays(); toast("已记下这句");
}
function closeOverlays(){
  $("detail").hidden = true; $("composer").hidden = true; $("mask").hidden = true;
}

function renderExplore(){
  const sorted = m => Array.from(m.entries()).sort((a,b) => b[1]-a[1]);
  const tags = sorted(S.tags);
  const kinds = sorted(S.kinds);
  const domains = sorted(S.domains).slice(0, 12);
  const grid = (items, type, labelFn, focusable) => items.length
    ? '<div class="grid">' + items.map(([v,n]) =>
        '<button class="gitem" data-act="pick" data-type="' + type + '" data-value="' + esc(v) + '">' +
        '<span>' + esc(labelFn(v)) + '</span><em>' + n + '</em>' +
        (focusable ? '<span class="fav-btn' + (S.focus.indexOf(v) >= 0 ? " on" : "") +
          '" data-act="focus" data-value="' + esc(v) + '">' +
          (S.focus.indexOf(v) >= 0 ? "✓" : "+") + '</span>' : "") +
        '</button>').join("") + '</div>'
    : '<div class="note">这一组暂时是空的，先回信息流刷几条。</div>';
  $("exploreBody").innerHTML =
    '<h2 class="sect">领域 / 主题标签</h2>' +
    '<p class="note">按资源内容组织，不按平台组织。右侧 + 是关注，关注列表存在服务端，换手机也还在。</p>' +
    grid(tags, "topic", v => v, true) +
    '<h2 class="sect">资源类型</h2>' +
    '<p class="note">一条资源可以同时属于多个标签，但只有一个类型。</p>' +
    grid(kinds, "kind", kindLabel, false) +
    '<h2 class="sect">来源站点</h2>' +
    '<p class="note">来源是筛选项，不是主分类。来源信息在资源详情里完整保留。</p>' +
    grid(domains, "domain", v => v, false) +
    '<div class="ver">资源界面 ' + UI_VERSION + ' · 按内容分类 · 素材随包自带</div>';
}

function switchView(v){
  S.view = v;
  for (const name of ["feed", "explore"]) $("view-" + name).hidden = name !== v;
  document.querySelectorAll(".bottom button").forEach(b =>
    b.classList.toggle("on", b.dataset.view === v));
  $("top").querySelector(".tabs").hidden = v !== "feed";
  $("chips").hidden = v !== "feed";
  if (v === "explore") renderExplore();
}

$("searchBtn").innerHTML = svg("search");
$("refreshBtn").innerHTML = svg("refresh");
document.querySelector('.bottom [data-view="feed"]').innerHTML = svg("home") + "<span>信息流</span>";
document.querySelector('.bottom [data-view="explore"]').innerHTML = svg("grid") + "<span>分类</span>";
$("closeComposer").innerHTML = svg("close");

$("feed").addEventListener("click", e => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const a = b.dataset.act;
  if (a === "retry" || a === "more") { load(a === "retry"); return; }
  if (a === "tag") { applyFilter({topic:b.dataset.value}); return; }
  const el = b.closest(".card");
  const id = el ? el.dataset.id : "";
  if (a === "detail") openDetail(id);
  else if (a === "comment") openComposer(id);
  else if (a === "like" || a === "favorite") doAct(id, a, b);
});

$("chips").addEventListener("click", e => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  if (b.dataset.act === "clear") { applyFilter({}); return; }
  applyFilter({topic:b.dataset.value});
});

$("view-explore").addEventListener("click", e => {
  const f = e.target.closest('[data-act="focus"]');
  if (f) { toggleFocus(f.dataset.value); return; }
  const b = e.target.closest('[data-act="pick"]');
  if (b) applyFilter({[b.dataset.type]: b.dataset.value});
});

document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  S.stream = b.dataset.stream; S.topic = ""; S.kind = ""; S.domain = "";
  document.querySelectorAll(".tabs button").forEach(x => {
    x.classList.toggle("on", x === b);
    x.setAttribute("aria-selected", x === b ? "true" : "false");
  });
  renderChips(); load(true);
}));
document.querySelectorAll(".bottom button").forEach(b =>
  b.addEventListener("click", () => switchView(b.dataset.view)));

$("refreshBtn").addEventListener("click", () => load(true));
$("searchBtn").addEventListener("click", () => {
  $("searchRow").hidden = !$("searchRow").hidden;
  if (!$("searchRow").hidden) $("q").focus();
});
let qt;
$("q").addEventListener("input", () => {
  clearTimeout(qt);
  qt = setTimeout(() => applyFilter({topic:$("q").value.trim()}), 420);
});
$("q").addEventListener("keydown", e => { if (e.key === "Enter") { clearTimeout(qt);
  applyFilter({topic:$("q").value.trim()}); e.preventDefault(); } });
$("sendComment").addEventListener("click", sendComment);
$("mask").addEventListener("click", closeOverlays);
$("detail").addEventListener("click", e => { if (e.target.closest('[data-act="close"]')) closeOverlays(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeOverlays(); });

if ("IntersectionObserver" in window) {
  new IntersectionObserver(es => {
    if (es.some(x => x.isIntersecting) && S.view === "feed" && S.next != null && !S.loading) load(false);
  }, {rootMargin:"240px"}).observe($("tail"));
}
let touchY = 0;
document.addEventListener("touchstart", e => { touchY = e.touches[0].clientY; }, {passive:true});
document.addEventListener("touchend", e => {
  const dy = e.changedTouches[0].clientY - touchY;
  if (window.scrollY <= 0 && dy > 90 && S.view === "feed") load(true);
}, {passive:true});

(async () => {
  await syncFocus();
  renderChips();
  load(true);
})();
</script>
</body>
</html>"""

_HERO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "resource_hero_b64.txt")
try:
    with open(_HERO_PATH, "r", encoding="ascii") as _f:
        _HERO = _f.read().strip()
except OSError:
    _HERO = ""

PAGE = PAGE.replace(
    "__HERO_IMG__",
    '<img class="hero" alt="" src="data:image/jpeg;base64,' + _HERO + '">'
    if _HERO else "")
