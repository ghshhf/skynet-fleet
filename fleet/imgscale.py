# -*- coding: utf-8 -*-
"""把 PNG 缩小，只为了「能看见」——不依赖 PIL，纯标准库。

为什么要有这个
--------------
手机截一张屏就是 1280×2772 / 4MB。这么大的图直接丢给看图接口会被拒
（"Image too large"），终端里也没法显示。而没有缩略手段，等于「截图能力
有了但看不见」——能力在、效果零。

环境里没有 PIL（便携 Python 不带第三方包），Add-Type 编译也被安全策略拦，
所以这里自己解 PNG：zlib 解压 → 逐行反滤波 → 最近邻降采样 → 重新编码。

只管 PNG（`screencap -p` 出来的就是 PNG）。遇到不支持的位深/颜色类型
明确报错，**不猜**——猜错的图比没有图更坏。
"""
import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"

# 颜色类型 → 每像素通道数
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(path):
    """解码 PNG → (w, h, channels, bytearray 像素)。只支持 8 位非隔行。"""
    with open(path, "rb") as f:
        blob = f.read()
    if not blob.startswith(_SIG):
        raise ValueError("不是 PNG（文件头不对）")

    pos = len(_SIG)
    ihdr = None
    idat = []
    while pos + 8 <= len(blob):
        (ln,) = struct.unpack(">I", blob[pos:pos + 4])
        typ = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + ln]
        pos += 12 + ln                      # 长度 + 类型 + 数据 + CRC
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif typ == b"IDAT":
            idat.append(data)
        elif typ == b"IEND":
            break

    if not ihdr:
        raise ValueError("PNG 里没有 IHDR")
    w, h, depth, ctype, _comp, _filt, interlace = ihdr
    if depth != 8:
        raise ValueError("只支持 8 位深，这张是 %d 位" % depth)
    if interlace:
        raise ValueError("不支持隔行（Adam7）PNG")
    if ctype not in _CHANNELS:
        raise ValueError("不认识的 PNG 颜色类型 %d" % ctype)
    ch = _CHANNELS[ctype]
    bpp = ch                               # 8 位深时每像素字节数 = 通道数

    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    out = bytearray(h * stride)
    prev = bytearray(stride)
    o = 0
    for y in range(h):
        ft = raw[o]
        o += 1
        line = bytearray(raw[o:o + stride])
        o += stride
        if ft == 0:
            pass
        elif ft == 1:                      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:                      # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:                      # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                      # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        else:
            raise ValueError("第 %d 行的滤波类型不认识：%d" % (y, ft))
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, ch, out


def write_png(path, w, h, ch, pixels):
    """写 8 位 PNG。每行用 filter 0（不滤波）——小图不值得为体积换复杂度。"""
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[ch]
    stride = w * ch
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        rows += pixels[y * stride:(y + 1) * stride]

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    blob = (_SIG
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


def downscale(src, dst, max_w=720, max_h=1600):
    """等比缩到 max_w 以内（也受 max_h 限制）。最近邻：够看清布局，不失真。"""
    w, h, ch, px = read_png(src)
    scale = min(max_w / w, max_h / h, 1.0)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    stride = w * ch
    nstride = nw * ch
    out = bytearray(nh * nstride)
    sxs = [min(w - 1, int(x * w / nw)) * ch for x in range(nw)]
    for y in range(nh):
        sy = min(h - 1, int(y * h / nh))
        src_off = sy * stride
        dst_off = y * nstride
        for x in range(nw):
            so = src_off + sxs[x]
            out[dst_off + x * ch: dst_off + x * ch + ch] = px[so:so + ch]
    size = write_png(dst, nw, nh, ch, out)
    return nw, nh, size


def _selftest():
    """自测：造一张已知像素的图，缩完再解回来逐点核对。"""
    import tempfile
    import os
    d = tempfile.mkdtemp()

    # 4x4 纯色块：左半红、右半蓝，用来验证「缩放后颜色位置没串」
    w = h = 4
    px = bytearray()
    for y in range(h):
        for x in range(w):
            px += bytes((255, 0, 0, 255)) if x < 2 else bytes((0, 0, 255, 255))
    src = os.path.join(d, "a.png")
    write_png(src, w, h, 4, px)

    # 1) 读回来一模一样
    rw, rh, rch, rpx = read_png(src)
    assert (rw, rh, rch) == (4, 4, 4), (rw, rh, rch)
    assert rpx == px, "读回与原图不一致"

    # 2) 缩小：2x2，左上应仍是红、右上应仍是蓝
    dst = os.path.join(d, "b.png")
    nw, nh, size = downscale(src, dst, max_w=2, max_h=2)
    assert (nw, nh) == (2, 2), (nw, nh)
    _, _, _, spx = read_png(dst)
    assert spx[0:3] == b"\xff\x00\x00", spx[0:4]     # 左上红
    assert spx[4:7] == b"\x00\x00\xff", spx[4:8]     # 右上蓝

    # 3) 缩放不放大：小图放进去还是原尺寸
    nw2, nh2, _ = downscale(src, os.path.join(d, "c.png"), max_w=720, max_h=1600)
    assert (nw2, nh2) == (4, 4), (nw2, nh2)

    # 4) 拒绝不支持的东西，不猜
    bad = os.path.join(d, "bad.png")
    open(bad, "wb").write(b"not a png at all")
    try:
        read_png(bad)
        raise AssertionError("坏文件应该报错")
    except ValueError:
        pass

    # 5) 长图按 max_h 收（手机竖屏就是这种：1280x2772）
    tall = os.path.join(d, "tall.png")
    tw, th = 128, 276
    write_png(tall, tw, th, 3, bytearray([9] * (tw * th * 3)))
    nw3, nh3, _ = downscale(tall, os.path.join(d, "tall2.png"), max_w=720, max_h=160)
    assert nh3 == 160 and nw3 < tw, (nw3, nh3)

    print("imgscale 自测：5 组全过")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        w, h, size = downscale(sys.argv[1], sys.argv[2],
                               max_w=int(sys.argv[3]) if len(sys.argv) > 3 else 720)
        print("%dx%d · %dKB → %s" % (w, h, size // 1024, sys.argv[2]))
    else:
        _selftest()
