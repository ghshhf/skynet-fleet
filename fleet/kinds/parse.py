"""解析设备：下载层前置。把「分享链接→直链」整个领域吸收进天网源码（不封装外部 daemon、不调黑盒 exe）。

吸收来源（源码已拉至 /d/refs，吸收而非 vendored）：
- DLWangSan/douyin_parse：抖音解析核心逻辑 + xbogus.py(纯 Python MD5+RC4) + abogus.py(a_bogus，原依赖 gmssl.sm3 → 本文件用纯 Python SM3 替代，零依赖)
- Evil0ctal/Douyin_TikTok_Download_API：x-secsdk-web-signature(websign.py, MD5+uifid) + 多平台端点(douyin/tiktok/bilibili)
- 无水印原理：抖音 play_addr.url_list 中 playwm 替换为 play 即得无水印直链（公开 trick，无需破解）

设计原则（对齐 OSINT / 下载「提取万物融入己身」）：
- 解析层零依赖（urllib + 标准库），签名算法全用纯 Python 重写进源码
- 平台识别（口令/短链→URL）+ 抖音解析（签名请求→无水印直链）+ 对接下载引擎
- 抖音需登录态 Cookie（提供 uifid 等）拿满血无水印；无 Cookie 走降级（可能因风控失败）
- 多平台端点登记（douyin 完整样本，tiktok/bilibili 待深化），待逐一读源码内化，不编造 API
- AI 读本文件即可知道如何调用（capabilities 经 catalog 暴露）

诚实边界：真实抖音/海外平台解析需联网 + 有效登录态；本模块对签名/识别/对接做单元锁死，
          真实 API 联调需主人提供 Cookie 后实跑，门禁不强制联网。
"""

import os
import re
import time
import json
import base64
import struct
import random
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import socketserver
import http.server
import threading

from ..core import BaseDevice, DeviceResult as R

quote = urllib.parse.quote


# ============================ 纯 Python 国密 SM3（替代 gmssl，零依赖） ============================
def _sm3(data) -> str:
    """国密 SM3 哈希，返回 64 位 hex。

    优先用 Python 标准库 hashlib（随解释器提供、OpenSSL 验证，零 pip 依赖，
    与 download.py 用 hashlib.sha256 同一套路）；若运行环境 hashlib 未编译 sm3，
    回退到下方纯 Python 实现（SM3 标准算法）。
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        return hashlib.new("sm3", data).hexdigest()
    except (ValueError, AttributeError):
        return _sm3_pure(data)


def _sm3_pure(data) -> str:
    """纯 Python 国密 SM3 兜底实现（hashlib 不含 sm3 时启用）。"""
    def _rotl(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    def _ff(x, y, z, j):
        return (x ^ y ^ z) if j < 16 else ((x & y) | (x & z) | (y & z))

    def _gg(x, y, z, j):
        return (x ^ y ^ z) if j < 16 else ((x & y) | ((~x & 0xFFFFFFFF) & z))

    def _p0(x):
        return x ^ _rotl(x, 9) ^ _rotl(x, 17)

    def _p1(x):
        return x ^ _rotl(x, 15) ^ _rotl(x, 23)

    IV = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
          0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]
    T = [0x79CC4519] * 16 + [0x7A879D8A] * 48

    if isinstance(data, str):
        data = data.encode("utf-8")
    msg = bytearray(data)
    l = len(msg) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += l.to_bytes(8, "big")

    V = IV[:]
    for i in range(0, len(msg), 64):
        block = msg[i:i + 64]
        W = list(struct.unpack(">16I", block))
        for j in range(16, 68):
            W.append((_p1(W[j - 16] ^ W[j - 9] ^ _rotl(W[j - 3], 15)) ^ _rotl(W[j - 13], 7) ^ W[j - 6]) & 0xFFFFFFFF)
        W1 = [(W[j] ^ W[j + 4]) & 0xFFFFFFFF for j in range(64)]
        A, B, C, D, E, F, G, H = V
        for j in range(64):
            SS1 = _rotl((_rotl(A, 12) + E + _rotl(T[j], j % 32)) & 0xFFFFFFFF, 7)
            SS2 = SS1 ^ _rotl(A, 12)
            TT1 = (_ff(A, B, C, j) + D + SS2 + W1[j]) & 0xFFFFFFFF
            TT2 = (_gg(E, F, G, j) + H + SS1 + W[j]) & 0xFFFFFFFF
            A, B, C, D, E, F, G, H = TT1, A, _rotl(B, 9), C, _p0(TT2 ^ _rotl(C, 9)), E, _rotl(F, 19), G
        V = [(x ^ y) & 0xFFFFFFFF for x, y in zip(V, [A, B, C, D, E, F, G, H])]
    return "".join("%08x" % x for x in V)


# ============================ X-Bogus（吸收 xbogus.py，纯 Python MD5+RC4） ============================
class XBogus:
    """抖音 X-Bogus 签名（MD5+RC4+字符映射），零依赖。"""

    def __init__(self, user_agent=None):
        self._char_map = [None] * 128
        for i in range(10):
            self._char_map[48 + i] = i
        for i in range(6):
            self._char_map[97 + i] = 10 + i
        self._charset = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
        self._ua_key = b"\x00\x01\x0c"
        self.user_agent = (user_agent if user_agent else
                           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0")

    @staticmethod
    def _md5(data):
        if isinstance(data, str):
            arr = [ord(c) for c in data]
        elif isinstance(data, (list, tuple)):
            arr = list(data)
        else:
            raise ValueError("Invalid input type")
        return hashlib.md5(bytes(arr)).hexdigest()

    def _md5_str_to_array(self, md5_str):
        if len(md5_str) > 32:
            return [ord(c) for c in md5_str]
        arr = []
        for i in range(0, len(md5_str), 2):
            high = self._char_map[ord(md5_str[i])]
            low = self._char_map[ord(md5_str[i + 1])]
            arr.append((high << 4) | low)
        return arr

    def _md5_encrypt(self, url_path):
        return self._md5_str_to_array(self._md5(self._md5_str_to_array(self._md5(url_path))))

    @staticmethod
    def _encoding_conversion(a, b, c, e, d, t, f, r, n, o, i, _, x, u, s, l, v, h, p):
        y = [a, int(i)]
        y.extend([b, _, c, x, e, u, d, s, t, l, f, v, r, h, n, p, o])
        return bytes(y).decode("ISO-8859-1")

    @staticmethod
    def _encoding_conversion2(a, b, c):
        return chr(a) + chr(b) + c

    @staticmethod
    def _rc4_encrypt(key, data):
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) % 256
            S[i], S[j] = S[j], S[i]
        i = j = 0
        out = bytearray()
        for byte in data:
            i = (i + 1) % 256
            j = (j + S[i]) % 256
            S[i], S[j] = S[j], S[i]
            out.append(byte ^ S[(S[i] + S[j]) % 256])
        return out

    def _calc(self, a1, a2, a3):
        x = ((a1 & 255) << 16) | ((a2 & 255) << 8) | a3
        return (self._charset[(x >> 18) & 63] + self._charset[(x >> 12) & 63] +
                self._charset[(x >> 6) & 63] + self._charset[x & 63])

    def get_xbogus(self, url_path):
        rc4 = self._rc4_encrypt(self._ua_key, self.user_agent.encode("ISO-8859-1"))
        a1 = self._md5_str_to_array(self._md5(base64.b64encode(rc4).decode("ISO-8859-1")))
        a2 = self._md5_str_to_array(self._md5(self._md5_str_to_array("d41d8cd98f00b204e9800998ecf8427e")))
        url_arr = self._md5_encrypt(url_path)
        timer = int(time.time())
        ct = 536919696
        new = [64, 0.00390625, 1, 12, url_arr[14], url_arr[15], a2[14], a2[15],
               a1[14], a1[15], (timer >> 24) & 255, (timer >> 16) & 255, (timer >> 8) & 255, timer & 255,
               (ct >> 24) & 255, (ct >> 16) & 255, (ct >> 8) & 255, ct & 255]
        xr = new[0]
        for i in range(1, len(new)):
            b = int(new[i]) if isinstance(new[i], float) else new[i]
            xr ^= b
        new.append(xr)
        a3 = new[0::2]
        a4 = new[1::2]
        merge = a3 + a4
        garbled = self._encoding_conversion2(2, 255, self._rc4_encrypt(
            b"\xff", self._encoding_conversion(*merge).encode("ISO-8859-1")).decode("ISO-8859-1"))
        xb = ""
        for i in range(0, len(garbled), 3):
            xb += self._calc(ord(garbled[i]), ord(garbled[i + 1]), ord(garbled[i + 2]))
        return "%s&X-Bogus=%s" % (url_path, xb), xb, self.user_agent


# ============================ A-Bogus（吸收 abogus.py，用 _sm3 替代 gmssl） ============================
class ABogus:
    """抖音 A-Bogus 签名（SM3+RC4+字符映射），用本文件 _sm3 替代 gmssl，零依赖。"""

    _arguments = [0, 1, 14]
    _ua_key = "\x00\x01\x0e"
    _end_string = "cus"
    _version = [1, 0, 1, 5]
    _browser = "1536|742|1536|864|0|0|0|0|1536|864|1536|864|1536|742|24|24|MacIntel"
    _charsets = {
        "s0": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
        "s1": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s2": "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s3": "ckdp1h4ZKsUB80/MfvV36XIgR25+WQAlEi7NLboqYTOPuzKFjJnry79HbGcaStCe",
        "s4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
    }

    def __init__(self, platform=None):
        self.chunk = []
        self.size = 0
        self.reg = [1937774191, 1226093241, 388252375, 3666478592,
                    2842636476, 372324522, 3817729613, 2969243214]
        self.ua_code = [76, 98, 15, 131, 97, 245, 224, 133, 122, 199, 241, 166, 79, 34, 90,
                        191, 128, 126, 122, 98, 66, 11, 14, 40, 49, 110, 110, 173, 67, 96, 138, 252]
        self.browser = self._generate_browser_info(platform) if platform else self._browser
        self.browser_len = len(self.browser)
        self.browser_code = [ord(c) for c in self.browser]

    @classmethod
    def _random_list(cls, a=None, b=170, c=85, d=0, e=0, f=0, g=0):
        r = a if a is not None else (random.random() * 10000)
        v = [r, int(r) & 255, int(r) >> 8]
        return [v[1] & b | d, v[1] & c | e, v[2] & b | f, v[2] & c | g]

    @classmethod
    def _list_1(cls, r1=None, a=170, b=85, c=45):
        return cls._random_list(r1, a, b, 1, 2, 5, c & a)

    @classmethod
    def _list_2(cls, r2=None, a=170, b=85):
        return cls._random_list(r2, a, b, 1, 0, 0, 0)

    @classmethod
    def _list_3(cls, r3=None, a=170, b=85):
        return cls._random_list(r3, a, b, 1, 0, 5, 0)

    @staticmethod
    def _from_char_code(*args):
        return "".join(chr(c) for c in args)

    def _generate_string_1(self, r1=None, r2=None, r3=None):
        return (self._from_char_code(*self._list_1(r1)) + self._from_char_code(*self._list_2(r2)) +
                self._from_char_code(*self._list_3(r3)))

    @staticmethod
    def _list_4(a, b, c, d, e, f, g, h, i, j, k, m, n, o, p, q, r):
        return [44, a, 0, 0, 0, 0, 24, b, n, 0, c, d, 0, 0, 0, 1, 0, 239,
                e, o, f, g, 0, 0, 0, 0, h, 0, 0, 14, i, j, 0, k, m, 3, p, 1, q, 1, r, 0, 0, 0]

    @staticmethod
    def _rc4_encrypt(plaintext, key):
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + ord(key[i % len(key)])) % 256
            s[i], s[j] = s[j], s[i]
        i = j = 0
        out = []
        for ch in plaintext:
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
            out.append(chr(s[(s[i] + s[j]) % 256] ^ ord(ch)))
        return "".join(out)

    def _list_4_list(self, url_params, method="GET", start_time=0, end_time=0):
        start_time = start_time or int(time.time() * 1000)
        end_time = end_time or (start_time + random.randint(4, 8))
        params_arr = self._generate_params_code(url_params)
        method_arr = self._generate_method_code(method)
        return self._list_4(
            (end_time >> 24) & 255, params_arr[21], self.ua_code[23], (end_time >> 16) & 255,
            params_arr[22], self.ua_code[24], (end_time >> 8) & 255, (end_time >> 0) & 255,
            (start_time >> 24) & 255, (start_time >> 16) & 255, (start_time >> 8) & 255,
            (start_time >> 0) & 255, method_arr[21], method_arr[22],
            (end_time // 256 // 256 // 256 // 256) & 0xFF, (start_time // 256 // 256 // 256 // 256) & 0xFF,
            self.browser_len)

    @staticmethod
    def _end_check_num(arr):
        r = 0
        for i in arr:
            r ^= i
        return r

    def _generate_string_2(self, url_params, method="GET", start_time=0, end_time=0):
        a = self._list_4_list(url_params, method, start_time, end_time)
        e = self._end_check_num(a)
        a.extend(self.browser_code)
        a.append(e)
        return self._rc4_encrypt(self._from_char_code(*a), "y")

    @classmethod
    def _sm3_to_array(cls, data):
        if isinstance(data, str):
            b = data.encode("utf-8")
        else:
            b = bytes(data)
        h = _sm3(b)
        return [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]

    def _generate_method_code(self, method):
        return self._sm3_to_array(self._sm3_to_array(method + self._end_string))

    def _generate_params_code(self, params):
        return self._sm3_to_array(self._sm3_to_array(params + self._end_string))

    @classmethod
    def _generate_result(cls, s, charset="s4"):
        cs = cls._charsets[charset]
        out = []
        for i in range(0, len(s), 3):
            rem = len(s) - i
            if rem >= 3:
                n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8) | ord(s[i + 2])
                out += [cs[(n >> 18) & 63], cs[(n >> 12) & 63], cs[(n >> 6) & 63], cs[n & 63]]
            elif rem == 2:
                n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8)
                out += [cs[(n >> 18) & 63], cs[(n >> 12) & 63], cs[(n >> 6) & 63]]
            else:
                n = ord(s[i]) << 16
                out += [cs[(n >> 18) & 63], cs[(n >> 12) & 63]]
        pad = (4 - len(out) % 4) % 4
        out.append("=" * pad)
        return "".join(out)

    def _generate_browser_info(self, platform):
        iw, ih = random.randint(1280, 1920), random.randint(720, 1080)
        ow, oh = random.randint(iw, 1920), random.randint(ih, 1080)
        return "|".join(str(v) for v in [iw, ih, ow, oh, 0, random.choice((0, 30)), 0, 0,
                                         ow, oh, ow, oh, iw, ih, 24, 24, platform])

    def get_value(self, url_params, method="GET", start_time=0, end_time=0,
                  random_num_1=None, random_num_2=None, random_num_3=None):
        if isinstance(url_params, dict):
            url_params = urllib.parse.urlencode(url_params)
        s1 = self._generate_string_1(random_num_1, random_num_2, random_num_3)
        s2 = self._generate_string_2(url_params, method, start_time, end_time)
        return self._generate_result(s1 + s2, "s4")


# ============================ x-secsdk-web-signature（吸收 websign.py，MD5+uifid） ============================
class WebSign:
    """抖音 API 防护签名 x-secsdk-web-signature：md5(uifid_timestamp_SALT_query)。纯 Python。"""

    SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
    UIFID_COOKIE_NAMES = ("uifid", "uifid_temp", "uifidtemp", "UIFID", "UIFID_TEMP", "UIFIDTEMP")
    VERIFY_FP_COOKIE = "s_v_web_id"
    SIGNATURE_PARAM = "x-secsdk-web-signature"
    UIFID_PARAM = "uifid"
    TIMESTAMP_PARAM = "timestamp"
    EXPIRE_HEADER = "x-secsdk-web-expire"

    @staticmethod
    def pick_uifid(cookies):
        if not cookies:
            return None
        for n in WebSign.UIFID_COOKIE_NAMES:
            v = cookies.get(n)
            if v:
                return v
        return None

    @staticmethod
    def _encode(pairs):
        return "&".join("%s=%s" % (quote(k, safe="*-._"), quote(v, safe="*-._")) for k, v in pairs)

    @classmethod
    def sign(cls, pairs, uifid, timestamp=None):
        stamp = str(int(time.time() if timestamp is None else timestamp))
        covered = list(pairs)
        if not any(name == cls.UIFID_PARAM for name, _ in covered):
            covered.append((cls.UIFID_PARAM, uifid))
        covered.append((cls.TIMESTAMP_PARAM, stamp))
        query = cls._encode(covered)
        sig = hashlib.md5(("%s_%s_%s_%s" % (uifid, stamp, cls.SALT, query)).encode()).hexdigest()
        return (query + "&" + cls.SIGNATURE_PARAM + "=" + sig, sig,
                {cls.UIFID_PARAM: uifid, cls.SIGNATURE_PARAM: sig, cls.EXPIRE_HEADER: stamp})


# ============================ 平台识别 + 抖音解析 ============================
PLATFORM_REGISTRY = {
    "douyin": {
        "detail_api": "https://www.douyin.com/aweme/v1/web/aweme/detail/",
        "sign": ["a_bogus", "x_bogus", "web_sign"],
        "nwm_trick": "playwm->play",
        "status": "ready",
    },
    "tiktok": {
        "detail_api": "https://www.tiktok.com/api/v1/item/detail/",
        "sign": ["web_sign"],
        "status": "pending",  # 端点已登记，待读源码内化
    },
    "bilibili": {
        "status": "pending",  # 待读源码内化
    },
    "direct": {
        "status": "passthrough",  # 已经是直链，无需解析
    },
}


def detect_platform(share_text):
    s = (share_text or "").lower()
    if "v.douyin.com" in s or "douyin.com" in s or "iesdouyin" in s:
        return "douyin"
    if "tiktok.com" in s:
        return "tiktok"
    if "bilibili.com" in s or "b23.tv" in s:
        return "bilibili"
    if re.match(r"^https?://", s.strip()):
        return "direct"
    return "unknown"


def extract_url(share_text):
    """从分享口令/短链/文本中抽取真实 URL。"""
    # 1. 标准 http(s) URL
    m = re.search(r"https?://[^\s，。、\n]+", share_text or "")
    if m:
        return m.group(0).rstrip("，。、")
    # 2. 裸短链 v.douyin.com/xxxx
    m = re.search(r"v\.douyin\.com/[^\s，。、\n]+", share_text or "")
    if m:
        return "https://" + m.group(0).rstrip("，。、")
    return None


def follow_redirect(short_url, timeout=15, ua=None):
    """跟 302 跳转拿真实视频页 URL（返回最终 URL 或 None）。"""
    ua = ua or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    req = urllib.request.Request(short_url, headers={"User-Agent": ua, "Referer": "https://www.douyin.com/"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            return e.headers.get("Location")
        return None
    except Exception:
        return None


class DouyinResolver:
    """抖音免登录解析（吸收 jiuhunwl/short_videos + zouzanyan/douyin_crawl）。

    主路径：api.amemv.com/aweme/v1/aweme/detail/ —— 免 Cookie、免签名
    （a_bogus / x_bogus / websign 一律不需要），仅带 aweme_id + aid + device_platform + version
    即可拿 aweme_detail JSON。兜底：分享页 iesdouyin.com/share/video/{id}/ 抠 window._ROUTER_DATA。

    签名器（XBogus / ABogus / WebSign）保留给 TikTok / 强制签名场景复用，抖音主路径不调用。
    """
    # 免 Cookie 详情接口（公开作品无需登录即可读；吸收 jiuhunwl/short_videos DouyinParser.php）
    DETAIL_ENDPOINT = "https://api.amemv.com/aweme/v1/aweme/detail/"
    SHARE_PAGE = "https://www.iesdouyin.com/share/video/{aweme_id}/"
    DETAIL_PARAMS = {
        "aid": "6383",
        "device_platform": "web",
        "version_code": "190500",
        "version_name": "19.5.0",
    }
    MOBILE_UA = ("Mozilla/5.0 (Linux; Android 13; 2210132C Build/TKQ1.220829.002) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36")
    DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

    def __init__(self, cookie="", ua=None):
        self.ua = ua or self.MOBILE_UA
        self.cookie = (cookie or "").strip()
        # 签名器保留给 TikTok / 强制签名场景；抖音主路径不依赖 cookie 与签名
        self.xbogus = XBogus(self.ua)
        self.abogus = ABogus()
        self.websign = WebSign()
        self._cookie_map = self._parse_cookie(self.cookie)

    @staticmethod
    def _parse_cookie(cookie_str):
        if not cookie_str:
            return {}
        out = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    # ---- 从分享文本/短链抽 video_id ----
    def get_video_id(self, share_url):
        patterns = [
            r"/video/(\d+)", r"/aweme/detail/(\d+)", r"/note/(\d+)",
            r"video_id=(\d+)", r"aweme_id=(\d+)", r"modal_id=(\d+)",
            r"note_id=(\d+)", r"/(\d{15,})",
        ]
        # 先试文本里直接带的 URL
        url = extract_url(share_url)
        if not url:
            url = share_url.strip()
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        # 短链：跟跳拿真实页
        real = follow_redirect(url, ua=self.ua) if url.startswith("http") else None
        if real:
            for pat in patterns:
                m = re.search(pat, real)
                if m:
                    return m.group(1)
            # 从 HTML 抽 aweme_id
            try:
                req = urllib.request.Request(real, headers={"User-Agent": self.ua})
                html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
                for pat in [r'"aweme_id"\s*:\s*"?(\d{15,})', r'"itemId"\s*:\s*"?(\d{15,})',
                            r'"awemeId"\s*:\s*"?(\d{15,})', r'aweme_id=(\d{15,})']:
                    m = re.search(pat, html)
                    if m and m.group(1).isdigit() and len(m.group(1)) >= 15:
                        return m.group(1)
            except Exception:
                pass
        return None

    @staticmethod
    def _nwm(url):
        """无水印 trick：playwm → play。"""
        if not url:
            return None
        return url.replace("playwm", "play")

    @staticmethod
    def extract_qualities(data):
        """从 aweme_detail 抽多清晰度无水印直链（吸收 DLWangSan；snake/camel 双风格兼容）。"""
        aweme = (data.get("aweme_detail") or {}) if isinstance(data, dict) else {}
        video = aweme.get("video") or aweme.get("video") or {}
        # 兼容 _ROUTER_DATA 的 camelCase（playAddr / bitRateList）
        play_addr = video.get("play_addr") or video.get("playAddr") or {}
        bit_rate = video.get("bit_rate") or video.get("bitRateList") or []
        uri = (play_addr.get("uri") or video.get("uri") or
               (bit_rate[0].get("play_addr", {}).get("uri") if bit_rate and isinstance(bit_rate[0], dict) else None))
        url_list = play_addr.get("url_list") or []
        qualities = []
        if bit_rate:
            for br in bit_rate:
                if not isinstance(br, dict):
                    continue
                pa = ((br.get("play_addr") or br.get("playAddr")) or {}).get("url_list") or []
                gear = br.get("gear_name", "") or br.get("gearName", "")
                ratio = ""
                qt = br.get("quality_type") or br.get("qualityType")
                if isinstance(qt, dict):
                    ratio = qt.get("name", "")
                elif isinstance(qt, (int, str)):
                    mm = re.search(r"(\d+p)", str(qt).lower())
                    if mm:
                        ratio = mm.group(1)
                if not ratio and gear:
                    mm = re.search(r"(\d+p)", gear.lower())
                    if mm:
                        ratio = mm.group(1)
                if not ratio:
                    brv = br.get("bit_rate", 0) or br.get("bitRate", 0) or 0
                    ratio = ("1080p" if brv >= 2000000 else "720p" if brv >= 1000000
                             else "540p" if brv >= 500000 else "480p")
                for u in pa:
                    qualities.append({"url": DouyinResolver._nwm(u), "ratio": ratio,
                                      "bit_rate": br.get("bit_rate", 0) or br.get("bitRate", 0), "gear_name": gear})
        if not qualities and url_list:
            for u in url_list:
                mm = re.search(r"ratio=(\d+p)", u.lower())
                qualities.append({"url": DouyinResolver._nwm(u), "ratio": mm.group(1) if mm else "1080p",
                                  "bit_rate": 0, "gear_name": ""})
        if not qualities and uri:
            for ratio in ["1080p", "720p", "540p", "480p", "360p"]:
                qualities.append({"url": "https://aweme.snssdk.com/aweme/v1/play/?video_id=%s&ratio=%s&line=0" % (uri, ratio),
                                  "ratio": ratio, "bit_rate": 0, "gear_name": ""})
        seen = set()
        uniq = []
        for q in qualities:
            if q["url"] not in seen:
                seen.add(q["url"])
                uniq.append(q)
        return uniq

    @staticmethod
    def extract_meta(data):
        aweme = (data.get("aweme_detail") or {}) if isinstance(data, dict) else {}
        author = aweme.get("author") or {}
        cover = (aweme.get("video") or {}).get("cover") or {}
        cl = cover.get("url_list") or []
        return {
            "aweme_id": aweme.get("aweme_id"),
            "desc": aweme.get("desc"),
            "create_time": aweme.get("create_time"),
            "author_nickname": author.get("nickname"),
            "author_sec_uid": author.get("sec_uid"),
            "cover_url": cl[0] if cl else None,
            "content_type": "image" if aweme.get("aweme_type") in (2, 68) or aweme.get("images") else "video",
        }

    # ---- 免登录详情接口 URL（锁死：无 a_bogus / 无 x_bogus / 无 cookie）----
    def _detail_url(self, vid):
        params = dict(self.DETAIL_PARAMS, aweme_id=vid)
        return self.DETAIL_ENDPOINT + "?" + urllib.parse.urlencode(params)

    def _fetch_detail(self, vid):
        """主路径：免 Cookie 详情接口，直接拿 aweme_detail JSON。"""
        req = urllib.request.Request(self._detail_url(vid), headers={
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
        })
        try:
            d = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
            j = json.loads(d)
        except Exception:
            return None
        if not isinstance(j, dict) or j.get("status_code") != 0:
            return None
        detail = j.get("aweme_detail")
        if not isinstance(detail, dict) or not detail:
            return None
        return j

    # ---- 末位兜底：分享页抠 window._ROUTER_DATA（zouzanyan/douyin_crawl 路线）----
    # 诚实标注：2026-11-02 实测 iesdouyin 分享页常只返「空壳」（_ROUTER_DATA 里只有
    # ua/webId/query 等元信息、无 play_addr），连试 3 次仍空壳 ⇒ 命中率低，仅作最后兜底。
    def _fetch_share_page(self, vid):
        url = self.SHARE_PAGE.format(aweme_id=vid)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": self.ua,
                "Referer": "https://www.iesdouyin.com/",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        except Exception:
            return None
        data = self._extract_router_data(html)
        if not data:
            return None
        node = self._find_item_node(data)
        if not node:
            return None
        # 归一成「aweme_detail」外形，交给 extract_qualities / extract_meta 复用
        return {"aweme_detail": node}

    @staticmethod
    def _extract_router_data(html):
        """括号深度匹配提取 window._ROUTER_DATA 的 JSON。"""
        marker = "window._ROUTER_DATA = "
        idx = html.find(marker)
        if idx < 0:
            idx = html.find("_ROUTER_DATA")
            if idx < 0:
                return None
            eq = html.find("=", idx)
            if eq < 0:
                return None
            start = eq + 1
        else:
            start = idx + len(marker)
        while start < len(html) and html[start].isspace():
            start += 1
        if start >= len(html) or html[start] != "{":
            return None
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(html)):
            c = html[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:i + 1])
                    except (ValueError, json.JSONDecodeError):
                        return None
        return None

    @staticmethod
    def _find_item_node(data):
        """在 _ROUTER_DATA 树里找含 desc + (video 或 images) 的节点 ≈ aweme_detail。"""
        if isinstance(data, list):
            for v in data:
                r = DouyinResolver._find_item_node(v)
                if r:
                    return r
            return None
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("desc"), str) and (
                isinstance(data.get("video"), dict) or isinstance(data.get("images"), list)):
            return data
        for v in data.values():
            r = DouyinResolver._find_item_node(v)
            if r:
                return r
        return None

    def resolve(self, share_url, retries=2):
        """解析：免登录详情接口优先（带重试），分享页末位兜底。全程不需要 cookie / 签名。"""
        vid = self.get_video_id(share_url)
        if not vid:
            return {"ok": False, "err": "无法提取视频 ID（口令/短链/链接均失败）"}
        data = None
        for _ in range(max(1, retries)):
            data = self._fetch_detail(vid)
            if data:
                break
        if not data:
            data = self._fetch_share_page(vid)
        if not data or not data.get("aweme_detail"):
            return {"ok": False, "login_required": False,
                    "err": "免登录解析失败（接口限流或作品已删除/私密）", "video_id": vid}
        meta = self.extract_meta(data)
        qualities = self.extract_qualities(data)
        nwm = qualities[0]["url"] if qualities else None
        return {"ok": True, "video_id": vid, "login_required": False,
                "nwm_url": nwm, "qualities": qualities, "meta": meta}


# ============================ 解析设备（对齐 Download 骨架） ============================
class Parse(BaseDevice):
    kind = "parse"
    via = "internet"

    def __init__(self, name="parse:resolver", **kw):
        super().__init__(name, via="internet", **kw)
        self.meta.setdefault("note", "解析层全内化：分享链接→直链；抖音免登录（api.amemv.com 详情接口，零 cookie/零签名），分享页兜底")

    def probe(self):
        return True, ""

    def caps(self):
        return ("parse.douyin", "parse.resolve", "parse.tiktok")

    def _douyin(self, args):
        share = args.get("share") or args.get("url") or args.get("share_url")
        if not share:
            return {"ok": False, "err": "缺少 share"}
        # 免登录：不读 cookie（cookie 仅保留可选，传给签名器备用，非必需）
        cookie = args.get("cookie") or os.environ.get("DOUYIN_COOKIE", "")
        return DouyinResolver(cookie=cookie).resolve(share)

    def _resolve(self, args):
        share = args.get("share") or args.get("url")
        if not share:
            return {"ok": False, "err": "缺少 share"}
        plat = args.get("platform") or detect_platform(share)
        if plat == "direct":
            r = {"ok": True, "platform": "direct", "nwm_url": extract_url(share) or share, "qualities": []}
        elif plat == "douyin":
            r = self._douyin(args)
            if r.get("ok"):
                r["platform"] = "douyin"
        elif plat == "tiktok":
            return {"ok": False, "err": "tiktok 解析待内化（端点已登记）"}
        else:
            return {"ok": False, "err": "未支持平台: %s" % plat}
        # 可选：解析后直接交给下载引擎
        if args.get("download") and r.get("ok") and r.get("nwm_url"):
            from .download import DownloadEngine
            dest = args.get("dest", "downloads/out.bin")
            d0 = os.path.dirname(dest)
            if d0:
                os.makedirs(d0, exist_ok=True)
            eng = DownloadEngine(concurrency=int(args.get("concurrency", 4)),
                                 proxy=args.get("proxy"))
            dl = eng.download([r["nwm_url"]], dest, resume=args.get("resume", True))
            r["download"] = dl
        return r

    def invoke(self, cap, args=None):
        args = args or {}
        op = cap.split(".", 1)[1]
        try:
            if op == "douyin":
                m = self._douyin(args)
            elif op == "resolve":
                m = self._resolve(args)
            elif op == "tiktok":
                m = {"ok": False, "err": "tiktok 解析待内化（端点已登记）"}
            else:
                return R.unavailable(cap, "未知解析子操作: %s" % op, device=self.name, via=self.via)
        except Exception as e:
            return R.unavailable(cap, "解析执行异常: %s" % e, device=self.name, via=self.via)
        if not m.get("ok"):
            return R.unavailable(cap, m.get("err", "未知失败"), device=self.name, via=self.via)
        return R.success(cap, self.name, via=self.via, data=m)


# ============================ 自测（不依赖真实平台，锁死框架正确性） ============================
def _selftest():
    out = {}
    sample = "7.10 a@A.TY 06/24 jPk:/ :8pm 复制打开抖音极速版，看看【凛凛碎的作品】喵喵  https://v.douyin.com/6zGANWG_NBA/"
    # 1. 口令/短链提取 URL
    out["extract_url"] = extract_url(sample)
    # 2. 平台识别
    out["detect_platform"] = detect_platform(sample)
    # 3. 无水印 trick
    playwm = "https://v3-dy-o.xxx.com/playwm/abc.m3u8?a=1"
    out["nwm_trick"] = DouyinResolver._nwm(playwm)
    # 4. 三重签名生成（非空 + 类型正确）
    xb = XBogus("Mozilla/5.0").get_xbogus("a=1&b=2")
    ab = ABogus().get_value({"a": "1", "b": "2"})
    ws, _, _ = WebSign.sign([("a", "1")], "uifid123")
    out["signatures"] = {
        "xbogus_len": len(xb[1]),
        "abogus_len": len(ab),
        "websign_has_sig": "x-secsdk-web-signature=" in ws,
    }
    # 5. SM3 已知向量（"abc" → 标准 SM3）
    out["sm3_abc"] = _sm3("abc")
    # 6. 免登录：详情接口 URL 不含任何签名参数，且不传 cookie 也能构造
    _r = DouyinResolver()
    _url = _r._detail_url("7498720869088513337")
    out["nologin"] = {
        "endpoint_ok": DouyinResolver.DETAIL_ENDPOINT == "https://api.amemv.com/aweme/v1/aweme/detail/",
        "has_aweme_id": "aweme_id=7498720869088513337" in _url,
        "has_aid": "aid=6383" in _url,
        "no_signature": all(s not in _url for s in ("a_bogus", "x_bogus", "x-secsdk", "msToken")),
        "cookie_not_required": _r.cookie == "",
    }
    # 7. 分享页兜底：_ROUTER_DATA 括号匹配 + item 节点定位
    _html = ('x<script>window._ROUTER_DATA = {"a":{"b":{"desc":"hi",'
             '"video":{"play_addr":{"url_list":["https://x/playwm/1.mp4"]}}}}}</script>')
    _rd = DouyinResolver._extract_router_data(_html)
    _node = DouyinResolver._find_item_node(_rd) if _rd else None
    out["share_page_fallback"] = {
        "router_ok": isinstance(_rd, dict),
        "item_found": bool(_node and _node.get("desc") == "hi"),
    }
    # 6. 对接下载引擎：用本地直链走 resolve(download=True) 端到端（platform=direct 绕过联网）
    class _H(http.server.BaseHTTPRequestHandler):
        DATA = b"PARSE_TEST_" * 50000
        def log_message(self, *a):
            pass
        def do_GET(self):
            rng = self.headers.get("Range")
            if rng:
                s, e = rng.replace("bytes=", "").split("-")
                s, e = int(s), int(e)
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (s, e, len(self.DATA)))
                self.send_header("Content-Length", str(e - s + 1))
                self.end_headers()
                self.wfile.write(self.DATA[s:e + 1])
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(self.DATA)))
                self.end_headers()
                self.wfile.write(self.DATA)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    local = "http://127.0.0.1:%d/file.bin" % port
    os.makedirs("tmp", exist_ok=True)
    dest = "tmp/parse_selftest.bin"
    pr = Parse()
    r = pr._resolve({"share": local, "platform": "direct", "download": True, "dest": dest})
    httpd.shutdown()
    out["resolve_download"] = {"ok": r.get("ok"), "downloaded_bytes": os.path.getsize(dest) if os.path.exists(dest) else 0,
                               "expect": len(_H.DATA)}
    out["ok"] = (out["extract_url"] == "https://v.douyin.com/6zGANWG_NBA/" and
                 out["detect_platform"] == "douyin" and
                 out["nwm_trick"] == "https://v3-dy-o.xxx.com/play/abc.m3u8?a=1" and
                 out["signatures"]["xbogus_len"] > 0 and out["signatures"]["abogus_len"] > 0 and
                 out["signatures"]["websign_has_sig"] and
                 out["sm3_abc"] == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0" and
                 all(out["nologin"].values()) and
                 out["share_page_fallback"]["router_ok"] and out["share_page_fallback"]["item_found"] and
                 out["resolve_download"]["downloaded_bytes"] == out["resolve_download"]["expect"])
    return out


if __name__ == "__main__":
    print(json.dumps(_selftest(), ensure_ascii=False, indent=2))
