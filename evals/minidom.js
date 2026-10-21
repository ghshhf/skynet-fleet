/* 门禁用的迷你 DOM：真选择器 + 真 DOM 树。
 *
 * 为什么不用手写假元素：app.js 里有一堆 `document.querySelectorAll(".sb-item")`
 * 这类按选择器找元素的代码。假 DOM 要是只会 `getElementById`，
 * 这些代码就永远返回空数组 ⇒ 门禁测不到它们 ⇒ 页面真出问题时门禁照样绿。
 * 这是「门禁骗人」的第四种形态：假环境太弱，把真 bug 藏起来了。
 *
 * 所以这里做两件事：
 *   ① 用一个极小的 HTML 解析器把 ui/index.html 读成真 DOM 树
 *      （标签、id、class、属性、父子关系、可见文本）；
 *   ② 实现够用的选择器：后代组合（空格）、并列（逗号）、
 *      标签名 / #id / .class / [attr] / [attr=value]。
 *
 * 不做的事：布局（没有 offsetWidth）、事件冒泡、样式计算。
 * 真要测那些，用 tools/uicheck/ 的真浏览器（Playwright）。
 */
"use strict";

/* ---------------- 极小 HTML 解析器 ---------------- */

const VOID_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img",
  "input", "link", "meta", "param", "source", "track", "wbr"]);

function makeEl(tag, attrs) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    _attrs: Object.assign({}, attrs || {}),
    _text: "",
    children: [],
    parentNode: null,
    style: {},
    value: "",
    hidden: false,
    disabled: false,
    title: "",
    scrollTop: 0,
    scrollHeight: 40,
    _h: {},
  };
  el.classList = makeClassList(el);
  Object.defineProperty(el, "textContent", {
    get() {
      if (el.children.length) {
        return el.children.map((c) => c.textContent).join("");
      }
      return el._text;
    },
    set(v) { el._text = String(v); el.children = []; },
    configurable: true,
  });
  Object.defineProperty(el, "className", {
    get() { return el._attrs["class"] || ""; },
    set(v) { el._attrs["class"] = String(v); el.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    configurable: true,
  });
  Object.defineProperty(el, "id", {
    get() { return el._attrs["id"] || ""; },
    set(v) { el._attrs["id"] = String(v); },
    configurable: true,
  });
  el.getAttribute = (n) => (n in el._attrs ? el._attrs[n] : null);
  el.setAttribute = (n, v) => { el._attrs[n] = String(v); };
  el.removeAttribute = (n) => { delete el._attrs[n]; };
  el.hasAttribute = (n) => n in el._attrs;
  el.addEventListener = (type, fn) => { (el._h[type] = el._h[type] || []).push(fn); };
  el.removeEventListener = () => { };
  el.appendChild = (c) => { c.parentNode = el; el.children.push(c); return c; };
  el.removeChild = (c) => { el.children = el.children.filter((x) => x !== c); c.parentNode = null; return c; };
  el.focus = () => { };
  el.blur = () => { };
  el.matches = (sel) => matchesCompound(el, parseCompound(sel));
  el.closest = (sel) => {
    let n = el;
    while (n) {
      if (matchesCompound(n, parseCompound(sel))) { return n; }
      n = n.parentNode;
    }
    return null;
  };
  el.querySelector = (sel) => queryAll(el, sel, true)[0] || null;
  el.querySelectorAll = (sel) => queryAll(el, sel, false);
  el.getElementsByClassName = (c) => queryAll(el, "." + c, false);
  el.click = () => { (el._h.click || []).forEach((f) => f({ target: el, preventDefault() { } })); };
  return el;
}

function makeClassList(el) {
  const cl = {
    _s: new Set(),
    add(...cs) { cs.forEach((c) => cl._s.add(c)); sync(); },
    remove(...cs) { cs.forEach((c) => cl._s.delete(c)); sync(); },
    contains(c) { return cl._s.has(c); },
    toggle(c, f) {
      const on = f === undefined ? !cl._s.has(c) : !!f;
      if (on) { cl._s.add(c); } else { cl._s.delete(c); }
      sync();
      return on;
    },
  };
  function sync() {
    el._attrs["class"] = Array.from(cl._s).join(" ");
  }
  return cl;
}

function parseHTML(html) {
  const root = makeEl("#root", {});
  const stack = [root];
  const re = /<!--[\s\S]*?-->|<!DOCTYPE[^>]*>|<\/([a-zA-Z][\w:-]*)\s*>|<([a-zA-Z][\w:-]*)((?:\s+[^\s"'>\/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*)\s*(\/?)>/gi;
  let last = 0, m;
  while ((m = re.exec(html)) !== null) {
    // 标签之间的文本
    const text = html.slice(last, m.index);
    if (text.trim()) {
      const t = makeEl("#text", {});
      t._text = text.replace(/\s+/g, " ");
      stack[stack.length - 1].appendChild(t);
    }
    last = re.lastIndex;
    if (m[0].startsWith("<!--") || /^<!DOCTYPE/i.test(m[0])) { continue; }
    if (m[1]) { // 闭合标签
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tagName === m[1].toUpperCase()) { stack.length = i; break; }
      }
      continue;
    }
    const tag = m[2].toLowerCase();
    const attrs = {};
    const attrRe = /([^\s"'>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
    let a;
    while ((a = attrRe.exec(m[3] || "")) !== null) {
      attrs[a[1]] = a[2] !== undefined ? a[2] : (a[3] !== undefined ? a[3] : (a[4] !== undefined ? a[4] : ""));
    }
    const el = makeEl(tag, attrs);
    if (attrs["class"]) {
      el.classList._s = new Set(attrs["class"].split(/\s+/).filter(Boolean));
    }
    stack[stack.length - 1].appendChild(el);
    if (!VOID_TAGS.has(tag) && !m[4]) { stack.push(el); }
  }
  return root;
}

/* ---------------- 选择器引擎 ---------------- */

function parseCompound(sel) {
  // 支持：tag #id .class [attr] [attr=value]
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:([~^$*|]?=)"?([^"\]]*)"?)?\]/g;
  let m;
  while ((m = re.exec(sel)) !== null) {
    if (m[1]) { out.tag = m[1].toUpperCase(); }
    else if (m[2]) { out.id = m[2]; }
    else if (m[3]) { out.classes.push(m[3]); }
    else if (m[4]) { out.attrs.push([m[4], m[5] || null, m[6] === undefined ? null : m[6]]); }
  }
  return out;
}

function matchesCompound(el, c) {
  if (!el || el.tagName === "#TEXT") { return false; }
  if (c.tag && el.tagName !== c.tag) { return false; }
  if (c.id && el.id !== c.id) { return false; }
  for (const cls of c.classes) {
    if (!el.classList.contains(cls)) { return false; }
  }
  for (const [name, op, val] of c.attrs) {
    const v = el.getAttribute(name);
    if (v === null) { return false; }
    if (op === null) { continue; }
    if (op === "=" && v !== val) { return false; }
    if (op === "*=" && v.indexOf(val) < 0) { return false; }
    if (op === "^=" && v.indexOf(val) !== 0) { return false; }
    if (op === "$=" && v.slice(-val.length) !== val) { return false; }
  }
  return true;
}

/* 选择器：逗号并列 + 空格后代 */
function queryAll(root, sel, one) {
  const results = [];
  const groups = String(sel).split(",").map((s) => s.trim()).filter(Boolean);
  for (const group of groups) {
    const parts = group.split(/\s+/).map(parseCompound);
    walk(root, parts, 0, results);
  }
  return results;
}

function walk(node, parts, idx, results) {
  if (idx >= parts.length) { return; }
  const c = parts[idx];
  for (const child of node.children) {
    if (child.tagName === "#TEXT") { continue; }
    if (matchesCompound(child, c)) {
      if (idx === parts.length - 1) {
        results.push(child);
      } else {
        // 后代组合逆序：剩下的段由祖先链满足
        if (hasAncestorChain(child, parts, idx - 1)) {
          walk(child, parts, idx + 1, results);
        }
      }
    }
    // 后代组合：子树的任何后代都可以是下一段
    walk(child, parts, idx, results);
  }
}

/* 顶层 document 级查询：从 #root 往下找 */
function queryAllDoc(root, sel) {
  const results = [];
  const groups = String(sel).split(",").map((s) => s.trim()).filter(Boolean);
  const all = [];
  collectAll(root, all);
  for (const group of groups) {
    const parts = group.split(/\s+/).map(parseCompound);
    // 后代组合是逆序的：元素自己匹配**最后一段**（离它最近的那段），
    // 祖先链依次匹配**前面各段**。
    // `#tabBar .tab` ＝ 祖先是 #tabBar 的 .tab 元素。
    const last = parts[parts.length - 1];
    for (const el of all) {
      if (!matchesCompound(el, last)) { continue; }
      if (parts.length === 1) { results.push(el); continue; }
      if (hasAncestorChain(el, parts, parts.length - 2)) { results.push(el); }
    }
  }
  return results;
}

/* 祖先链能不能依次匹配 parts[from..0]（从近到远） */
function hasAncestorChain(el, parts, from) {
  if (from < 0) { return true; }
  let n = el.parentNode;
  while (n) {
    if (matchesCompound(n, parts[from]) && hasAncestorChain(n, parts, from - 1)) {
      return true;
    }
    n = n.parentNode;
  }
  return false;
}

function collectAll(node, out) {
  for (const child of node.children) {
    if (child.tagName === "#TEXT") { continue; }
    out.push(child);
    collectAll(child, out);
  }
}

module.exports = {
  makeEl, parseHTML, makeClassList,
  parseCompound, matchesCompound, collectAll, queryAllDoc,
};
