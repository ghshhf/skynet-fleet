"""定位层：一张图、一个文件、一个 IP，各自带着多少「它在哪」的线索。

为什么单独一层
--------------
主人在意的场景很实在：**发出去的一张照片，里面有没有说清你在哪**。
这件事和 osint 不同——osint 是去网上查一个标识，这里是从**内容本身**里读。
一张 JPEG 的 EXIF 里可能写着拍摄点的经纬度（米级），一个 PDF 里可能写着作者
和最后保存的机器路径，一个 IP 只到城市级。三种精度差三个数量级，
必须分开说，混在一起讲就是骗人。

最要命的一个坑：坐标系
----------------------
GPS 记录的原始坐标是 **WGS84**（国际标准）。但国内地图（高德/腾讯/百度）用的是
**GCJ-02（火星坐标）** 或 **BD-09**。直接把 WGS84 的经纬度丢给高德，会**偏
300–500 米**——在城市里就是错了一个街区。很多人以为"地图不准"，其实是没转。

所以这里所有输出都同时给三套坐标 + 各自对应的地图链接，让人点开就是对的。

精度分级（写死在这里，不许模糊）
--------------------------------
| 来源 | 精度 | 说明 |
|---|---|---|
| EXIF GPS | 米级 | 拍照那一刻的真实位置，除非被编辑过 |
| IP 定位 | 城市级 | 通常 5–50 km，只能说到「哪个城市」 |
| 文件元数据 | 无 | 只有作者/软件/路径，不含位置（除非文件名带） |

诚实边界
--------
- **这是防御性工具**：用来检查「我发出去的这张图暴露了什么」，不是用来定位别人。
- 只读本地文件，不上传任何内容；IP 查询走公开地理库（城市级）。
- 被压缩/经过微信等平台转发的图，EXIF 通常已被剥掉——查不到不是工具坏了。
"""
import json
import math
import os
import re
import struct
import time
import urllib.parse

from ..core import BaseDevice, DeviceResult as R
from .. import net

# ============================================================ 坐标系

#: WGS84 → GCJ-02 的标准公开参数（国内地图必须这一步，否则偏移 300–500 米）
_A = 6378245.0                  # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323    # 第一偏心率平方
_X_PI = math.pi * 3000.0 / 180.0
_PI = math.pi


def _out_of_china(lng, lat):
    """粗略判断是否在中国境外。境外不做偏移（GCJ-02 只在国内生效）。"""
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(lng, lat):
    ret = (-100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat
           + 0.2 * math.sqrt(abs(lng)))
    ret += (20.0 * math.sin(6.0 * lng * _PI) + 20.0 * math.sin(2.0 * lng * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * _PI) + 40.0 * math.sin(lat / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * _PI) + 320 * math.sin(lat * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(lng, lat):
    ret = (300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat
           + 0.1 * math.sqrt(abs(lng)))
    ret += (20.0 * math.sin(6.0 * lng * _PI) + 20.0 * math.sin(2.0 * lng * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * _PI) + 40.0 * math.sin(lng / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * _PI) + 300.0 * math.sin(lng / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng, lat):
    """国际坐标 → 火星坐标（高德/腾讯用这个）。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng, lat):
    """火星坐标 → 国际坐标（反向，一代迭代够用，误差 < 1 米）。"""
    if _out_of_china(lng, lat):
        return lng, lat
    glng, glat = wgs84_to_gcj02(lng, lat)
    return lng * 2 - glng, lat * 2 - glat


def gcj02_to_bd09(lng, lat):
    """火星坐标 → 百度坐标。"""
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def bd09_to_gcj02(lng, lat):
    x = lng - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def _fmt(v, n=6):
    return None if v is None else round(float(v), n)


def coords_all(lng, lat, source="wgs84"):
    """给一个坐标，返回三套坐标系 + 各自的地图链接。

    source 说明这个坐标原本是哪套——EXIF/IP 查询结果都是 wgs84。
    """
    if source == "gcj02":
        gcj = (lng, lat)
        wgs = gcj02_to_wgs84(lng, lat)
    elif source == "bd09":
        gcj = bd09_to_gcj02(lng, lat)
        wgs = gcj02_to_wgs84(*gcj)
    else:
        wgs = (lng, lat)
        gcj = wgs84_to_gcj02(lng, lat)
    bd = gcj02_to_bd09(*gcj)
    return {
        "wgs84": [_fmt(wgs[0]), _fmt(wgs[1])],
        "gcj02": [_fmt(gcj[0]), _fmt(gcj[1])],
        "bd09": [_fmt(bd[0]), _fmt(bd[1])],
        "links": {
            # 国内三家用 GCJ-02 / BD-09，点开才是对的
            "amap": "https://uri.amap.com/marker?position=%s,%s&name=%s"
                    % (_fmt(gcj[0]), _fmt(gcj[1]), urllib.parse.quote("位置")),
            "tencent": "https://apis.map.qq.com/uri/v1/marker?marker=coord:%s,%s;title:%s"
                       % (_fmt(gcj[1]), _fmt(gcj[0]), urllib.parse.quote("位置")),
            "baidu": "https://api.map.baidu.com/marker?location=%s,%s&title=%s&content=%s"
                     % (_fmt(bd[1]), _fmt(bd[0]), urllib.parse.quote("位置"),
                        urllib.parse.quote("位置")),
            # 国际两家用 WGS-84
            "osm": "https://www.openstreetmap.org/?mlat=%s&mlon=%s#map=18/%s/%s"
                   % (_fmt(wgs[1]), _fmt(wgs[0]), _fmt(wgs[1]), _fmt(wgs[0])),
            "google": "https://www.google.com/maps?q=%s,%s" % (_fmt(wgs[1]), _fmt(wgs[0])),
        },
        "note": ("国内地图（高德/腾讯/百度）用 GCJ-02 / BD-09，直接喂 WGS-84 会偏 300–500 米。"
                 "这里已经转好，点开即准。"),
    }


# ============================================================ EXIF

def _gps_to_decimal(val, ref):
    """EXIF 的 GPS 是 (度, 分, 秒) 三个有理数。转十进制小数。

    ref 是 N/S/E/W —— 忘了看这个符号，南半球和西半球的坐标会整体错号。
    每个分量既可能是 PIL 的 IFDRational，也可能是 (分子, 分母) 元组。
    """
    try:
        parts = [_rat(x) for x in val]
    except Exception:
        return None
    if len(parts) != 3 or any(p is None for p in parts):
        return None
    d, m, s = parts
    dec = d + m / 60.0 + s / 3600.0
    if str(ref).upper() in ("S", "W"):
        dec = -dec
    return dec


def _rat(v):
    """PIL 的 IFDRational 转 float；元组型也兼容。"""
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1]) if v[1] else 0.0
        return float(v)
    except Exception:
        return None


def read_image(path):
    """读一张图的 EXIF。返回结构化结果（不抛异常，读不到就说读不到）。"""
    out = {"ok": False, "path": path, "bytes": 0, "format": "", "size": None}
    try:
        st = os.stat(path)
        out["bytes"] = st.st_size
        out["mtime"] = st.st_mtime
    except Exception as e:
        out["err"] = "读不到文件：%s" % e
        return out

    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
    except Exception as e:
        out["err"] = "没有 PIL，读不了 EXIF：%s" % e
        return out

    try:
        with Image.open(path) as im:
            out["format"] = im.format or ""
            out["size"] = list(im.size)
            out["mode"] = im.mode
            exif = im.getexif()
    except Exception as e:
        out["err"] = "不是可识别的图片，或文件已损坏：%s" % e
        return out

    tags = {}
    gps = {}
    if exif:
        for tag_id, val in exif.items():
            name = TAGS.get(tag_id, str(tag_id))
            if name == "GPSInfo":
                for k, v in (val or {}).items():
                    gps[GPSTAGS.get(k, str(k))] = v
            else:
                tags[name] = val

    # 常用字段抽出来，原始字典也留着（有些冷门字段只有原始值有用）
    def pick(name):
        v = tags.get(name)
        if v is None:
            return None
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8", "replace").strip("\x00").strip()
            except Exception:
                return v.hex()[:64]
        if isinstance(v, (int, float, str)):
            return v
        return str(v)[:120]

    info = {
        "make": pick("Make"),
        "model": pick("Model"),
        "lens": pick("LensModel"),
        "software": pick("Software"),
        "datetime": pick("DateTime") or pick("DateTimeOriginal"),
        "datetime_original": pick("DateTimeOriginal"),
        "datetime_digitized": pick("DateTimeDigitized"),
        "orientation": pick("Orientation"),
        "artist": pick("Artist"),
        "copyright": pick("Copyright"),
        "image_description": pick("ImageDescription"),
        "user_comment": pick("UserComment"),
        "host_computer": pick("HostComputer"),
        "serial": pick("BodySerialNumber") or pick("CameraSerialNumber"),
    }
    info = {k: v for k, v in info.items() if v not in (None, "", 0)}

    lat = lng = None
    alt = None
    gps_out = {}
    if gps:
        if "GPSLatitude" in gps and "GPSLongitude" in gps:
            lat = _gps_to_decimal(gps["GPSLatitude"], gps.get("GPSLatitudeRef", "N"))
            lng = _gps_to_decimal(gps["GPSLongitude"], gps.get("GPSLongitudeRef", "E"))
        if "GPSAltitude" in gps:
            alt = _rat(gps["GPSAltitude"])
        for k in ("GPSDateStamp", "GPSTimeStamp", "GPSProcessingMethod",
                  "GPSImgDirection", "GPSSpeed", "GPSDOP"):
            if k in gps:
                v = gps[k]
                gps_out[k] = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)[:80]

    out.update({
        "ok": True,
        "exif_present": bool(tags or gps),
        "camera": info,
        "gps_raw": gps_out,
        "location": None,
    })
    if lat is not None and lng is not None and not (abs(lat) < 1e-9 and abs(lng) < 1e-9):
        loc = coords_all(lng, lat, "wgs84")
        loc.update({"lat": _fmt(lat), "lng": _fmt(lng), "altitude_m": _fmt(alt, 1) if alt else None,
                    "precision": "米级（拍摄点）",
                    "source": "EXIF GPS"})
        out["location"] = loc
    out["raw_exif_count"] = len(tags) + len(gps)
    return out


def read_pdf(path):
    """PDF 元数据：作者、标题、创建工具、创建/修改时间。"""
    out = {"ok": False, "path": path, "bytes": os.path.getsize(path) if os.path.exists(path) else 0}
    try:
        from pypdf import PdfReader
    except Exception as e:
        out["err"] = "没有 pypdf：%s" % e
        return out
    try:
        r = PdfReader(path)
        md = r.metadata or {}
        keep = {}
        for k, v in dict(md).items():
            kk = str(k).lstrip("/")
            try:
                keep[kk] = str(v)[:200]
            except Exception:
                pass
        out.update({"ok": True, "pages": len(r.pages), "metadata": keep})
    except Exception as e:
        out["err"] = "PDF 读取失败：%s" % e
    return out


def read_office(path):
    """docx/xlsx/pptx 是 zip，核心属性在 docProps/core.xml 与 app.xml。"""
    import zipfile
    out = {"ok": False, "path": path, "bytes": os.path.getsize(path) if os.path.exists(path) else 0}
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            picked = {}
            for target in ("docProps/core.xml", "docProps/app.xml"):
                if target in names:
                    raw = z.read(target).decode("utf-8", "replace")
                    for tag in ("creator", "lastModifiedBy", "created", "modified",
                                "title", "subject", "Company", "Application",
                                "AppVersion", "TotalTime"):
                        m = re.search(r"<%s[^>]*>([^<]*)</%s>" % (tag, tag), raw)
                        if m and m.group(1).strip():
                            picked[tag] = m.group(1).strip()[:200]
            out.update({"ok": True, "metadata": picked, "entries": len(names)})
    except Exception as e:
        out["err"] = "不是 Office 文档或已损坏：%s" % e
    return out


def read_any(path):
    """按扩展名分派。认不出的类型也返回基本信息，不当失败。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".bmp", ".gif"):
        return read_image(path)
    if ext == ".pdf":
        return read_pdf(path)
    if ext in (".docx", ".xlsx", ".pptx", ".docm", ".xlsm"):
        return read_office(path)
    out = {"ok": True, "path": path, "kind": "other", "bytes": 0}
    try:
        st = os.stat(path)
        out.update({"bytes": st.st_size, "mtime": st.st_mtime})
    except Exception as e:
        out.update({"ok": False, "err": str(e)})
    out["note"] = "这类文件（%s）不解析元数据；只给了大小与时间" % (ext or "无扩展名")
    return out


# ============================================================ IP
_IP_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


def ip_locate(ip):
    """IP → 城市级坐标。返回经纬度 + 三套坐标链接 + 归属信息。

    精度必须说清：IP 定位是**城市级**，通常误差 5–50 km。它能告诉你在哪个城市，
    不能告诉你住哪栋楼。把它当门牌号用，一定会失望。
    """
    ip = str(ip).strip()
    if not _IP_RE.fullmatch(ip):
        return {"ok": False, "err": "这不是一个合法 IPv4 地址：%r" % ip}
    try:
        with net.urlopen("https://ipwho.is/%s" % ip, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "err": "IP 查询失败：%s" % e}
    if not data.get("success", True):
        return {"ok": False, "err": data.get("message") or "查询被拒"}
    lat, lng = data.get("latitude"), data.get("longitude")
    conn = data.get("connection") or {}
    out = {
        "ok": True, "ip": ip, "type": data.get("type"),
        "country": data.get("country"), "region": data.get("region"),
        "city": data.get("city"), "postal": data.get("postal"),
        "timezone": (data.get("timezone") or {}).get("id"),
        "isp": conn.get("isp"), "org": conn.get("org"), "asn": conn.get("asn"),
        "source": "ipwho.is（免费公开地理库）",
        "precision": "城市级（通常 5–50 km，不是门牌号）",
    }
    if lat is not None and lng is not None:
        out["location"] = coords_all(lng, lat, "wgs84")
        out["location"].update({"lat": _fmt(lat), "lng": _fmt(lng),
                                "precision": "城市级", "source": "IP 地理库"})
    return out


def find_ips(text, limit=20):
    """从一段文本（比如截图的 OCR 结果、日志、文件名）里抠出所有 IP。"""
    seen, out = set(), []
    for m in _IP_RE.finditer(str(text)):
        ip = m.group(0)
        if ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
        if len(out) >= limit:
            break
    return out


# ============================================================ 设备

class Geo(BaseDevice):
    """kind=geo：把「内容里的位置线索」读出来并落到地图上。"""

    kind = "geo"
    via = "local"

    def __init__(self, name="geo:local", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "读图片 EXIF / 文件元数据 / IP 定位；防御性用途——看自己发出去的东西暴露了什么")

    def probe(self):
        try:
            from PIL import Image  # noqa: F401
        except Exception as e:
            return False, "没有 PIL（pip install pillow）：%s" % e
        return True, ""

    def caps(self):
        return CAPS

    def invoke(self, cap, args=None):
        args = dict(args or {})
        if cap not in CAPS:
            return R.unsupported(cap, "%s（%s）没有这项能力" % (self.name, self.kind),
                                 device=self.name, via=self.via)
        fn = getattr(self, "_do_" + cap.split(".", 1)[1], None)
        if fn is None:
            return R.unsupported(cap, "这项能力没接上实现", device=self.name, via=self.via)
        try:
            return fn(args)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 能力 ----------------

    def _do_image(self, args):
        path = args.get("path")
        if not path:
            return R.error("geo.image", "需要 path（图片路径）",
                           device=self.name, via=self.via)
        if not os.path.exists(path):
            return R.unavailable("geo.image", "文件不存在：%s" % path,
                                 device=self.name, via=self.via)
        r = read_image(path)
        if not r.get("ok"):
            return R.unavailable("geo.image", r.get("err") or "读不了这张图",
                                 device=self.name, via=self.via)
        data = dict(r)
        if not data.get("location"):
            data["verdict"] = ("这张图里没有 GPS 信息。常见原因：相机没开定位、"
                               "图被微信/微博等平台压缩时剥掉了 EXIF、或是截图。")
        else:
            data["verdict"] = ("这张图带 GPS！直接发原图等于公开拍摄点（米级）。"
                               "要发就先去 EXIF，或者只发截图。")
            data["privacy"] = _privacy_advice(data)
        return R.success("geo.image", self.name, via=self.via, data=data)

    def _do_file(self, args):
        path = args.get("path")
        if not path:
            return R.error("geo.file", "需要 path", device=self.name, via=self.via)
        if not os.path.exists(path):
            return R.unavailable("geo.file", "文件不存在：%s" % path,
                                 device=self.name, via=self.via)
        r = read_any(path)
        if not r.get("ok"):
            return R.unavailable("geo.file", r.get("err") or "读不了这个文件",
                                 device=self.name, via=self.via)
        # 文件名里可能带 IP
        ips = find_ips(os.path.basename(path))
        if ips:
            r["ips_in_name"] = ips
            r["ip_location"] = [ip_locate(i) for i in ips[:3]]
        return R.success("geo.file", self.name, via=self.via, data=r)

    def _do_ip(self, args):
        ips = args.get("ip") or args.get("ips")
        if not ips:
            return R.error("geo.ip", "需要 ip（单个或逗号分隔）", device=self.name, via=self.via)
        if isinstance(ips, str):
            ips = [i.strip() for i in ips.split(",") if i.strip()]
        results = []
        for ip in ips[:10]:
            results.append(ip_locate(ip))
        ok = [r for r in results if r.get("ok")]
        return R.success("geo.ip", self.name, via=self.via, data={
            "count": len(results), "ok_count": len(ok),
            "results": results,
            "precision": "城市级——能定位到城市，不能定位到门牌号",
        })

    def _do_locate(self, args):
        """统一入口：给我一个路径或一个 IP，我自己判断该走哪条路。"""
        target = args.get("target")
        if not target:
            return R.error("geo.locate", "需要 target（文件路径 / IP / 目录）",
                           device=self.name, via=self.via)
        if _IP_RE.fullmatch(str(target).strip()):
            r = ip_locate(target)
            return R.success("geo.locate", self.name, via=self.via,
                             data={"kind": "ip", "result": r})

        if not os.path.exists(target):
            # 不是本地路径，也不是 IP：试试从字符串里抠 IP
            ips = find_ips(target)
            if ips:
                return R.success("geo.locate", self.name, via=self.via, data={
                    "kind": "text", "ips": ips,
                    "results": [ip_locate(i) for i in ips[:5]]})
            return R.unavailable("geo.locate",
                                 "既不是存在的路径，也不是 IP：%r" % target,
                                 device=self.name, via=self.via)

        if os.path.isdir(target):
            return self._do_batch({"dir": target, "limit": args.get("limit") or 200})

        r = read_any(target)
        if not r.get("ok"):
            return R.unavailable("geo.locate", r.get("err") or "读不了", device=self.name, via=self.via)
        return R.success("geo.locate", self.name, via=self.via, data={
            "kind": "file", "result": r,
            "has_location": bool(r.get("location")),
        })

    def _do_batch(self, args):
        """扫一个目录：哪些文件带定位信息。查自己的相册/图库时最有用。"""
        d = args.get("dir") or args.get("path")
        if not d or not os.path.isdir(d):
            return R.error("geo.batch", "需要 dir（要扫的目录）", device=self.name, via=self.via)
        limit = int(args.get("limit") or 300)
        exts = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic")
        hits, scanned, total = [], 0, 0
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for fn in files:
                if not fn.lower().endswith(exts):
                    continue
                total += 1
                if scanned >= limit:
                    break
                p = os.path.join(root, fn)
                scanned += 1
                try:
                    r = read_image(p)
                except Exception:
                    continue
                if r.get("ok") and r.get("location"):
                    loc = r["location"]
                    hits.append({
                        "path": p,
                        "lat": loc.get("lat"), "lng": loc.get("lng"),
                        "when": (r.get("camera") or {}).get("datetime"),
                        "camera": "%s %s" % ((r.get("camera") or {}).get("make") or "",
                                             (r.get("camera") or {}).get("model") or ""),
                        "amap": loc["links"]["amap"],
                    })
            if scanned >= limit:
                break
        hits.sort(key=lambda x: x.get("when") or "")
        return R.success("geo.batch", self.name, via=self.via, data={
            "dir": d, "scanned": scanned, "total_images": total,
            "with_gps": len(hits),
            "hits": hits[:100],
            "verdict": ("扫了 %d 张，%d 张带 GPS 坐标——这些原图发出去就等于公开拍摄点。"
                        % (scanned, len(hits))),
            "note": "只扫常见图片格式；超过 limit 就停，不递归无底洞。",
        })


CAPS = ("geo.image", "geo.file", "geo.ip", "geo.locate", "geo.batch")


def _privacy_advice(data):
    tips = ["发之前先剥 EXIF（Windows：属性 → 详细信息 → 删除属性和个人信息）",
            "或者直接截图再发——截图不带原始 EXIF"]
    cam = data.get("camera") or {}
    if cam.get("model"):
        tips.append("设备型号（%s）也会一并暴露" % cam["model"])
    if cam.get("datetime_original"):
        tips.append("拍摄时间（%s）同样在内" % cam["datetime_original"])
    return tips


# ============================================================ 自测

def _selftest():
    ok = []

    def chk(name, cond, extra=""):
        ok.append((name, bool(cond), extra))

    # 坐标转换：北京天安门 WGS84 → GCJ02 应该往东北偏约 500 米
    lng, lat = 116.3912, 39.9073
    g = wgs84_to_gcj02(lng, lat)
    d = math.hypot((g[0] - lng) * 111000 * math.cos(math.radians(lat)),
                   (g[1] - lat) * 111000)
    chk("WGS84→GCJ02 偏移量合理(100–800m)", 100 < d < 800, "%.0f m" % d)
    back = gcj02_to_wgs84(*g)
    chk("GCJ02→WGS84 可逆", abs(back[0] - lng) < 1e-5 and abs(back[1] - lat) < 1e-5,
        str(back))
    bd = gcj02_to_bd09(*g)
    g2 = bd09_to_gcj02(*bd)
    chk("BD09 往返", abs(g2[0] - g[0]) < 1e-5 and abs(g2[1] - g[1]) < 1e-5)
    chk("境外不偏移", wgs84_to_gcj02(-74.0, 40.7) == (-74.0, 40.7))

    c = coords_all(116.3912, 39.9073)
    chk("三套坐标齐全", all(k in c for k in ("wgs84", "gcj02", "bd09")))
    chk("五家地图链接", len(c["links"]) == 5, str(list(c["links"])))
    chk("高德链接用 GCJ02", "position=116.39" in c["links"]["amap"])

    # 度分秒
    chk("DMS 转十进制", abs(_gps_to_decimal([(116, 1), (23, 1), (28, 1)], "E") - 116.391111) < 1e-4)
    chk("S/W 取负", _gps_to_decimal([(74, 1), (0, 1), (0, 1)], "W") < 0)

    # IP 抽取
    found = find_ips("server 8.8.8.8 and 114.114.114.114, bad 999.1.1.1")
    chk("IP 抽取", found == ["8.8.8.8", "114.114.114.114"], str(found))

    # EXIF 写读闭环
    try:
        from PIL import Image
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.jpg")
            im = Image.new("RGB", (10, 10), "red")
            ex = im.getexif()
            ex[271] = "TestMake"      # Make
            ex[272] = "TestModel"     # Model
            im.save(p, exif=ex)
            r = read_image(p)
            chk("EXIF 读取", r.get("ok") and (r.get("camera") or {}).get("model") == "TestModel",
                str(r.get("camera")))
            chk("无 GPS 时 location 为 None", r.get("location") is None)
    except Exception as e:
        chk("EXIF 闭环", False, str(e))

    passed = sum(1 for _, c, _ in ok if c)
    for n, c, x in ok:
        print("  %s %s %s" % ("✓" if c else "✗", n, x))
    print("geo selftest: %d/%d" % (passed, len(ok)))
    return passed == len(ok)


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
