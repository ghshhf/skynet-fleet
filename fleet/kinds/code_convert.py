"""代码转化层（主人 11-02 定的方向）：任意语言参考码 → 目标语言（默认 Python）。

动机：手写重实现容易踩格式/编码坑——本次纯 Python SM3 就是系统性错误，逐轮差分定位不到，
最后靠标准库 hashlib 兜底。把「人工照抄」换成「LLM 机器转 + 语法/编译机器验」。

设计取舍：
- 复用已有 llmgw.chat（不引外部 daemon、不装 PoC 级 transpiler）。
- verify 只做「语法/编译」级校验，**不做语义证明**；哈希/加密常量靠 prompt 强制逐位等价。
- LLM 不可用时如实报错，不伪造转译结果。

诚实边界：
- 转译由 LLM 完成，不保证语义 100% 等价。
- verify 拦得住语法错，拦不住"悄悄改常数"。真要用在加密/哈希上，必须再过标准测试向量门禁。
"""
import ast
import os
import re
import subprocess
import sys
import tempfile

from ..core import BaseDevice, DeviceResult as R

PROMPT_TMPL = """你是严谨的源代码转译器。把下面这段 {src} 代码转成 {dst}。

硬性要求：
1. 只输出 {dst} 代码，不要解释、不要 markdown 围栏（```）。
2. 算法与常量必须逐位等价，尤其是哈希/加密/编码表/魔数，禁止"优化"或"近似"。
3. 若依赖 {src} 专有库，请在 {dst} 里用标准库或等价实现替代，并在顶部注释写明替代关系。
4. 不编造不存在的 API；拿不准的地方写注释 TODO，不要瞎写。

源语言: {src}
目标语言: {dst}
源文件名: {name}

源代码：
{code}
"""

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+\-]*\s*\n(.*?)\n```\s*$", re.S)


def _strip_fence(text):
    """去掉 LLM 爱加的 markdown 围栏；半截围栏也兜住。"""
    t = (text or "").strip()
    m = _FENCE.match(t)
    if m:
        return m.group(1).strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
    if t.endswith("```"):
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _verify_python(code):
    """语法校验：ast.parse + py_compile（子进程，注入 UTF-8 防 GBK 崩）。"""
    try:
        ast.parse(code)
    except SyntaxError as e:
        return False, "ast.parse 失败: %s (line %s)" % (e.msg, e.lineno)
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-m", "py_compile", path],
                           capture_output=True, text=True, timeout=30, env=env)
        if p.returncode != 0:
            return False, "py_compile 失败: %s" % ((p.stderr or p.stdout or "")[:400])
        return True, "python 语法/编译通过"
    except Exception as e:
        return False, "校验异常: %s" % e
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class CodeConvert(BaseDevice):
    """代码转译设备：LLM 机器转 + 语法/编译机器验。"""

    kind = "code"
    via = "internet"

    def __init__(self, name="code:converter", **kw):
        super().__init__(name, via="internet", **kw)
        self.meta.setdefault("note", "代码转译：任意语言参考码 → 目标语言（默认 Python），复用 llmgw.chat 转译 + 语法/编译校验")

    def probe(self):
        return True, ""

    def caps(self):
        return ("code.convert", "code.verify")

    # ---- 复用已有 LLM 网关（懒导入，避免循环依赖）----
    def _llm(self, prompt, model, cls, timeout, max_tokens):
        from .llmgw_device import LlmGwDevice
        args = {"messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(max_tokens or 8192),
                "timeout": int(timeout or 180)}
        if model:
            args["model"] = model
        elif cls:
            args["class"] = cls
        res = LlmGwDevice().invoke("llmgw.chat", args)
        if not getattr(res, "ok", False):
            return None, getattr(res, "err", "llm 调用失败")
        data = getattr(res, "data", {}) or {}
        body = data.get("result") or {}
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None, "llmgw 返回体缺少 choices[0].message.content"
        return content, None

    def _convert(self, args):
        code = args.get("code") or args.get("source")
        if not code:
            return {"ok": False, "err": "缺少 code"}
        src = str(args.get("src") or args.get("from") or "auto").strip()
        dst = str(args.get("dst") or args.get("to") or "python").strip().lower()
        name = args.get("name") or ("snippet." + ("py" if dst in ("python", "py") else dst))
        prompt = PROMPT_TMPL.format(src=src, dst=dst, name=name, code=code)
        content, err = self._llm(prompt, args.get("model"),
                                 args.get("class") or "coder",
                                 args.get("timeout"), args.get("max_tokens"))
        if err:
            return {"ok": False, "err": err}
        out = _strip_fence(content).replace("\r\n", "\n")
        if not out:
            return {"ok": False, "err": "LLM 返回空代码"}
        verified, vmsg = None, ""
        if dst in ("python", "py"):
            verified, vmsg = _verify_python(out)
        else:
            vmsg = "目标语言 %s 无本地校验器，未验证" % dst
        ok = verified is not False
        return {"ok": ok, "ok_verify": verified, "verify_msg": vmsg,
                "lang": dst, "code": out, "chars": len(out),
                "note": "LLM 转译，语义等价不保证；verify 仅语法/编译级"}

    def _verify(self, args):
        code = args.get("code") or args.get("source")
        if not code:
            return {"ok": False, "err": "缺少 code"}
        lang = str(args.get("lang") or "python").lower()
        if lang in ("python", "py"):
            okv, msg = _verify_python(code)
            return {"ok": okv, "ok_verify": okv, "verify_msg": msg, "lang": "python"} if okv \
                else {"ok": False, "err": msg, "lang": "python"}
        return {"ok": False, "err": "暂只支持 python 校验，收到 %s" % lang}

    def invoke(self, cap, args=None):
        args = args or {}
        try:
            if cap == "code.convert":
                m = self._convert(args)
            elif cap == "code.verify":
                m = self._verify(args)
            else:
                return R.unsupported(cap, "未知 code 子操作: %s" % cap,
                                     device=self.name, via=self.via)
        except Exception as e:
            return R.unavailable(cap, "代码转化异常: %s" % e, device=self.name, via=self.via)
        if not m.get("ok"):
            return R.unavailable(cap, m.get("err", "未知失败"), device=self.name, via=self.via)
        return R.success(cap, self.name, via=self.via, data=m)


# ============================ 自测（离线，只验本地逻辑） ============================
def _selftest():
    out = {}
    out["strip_fence"] = _strip_fence("```python\nx = 1\n```")
    out["strip_bare"] = _strip_fence("x = 1")
    good, _ = _verify_python("x = 1\n")
    bad, _ = _verify_python("def f(:\n  pass\n")
    out["verify_good"] = good
    out["verify_bad"] = bad
    out["ok"] = (out["strip_fence"] == "x = 1" and out["strip_bare"] == "x = 1"
                 and out["verify_good"] is True and out["verify_bad"] is False)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(_selftest(), ensure_ascii=False, indent=2))
