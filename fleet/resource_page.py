# -*- coding: utf-8 -*-
"""resource_page —— 资源流的单页前端（零依赖、可离线）。

不是营销页：只做计划书 MVP 的五个动作——刷流、按主题筛、看详情、赞/收藏/评论。
页面本身不含数据；token 从 URL 的 `#t=` 读（不进请求行），再放进 `X-Fleet-Token`。
"""

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>天网 · 资源流</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--txt:#e8eaf0;--dim:#8b93a7;--acc:#5aa9ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  padding:14px 18px;background:rgba(15,17,21,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0 8px 0 0;font-weight:650}
input{background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:9px;
  padding:8px 11px;min-width:180px;outline:none}
input:focus{border-color:var(--acc)}
button{background:var(--card);border:1px solid var(--line);color:var(--txt);border-radius:9px;
  padding:8px 12px;cursor:pointer}
button:hover{border-color:var(--acc)}
button.on{color:var(--acc);border-color:var(--acc)}
main{max-width:860px;margin:0 auto;padding:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 12px}
.card h2{font-size:17px;margin:0 0 6px;line-height:1.35}
.card h2 a{color:var(--txt);text-decoration:none}
.card h2 a:hover{color:var(--acc)}
.meta{color:var(--dim);font-size:12.5px;display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.tag{background:#202634;color:#a9c7ff;border-radius:999px;padding:1px 9px;font-size:12px}
.sum{color:#c3c9d6;margin:0 0 10px}
.why{color:#7f8aa3;font-size:12px;margin-bottom:10px}
.acts{display:flex;gap:8px;flex-wrap:wrap}
.acts button{padding:5px 11px;font-size:13px}
#more{text-align:center;padding:10px 0 40px}
#stat{color:var(--dim);font-size:12.5px;margin-left:auto}
#detail{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;padding:18px;z-index:5}
#detail.show{display:flex}
.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;max-width:720px;width:100%;
  max-height:86vh;overflow:auto;padding:22px}
.panel h2{margin:0 0 10px}
.panel .close{float:right}
.origin{color:var(--dim);font-size:12.5px;word-break:break-all;margin:3px 0}
.actlog{border-top:1px solid var(--line);margin-top:14px;padding-top:10px;color:#b9c0cf;font-size:13px}
.empty{color:var(--dim);text-align:center;padding:60px 0}
</style>
</head>
<body>
<header>
  <h1>资源流</h1>
  <input id="q" placeholder="主题，如 AI / 工具 / 论文">
  <button onclick="reload()">刷新</button>
  <button onclick="importNf()">导入已有资源</button>
  <span id="stat"></span>
</header>
<main><div id="feed"></div><div id="more"></div></main>
<aside id="detail" onclick="if(event.target===this)closeDetail()"><div class="panel" id="panel"></div></aside>
<script>
const TOKEN = (location.hash.match(/t=([^&]+)/) || [])[1] || localStorage.fleet_token || "";
if (TOKEN) localStorage.fleet_token = TOKEN;
const H = TOKEN ? {"X-Fleet-Token": TOKEN} : {};
const SESSION = "web-" + (localStorage.fleet_session || (localStorage.fleet_session = Date.now().toString(36)));
let cursor = 0, loading = false;

function esc(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
  {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

async function api(path, opt){
  const r = await fetch(path, Object.assign({headers: H}, opt || {}));
  const j = await r.json().catch(() => ({ok:false, err:"返回不是 JSON"}));
  if (!j.ok && j.err) console.warn(path, j.err);
  return j;
}

function card(x){
  return `<article class="card">
    <h2><a href="${esc(x.url)}" target="_blank" rel="noreferrer">${esc(x.title)}</a></h2>
    <div class="meta"><span>${esc(x.domain)}</span><span>${esc(x.published_at)}</span>
      ${(x.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join("")}</div>
    <p class="sum">${esc(x.summary || "（暂无摘要，点标题去看原文）")}</p>
    <div class="why">${esc(x.why || "")} · 分数 ${Number(x.score||0).toFixed(2)}</div>
    <div class="acts">
      <button onclick="act('${esc(x.id)}','like',this)">赞</button>
      <button onclick="act('${esc(x.id)}','favorite',this)">收藏</button>
      <button onclick="comment('${esc(x.id)}')">评论</button>
      <button onclick="detail('${esc(x.id)}')">详情</button>
    </div></article>`;
}

async function load(reset){
  if (loading) return;
  loading = true;
  if (reset) { cursor = 0; document.getElementById("feed").innerHTML = ""; }
  const topic = encodeURIComponent(document.getElementById("q").value.trim());
  const j = await api(`/api/resources?topic=${topic}&limit=12&cursor=${cursor}&session=${SESSION}`);
  if (j.ok) {
    document.getElementById("feed").insertAdjacentHTML("beforeend",
      j.items.length ? j.items.map(card).join("") : '<div class="empty">这里还没有资源。先点「导入已有资源」，或让采集继续跑。</div>');
    cursor = j.next_cursor;
    document.getElementById("stat").textContent = `共 ${j.total} 条`;
    document.getElementById("more").innerHTML = cursor == null ? "" :
      '<button onclick="load(false)">加载更多</button>';
  } else {
    document.getElementById("feed").innerHTML = `<div class="empty">${esc(j.err || "读取失败")}</div>`;
  }
  loading = false;
}

function reload(){ load(true); }

async function act(id, action, btn){
  const j = await api("/api/resources/act", {method:"POST",
    headers:Object.assign({"Content-Type":"application/json"}, H),
    body:JSON.stringify({id, action})});
  if (j.ok) {
    if (btn) { btn.classList.add("on"); btn.textContent = action === "like" ? "已赞" : "已收藏"; }
    if (j.duplicate) console.log("already", action);
  }
}

async function comment(id){
  const text = prompt("写一句评论（会落库，用来调整标签权重）");
  if (!text) return;
  const j = await api("/api/resources/act", {method:"POST",
    headers:Object.assign({"Content-Type":"application/json"}, H),
    body:JSON.stringify({id, action:"comment", text})});
  if (j.ok) alert("已记下");
}

async function detail(id){
  const j = await api(`/api/resources/detail?id=${encodeURIComponent(id)}`);
  if (!j.ok) return;
  const x = j.item;
  document.getElementById("panel").innerHTML = `
    <button class="close" onclick="closeDetail()">关闭</button>
    <h2>${esc(x.title)}</h2>
    <div class="meta"><span>${esc(x.domain)}</span><span>${esc(x.kind)}</span>
      ${x.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join("")}</div>
    <p class="sum">${esc(x.summary)}</p>
    <p><a href="${esc(x.url)}" target="_blank" rel="noreferrer">打开原文</a></p>
    <div class="origin">出处 ${x.origins.length} 个：</div>
    ${x.origins.map(o=>`<div class="origin">· ${esc(o.source_id)} → ${esc(o.url)}</div>`).join("")}
    <div class="actlog">${x.actions.length ? x.actions.map(a=>`· ${esc(a.action)} ${esc(a.text)}`).join("<br>") : "还没有行为"}</div>`;
  document.getElementById("detail").classList.add("show");
}

function closeDetail(){ document.getElementById("detail").classList.remove("show"); }

async function importNf(){
  const j = await api("/api/resources/import", {method:"POST",
    headers:Object.assign({"Content-Type":"application/json"}, H), body:"{}"});
  alert(j.ok ? `导入完成：新增 ${j.new}，合并 ${j.merged}` : (j.err || "导入失败"));
  if (j.ok) reload();
}

document.getElementById("q").addEventListener("keydown", e => { if (e.key === "Enter") reload(); });
load(true);
</script>
</body>
</html>"""
