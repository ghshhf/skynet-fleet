/* ==========================================================================
   HTML 渲染后端（Node）：HTML → 确定性逐帧 → MP4
   --------------------------------------------------------------------------
   跟 HyperFrames / Remotion 同一类东西的内核：**可寻址动画 + 逐帧捕捉**。
   但本仓不引外部框架，原因有三：
     ① 本仓产品本身就是纯手写 HTML/CSS/JS、零构建、断网可用，
        渲染后端也该是同一套技术栈 —— 不引入 npm 依赖、不进 git；
     ② 只需要「把动画拨到某一时刻」这一件事，Web Animations API 就够，
        不必为了 seek 能力引入 GSAP；
     ③ 少一个依赖就少一处会坏的地方。

   确定性从哪来：不用真实时间播放，而是把页面上所有动画 pause 住，
   渲染第 f 帧时统一设 currentTime = f/fps*1000。
   同一份 HTML ⇒ 每一帧都一样 ⇒ 能进 git、能 diff、能做像素回归。

   用法：
     node htmlrender.js --html in.html --frames <dir> --out out.mp4 \
          --width 1920 --height 1080 --fps 30 --duration 5
   退出码：0 成功；非 0 失败（stderr 写明原因）。
   ========================================================================== */
"use strict";

const path = require("path");
const fs = require("fs");
const { spawnSync } = require("child_process");

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  if (i < 0) { return dflt; }
  const v = process.argv[i + 1];
  if (v === undefined || v.startsWith("--")) { return true; }
  return v;
}

const HTML_PATH = arg("html", "");
const FRAMES = arg("frames", "");
const OUT = arg("out", "");
const W = Number(arg("width", 1920));
const H = Number(arg("height", 1080));
const FPS = Number(arg("fps", 30));
const DURATION = Number(arg("duration", 5));
const FFMPEG = process.env.FLEET_FFMPEG ||
  "D:/tools/ffmpeg/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe";

if (!HTML_PATH || !FRAMES || !OUT) {
  console.error("用法：--html <in.html> --frames <dir> --out <out.mp4>");
  process.exit(2);
}

/* ---------------------------------------------------------------------------
   确定性 lint：在渲染之前先查「会不会渲出不一样的画面」。
   这几条是 Video as Code 的硬约束（跟 HyperFrames 的 determinism contract 同源）：
     ① Date.now() / new Date()      ⇒ 每次跑画面不一样
     ② 没播种的 Math.random()        ⇒ 每次跑画面不一样
     ③ 渲染时发网络请求               ⇒ 每次跑拿到的视觉状态不一样
   查出来就**直接拒绝渲染**，别渲出一部「这次好看、下次不一样」的片子。
   --------------------------------------------------------------------------- */
function lintDeterminism(src) {
  const problems = [];
  // 剥掉注释再查。
  // 🔴 实测踩出来的坑：HTML 注释 <!-- --> 和 JS 注释都要剥。
  // 早期版本只剥了 JS 的两种注释，模板里那段「不许用 Date.now()」的说明
  // 被当成真代码 ⇒ 自己的模板渲不出来（假红）。
  // 反过来也别剥过头：只剥注释，字符串字面量里的内容不动
  // （有人在字符串里写 "Date.now()" 只是个提示词，不是真调用）。
  const noComment = src
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ");
  if (/Date\.now\s*\(|new\s+Date\s*\(/.test(noComment)) {
    problems.push("用了 Date.now() / new Date()：每次渲染画面会不一样（改成固定值或从 HTML 变量传进来）");
  }
  // Math.random：有播种的写法（Math.random(seed) 之类）不常见，直接提示
  if (/Math\.random\s*\(/.test(noComment)) {
    problems.push("用了 Math.random()：每次渲染画面会不一样（用固定序列或从 HTML 变量传进来）");
  }
  if (/\bfetch\s*\(|XMLHttpRequest|EventSource|WebSocket/.test(noComment)) {
    problems.push("渲染时会发网络请求：视觉状态会随网络变化（把数据内联进 HTML）");
  }
  return problems;
}

(async function main() {
  const src = fs.readFileSync(HTML_PATH, "utf-8");
  const problems = lintDeterminism(src);
  if (problems.length) {
    console.error("确定性检查没过：");
    problems.forEach((p) => console.error("  x " + p));
    process.exit(3);
  }

  let chromium;
  try {
    require("module").Module._initPaths();
    ({ chromium } = require("playwright"));
  } catch (e) {
    console.error("加载 playwright 失败：" + e.message);
    process.exit(4);
  }

  fs.mkdirSync(FRAMES, { recursive: true });
  // 🔴 两个 flag 是确定性的一部分：transform 亚像素定位与字体子像素定位
  // 会让同一份 HTML 两次渲出不同像素（实测 linear 动画 59/60 帧边缘差
  // 2/255——逻辑完全确定，是光栅器的亚像素行为）。关掉子像素定位后
  // 亚像素抖动消失，逐字节一致才真正成立。
  const browser = await chromium.launch({
    args: ["--disable-font-subpixel-positioning", "--disable-lcd-text"],
  });
  const page = await browser.newPage({
    viewport: { width: W, height: H },
    deviceScaleFactor: 1,
  });
  await page.goto("file:///" + path.resolve(HTML_PATH).replace(/\\/g, "/"), {
    waitUntil: "load",
  });
  // 给字体和布局一个稳定的机会：等 document.fonts.ready
  await page.evaluate(() => (document.fonts && document.fonts.ready) ? document.fonts.ready : null);
  await page.waitForTimeout(200);

  // 把所有动画变成可拨的钟
  const anims = await page.evaluate(() => {
    const a = document.getAnimations();
    a.forEach((x) => { try { x.pause(); x.currentTime = 0; } catch (_) { } });
    return a.length;
  });
  console.log("[htmlrender] 动画 " + anims + " 个，尺寸 " + W + "x" + H +
              " @" + FPS + "fps，时长 " + DURATION + "s");

  const total = Math.max(1, Math.round(DURATION * FPS));
  const t0 = Date.now();
  for (let f = 0; f < total; f++) {
    const t = (f / FPS) * 1000;
    await page.evaluate(async (tt) => {
      document.getAnimations().forEach((a) => { try { a.currentTime = tt; } catch (_) { } });
      // 🔴 拨完时间必须等两帧再截图：currentTime 的设置要过渲染管线才落到
      // 画面上，设完立刻 screenshot 是 race——偶发截到旧帧 ⇒ 同一份 HTML
      // 两次渲染不一致（t23 实测红过一次）。两帧 = 设置生效 + 合成完成。
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    }, t);
    const name = "f" + String(f).padStart(4, "0") + ".png";
    await page.screenshot({ path: path.join(FRAMES, name) });
  }
  const shotMs = Date.now() - t0;
  await browser.close();

  const n = fs.readdirSync(FRAMES).filter((f) => f.endsWith(".png")).length;
  console.log("[htmlrender] 逐帧 " + n + " 帧 / " + (shotMs / 1000).toFixed(1) + "s = " +
              (n / (shotMs / 1000)).toFixed(1) + " 帧/秒");

  /* ---- ffmpeg 合成 ---- */
  if (!fs.existsSync(FFMPEG)) {
    console.error("没找到 ffmpeg：" + FFMPEG);
    process.exit(5);
  }
  const r = spawnSync(FFMPEG, [
    "-y", "-v", "error", "-framerate", String(FPS),
    "-i", path.join(FRAMES, "f%04d.png"),
    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
    "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT,
  ], { encoding: "utf-8" });
  if (r.status !== 0) {
    console.error("ffmpeg 合成失败（rc=" + r.status + "）：" + (r.stderr || "").slice(0, 600));
    process.exit(6);
  }

  const report = { frames: n, fps: FPS, width: W, height: H, duration: DURATION,
                   shotMs: shotMs, lint: { ok: true, problems: [] } };
  const rpt = path.join(path.dirname(FRAMES), "report.json");
  try { fs.writeFileSync(rpt, JSON.stringify(report, null, 2)); } catch (_) { }
  console.log("[htmlrender] 完成 → " + OUT + "（" +
              (fs.statSync(OUT).size / 1024).toFixed(0) + " KB）");
})().catch((e) => {
  console.error("渲染失败：" + (e && e.stack ? e.stack : e));
  process.exit(1);
});
