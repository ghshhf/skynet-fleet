"""本机端点自适应：给每个写死的地址/端口留一个环境变量覆盖入口。

约定（NAME 大写，如 DECIDER）：
  FLEET_<NAME>_URL    整条 URL 优先
  FLEET_<NAME>_HOST   只改主机
  FLEET_<NAME>_PORT   只改端口

都不设时用默认值——默认值是方便，不是硬约束。这样换机器、换端口、
端口被占用时不必改代码。协议标准端口（ADB 5555 / NAT-PMP 5351 / WoL 9）
属于协议规定值，不走这一层。
"""
import os


def url(name, default):
    """整条 URL 覆盖。"""
    return os.environ.get("FLEET_" + name.upper() + "_URL") or default


def port_num(name, default):
    """只要一个整数端口。"""
    v = os.environ.get("FLEET_" + name.upper() + "_PORT")
    if not v:
        return int(default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def local(name, default_port, path="", scheme="http", host="127.0.0.1"):
    """拼一条本机服务 URL：URL 覆盖 > HOST/PORT 覆盖 > 默认。"""
    full = os.environ.get("FLEET_" + name.upper() + "_URL")
    if full:
        return full.rstrip("/") + path
    h = os.environ.get("FLEET_" + name.upper() + "_HOST") or host
    p = port_num(name, default_port)
    return "%s://%s:%d%s" % (scheme, h, p, path)


def describe():
    """体检用：列出当前生效的覆盖项，便于确认「到底改没改到」。"""
    out = {}
    for k, v in os.environ.items():
        if k.startswith("FLEET_") and (k.endswith("_URL") or
                                       k.endswith("_PORT") or
                                       k.endswith("_HOST")):
            out[k] = v
    return out


def _selftest():
    """离线自测：只动临时环境变量，结束恢复，不碰系统设置。"""
    fails, n = [], 0

    def ck(cond, label):
        nonlocal n
        n += 1
        if not cond:
            fails.append(label)

    keys = [k for k in os.environ if k.startswith("FLEET_ST_")]
    saved = {k: os.environ[k] for k in keys}
    try:
        for k in keys:
            del os.environ[k]
        ck(url("ST_A", "http://127.0.0.1:1") == "http://127.0.0.1:1", "url 默认值")
        ck(port_num("ST_A", 8790) == 8790, "port_num 默认值")
        ck(local("ST_A", 8790, "/v1") == "http://127.0.0.1:8790/v1", "local 默认值")

        os.environ["FLEET_ST_A_URL"] = "http://10.0.0.9:7001"
        ck(url("ST_A", "http://127.0.0.1:1") == "http://10.0.0.9:7001", "URL 覆盖")
        ck(local("ST_A", 8790, "/v1") == "http://10.0.0.9:7001/v1", "local 优先整条 URL")
        del os.environ["FLEET_ST_A_URL"]

        os.environ["FLEET_ST_A_PORT"] = "9998"
        ck(port_num("ST_A", 8790) == 9998, "PORT 覆盖")
        ck(local("ST_A", 8790) == "http://127.0.0.1:9998", "local 用覆盖端口")
        os.environ["FLEET_ST_A_PORT"] = "not-a-number"
        ck(port_num("ST_A", 8790) == 8790, "非法端口回退默认值")
        del os.environ["FLEET_ST_A_PORT"]

        os.environ["FLEET_ST_A_HOST"] = "10.0.0.5"
        ck(local("ST_A", 8790, "/x") == "http://10.0.0.5:8790/x", "HOST 覆盖")
        ck(local("ST_A", 8790, "/x", scheme="https") == "https://10.0.0.5:8790/x",
           "scheme 可换")
        del os.environ["FLEET_ST_A_HOST"]

        os.environ["FLEET_ST_B_PORT"] = "1234"
        ck("FLEET_ST_B_PORT" in describe(), "describe 列出覆盖项")
        ck("PATH" not in describe(), "describe 不列无关环境变量")
        del os.environ["FLEET_ST_B_PORT"]
    finally:
        for k in [k for k in os.environ if k.startswith("FLEET_ST_")]:
            del os.environ[k]
        os.environ.update(saved)

    print("endpoints 自测 %d/%d 通过" % (n - len(fails), n))
    for f in fails:
        print("  FAIL", f)
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
