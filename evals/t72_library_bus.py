"""门禁 t72：本地资料库三台设备（论文库 / 域名库 / 文档转换）接进总线的验收。

为什么需要这条门禁
------------------
2026-11-03 的模块树盘点发现三处「0 能力」：论文库、域名库、任意格式文档，
共同点是**数据已经在本机、总线里却没有出口**——agent 想用只能重新上网，
等于把已经付过的带宽和时间再付一遍。

这条门禁守四件事：

  一、接上了：能力在 CAPABILITIES 总表里，且能被 reg.call 分派到对应设备
     （踩过 signup 那个坑：设备 reg.add 了、能力没进总表 ⇒ 查表全落空）
  二、搜得准：域名按标签边界命中（"tts" 不许命中 bhatts.org），论文按分数排序
  三、按需解析：paper.text / doc.convert 只在被调用时读一个文件，
     **不落 .txt、不建索引、不扫全库**（主人定的边界，不许自作聪明）
  四、坏环境不崩：库不存在 / 行是坏的 / 文件不存在 —— 一律结构化失败，不抛异常

    python evals/t72_library_bus.py
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_OK = _FAIL = 0


def check(cond, msg):
    global _OK, _FAIL
    if cond:
        _OK += 1
    else:
        _FAIL += 1
        print("  x %s" % msg)


def sec(t):
    print("\n== %s" % t)


def make_pdf(path, text="HELLO SKYNET"):
    """手搓一个最小合法 PDF（带 xref）：门禁不依赖第三方生成器。"""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        None,
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream = ("BT /F1 14 Tf 20 100 Td (%s) Tj ET" % text).encode()
    objs[3] = b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, body in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += (b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    with open(path, "wb") as f:
        f.write(bytes(out))


def build_paper_lib(base):
    os.makedirs(os.path.join(base, "pdf"), exist_ok=True)
    make_pdf(os.path.join(base, "pdf", "2501.00001.pdf"), "HELLO SKYNET")
    recs = [
        {"id": "2501.00001", "title": "Efficient Quantization for Small Language Models",
         "abstract": "We study 4-bit quantization and KV cache compression for on-device LLM.",
         "authors": "A. Author", "date": "2026-11-01", "tags": ["compression"],
         "source": "arxiv-rss", "links": []},
        {"id": "2501.00002", "title": "Tool Use in LLM Agents",
         "abstract": "An agent framework with tool calling and self-improvement.",
         "authors": "B. Author", "date": "2026-11-02", "tags": ["agent"],
         "source": "hf-daily", "links": []},
        {"id": "2501.00003", "title": "Retrieval Augmented Memory",
         "abstract": "Long context memory for assistants, RAG based.",
         "authors": "C. Author", "date": "2026-10-30", "tags": ["memory-rag"],
         "source": "arxiv-rss", "links": []},
    ]
    with open(os.path.join(base, "papers.jsonl"), "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write("{坏行 不是 json\n")          # 坏行必须被跳过，不许整库不可用
    return recs


def build_domain_lib(base):
    os.makedirs(base, exist_ok=True)
    cls = [
        {"domain": "tts.example.com", "category": "AI工具/平台", "title": "TTS Studio",
         "desc": "text to speech synthesis", "basis": "ai-manual", "url": "https://tts.example.com"},
        {"domain": "aditya.bhatts.org", "category": "高校/实验室", "title": "Aditya Lab",
         "desc": "personal page", "basis": "ai-lex", "url": "https://aditya.bhatts.org"},
        {"domain": "github.com", "category": "代码仓库/开源项目", "title": "GitHub",
         "desc": "code hosting", "basis": "rule", "url": "https://github.com"},
    ]
    res = [{"domain": "tts.example.com", "url": "https://tts.example.com",
            "found_via": "queue:test"},
           {"domain": "newly.example.net", "url": "https://newly.example.net",
            "found_via": "graph"}]
    json.dump(cls, open(os.path.join(base, "classified.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(res, open(os.path.join(base, "resources.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return cls, res


def main():
    from fleet import catalog
    from fleet.kinds.paper_device import PaperDevice
    from fleet.kinds.domain_device import DomainDevice
    from fleet.kinds.doc_device import DocDevice

    tmp = tempfile.mkdtemp(prefix="t72_")
    pdir = os.path.join(tmp, "papers")
    ddir = os.path.join(tmp, "domains")
    try:
        recs = build_paper_lib(pdir)
        build_domain_lib(ddir)

        # ------------------------------------------------------------------
        sec("一、接上了：能力进总表 + 设备存在 + 能分派")
        reg = catalog.build(with_nodes=False)
        names = set(reg.cap_names())
        pcaps = {"paper.stats", "paper.search", "paper.get", "paper.recent",
                 "paper.tags", "paper.path", "paper.text"}
        dcaps = {"domain.stats", "domain.search", "domain.get", "domain.categories",
                 "domain.by_category", "domain.recent"}
        xcaps = {"doc.convert", "doc.read", "doc.formats"}
        for label, group in (("paper", pcaps), ("domain", dcaps), ("doc", xcaps)):
            missing = sorted(group - names)
            check(not missing, "%s 能力没进总表：%s" % (label, missing))
        for name in ("paper:lib", "domain:lib", "doc:convert"):
            check(reg.device(name) is not None, "设备没注册：%s" % name)
        for n in sorted(pcaps | dcaps | xcaps):
            c = reg.capability(n)
            check(c is not None and isinstance(c.params, tuple),
                  "%s 的参数表没归一成元组" % n)

        # ------------------------------------------------------------------
        sec("二、论文库：搜得准、读得到、按需解析不落盘")
        P = PaperDevice(root=pdir)
        check(P.probe()[0] is True, "论文库应恒可用")

        r = P.invoke("paper.stats")
        check(r.ok and r.data["papers"] == 3, "stats 应数出 3 篇（坏行跳过），实得 %r"
              % (r.data.get("papers") if r.ok else r.err))
        check(r.data.get("pdf_files") == 1, "stats 应数出 1 个 PDF")
        check(r.data.get("tags", {}).get("compression") == 1, "stats 方向标签不对")

        r = P.invoke("paper.search", {"q": "quantization"})
        check(r.ok and r.data["total"] == 1, "搜 quantization 应命中 1 篇，实得 %r"
              % (r.data.get("total") if r.ok else r.err))
        check(r.data["results"][0]["id"] == "2501.00001", "命中排序不对")
        check("pdf" in (r.data["results"][0] or {}), "搜索结果应带本地 pdf 路径")

        r = P.invoke("paper.search", {"tag": "agent"})
        check(r.ok and r.data["total"] == 1 and r.data["results"][0]["id"] == "2501.00002",
              "按 tag=agent 过滤应只出 2501.00002")

        r = P.invoke("paper.search", {"q": ""})
        check((not r.ok) and "q" in (r.err or ""), "空查询应结构化拒绝")

        r = P.invoke("paper.get", {"id": "2501.00002"})
        check(r.ok and r.data["title"].startswith("Tool Use"), "get 取详情失败")
        r = P.invoke("paper.get", {"id": "nope"})
        check((not r.ok) and r.err, "不存在的 id 应报错而不是空成功")

        r = P.invoke("paper.path", {"id": "2501.00001"})
        check(r.ok and r.data["exists"] is True, "path 应给出存在的 PDF")

        before = set(os.listdir(pdir))
        r = P.invoke("paper.text", {"id": "2501.00001"})
        check(r.ok and "HELLO SKYNET" in (r.data.get("text") or ""),
              "按需解析应抽出正文，实得 %r" % (r.err if not r.ok else r.data.get("text", "")[:60]))
        after = set(os.listdir(pdir))
        check(before == after, "按需解析不许在库目录落任何新文件：%s" % (after - before))

        r = P.invoke("paper.text", {"id": "2501.00002"})     # 没有 PDF 文件
        check((not r.ok) and "PDF" in (r.err or ""), "缺 PDF 应明确报错")

        # ------------------------------------------------------------------
        sec("三、域名库：标签边界不假命中、前缀可解析、类别可用")
        D = DomainDevice(data_dir=ddir)
        r = D.invoke("domain.stats")
        check(r.ok and r.data["domains"] == 4, "stats 应数出 4 条（3 分类 + 1 新发现）")
        check(0 < r.data["classified_pct"] <= 100, "已分类率不合理")

        r = D.invoke("domain.search", {"q": "tts"})
        doms = [x["domain"] for x in r.data.get("results", [])]
        check("tts.example.com" in doms, "tts 应命中 tts.example.com")
        check("aditya.bhatts.org" not in doms,
              "tts 不许命中 bhatts（子串假命中，已踩过）")

        r = D.invoke("domain.get", {"domain": "https://github.com/"})
        check(r.ok and r.data["category"].startswith("代码仓库"),
              "get 应容忍 https:// 前缀与尾斜杠")

        r = D.invoke("domain.by_category", {"category": "AI工具"})
        check(r.ok and r.data["total"] == 1, "按类别列域名失败")
        r = D.invoke("domain.categories")
        check(r.ok and sum(r.data["categories"].values()) == 4, "类别直方图总数不对")
        r = D.invoke("domain.search", {"q": ""})
        check(not r.ok, "空查询应拒绝")

        # ------------------------------------------------------------------
        sec("四、文档转换：靠开源 MarkItDown，各种格式一个入口，按需单文件")
        X = DocDevice()
        ok, why = X.probe()
        check(ok, "doc 设备应可用（装 markitdown）：%s" % why)
        r = X.invoke("doc.formats")
        check(r.ok and "MarkItDown" in r.data.get("engine", ""), "引擎应报 MarkItDown")
        check(len(r.data.get("exts") or []) >= 8, "支持的格式太少")

        pdf = os.path.join(pdir, "pdf", "2501.00001.pdf")
        before = set(os.listdir(os.path.dirname(pdf)))
        r = X.invoke("doc.convert", {"path": pdf})
        check(r.ok and "HELLO SKYNET" in r.data.get("text", ""),
              "doc.convert 应转出正文，实得 %r" % (r.err if not r.ok else ""))
        after = set(os.listdir(os.path.dirname(pdf)))
        check(before == after, "转换不许落中间文件")

        r = X.invoke("doc.read", {"path": pdf})
        check(r.ok and "HELLO SKYNET" in r.data.get("text", ""), "doc.read 纯文本失败")

        r = X.invoke("doc.convert", {"path": os.path.join(tmp, "nope.pdf")})
        check((not r.ok) and "不存在" in (r.err or ""), "不存在的文件应明确报错")
        bad = os.path.join(tmp, "x.exe")
        open(bad, "wb").write(b"MZ")
        r = X.invoke("doc.convert", {"path": bad})
        check((not r.ok) and "不支持" in (r.err or ""), "不支持的扩展名应拒绝")

        # ------------------------------------------------------------------
        sec("五、空库 / 缺库：结构化成空，不许抛异常")
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        PE = PaperDevice(root=empty)
        r = PE.invoke("paper.stats")
        check(r.ok and r.data["papers"] == 0, "空论文库应返回 0 篇")
        r = PE.invoke("paper.search", {"q": "anything"})
        check(r.ok and r.data["total"] == 0, "空库搜索应返回 0 结果而不是报错")
        DE = DomainDevice(data_dir=empty)
        r = DE.invoke("domain.stats")
        check(r.ok and r.data["domains"] == 0, "空域名库应返回 0")

        # ------------------------------------------------------------------
        sec("六、真库冒烟（只读，不写）")
        real = PaperDevice()
        r = real.invoke("paper.stats")
        print("  · 真实论文库：%s 篇 / %s 个 PDF（%s）"
              % (r.data.get("papers"), r.data.get("pdf_files"), real.root()))
        reald = DomainDevice()
        r2 = reald.invoke("domain.stats")
        print("  · 真实域名库：%s 条，已分类 %s%%"
              % (r2.data.get("domains"), r2.data.get("classified_pct")))

        # ------------------------------------------------------------------
        sec("七、内容生成补强：免 key 出图 + 图片看/缩（走开源 ffmpeg）")
        for n in ("image.free", "image.info", "image.resize"):
            check(n in names, "能力没进总表：%s" % n)
        check(reg.device("image:ops") is not None, "image:ops 设备没注册")

        from fleet import imgscale
        from fleet.kinds.ai import AI
        from fleet.kinds.image_ops import ImageOps

        png = os.path.join(tmp, "fixture.png")
        imgscale.write_png(png, 64, 32, 3, bytearray([200, 40, 40] * (64 * 32)))
        I = ImageOps()
        r = I.invoke("image.info", {"path": png})
        check(r.ok and (r.data["width"], r.data["height"]) == (64, 32),
              "image.info 宽高不对：%r" % (r.data if r.ok else r.err))
        out_png = os.path.join(tmp, "small.png")
        r = I.invoke("image.resize", {"path": png, "max_w": 32, "max_h": 32, "out": out_png})
        check(r.ok and os.path.exists(out_png), "image.resize 没产出文件：%r"
              % (r.err if not r.ok else ""))
        check(r.ok and max(r.data["width"], r.data["height"]) <= 32, "缩放后仍超上限")
        check(r.ok and r.data["width"] == 32, "等比缩放比例不对（64x32 → 32x16）")

        A = AI()
        r = A.invoke("image.free", {})
        check((not r.ok) and "prompt" in (r.err or ""), "空 prompt 应结构化拒绝")
        check(A.probe()[0] is True, "免 key 出图设备应恒可用（pollinations 无需 token）")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== t72: %d 过 / %d 败 ===" % (_OK, _FAIL))
    return _FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
