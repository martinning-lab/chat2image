"""纯标准库二维码生成（byte 模式，版本 1–10，纠错等级 M）。

为什么要自己写：客户端承诺「零依赖，只要 Python 3.8+」，而付款必须给用户一张
能扫的图——当面付的 `qr.alipay.com/...` 链接在电脑浏览器里打不开（2026-11-20 真机
实测），只能用手机支付宝扫。装第三方库会让「装不上」的概率重新出现。

约束与取舍：
- 只做 byte 模式（URL 是 ASCII，够用），不碰数字/字母数字模式；
- 纠错固定 M（约 15%），URL 四十来字符在版本 3 左右，扫起来不挑设备；
- 掩码固定用 0（`(i+j)%2==0`）：规范允许任一合法掩码，解码器不挑；
  选掩码只为美观/可读性，这里不值得引入 8 套惩罚分的实现风险；
- 输出两种：PNG（用户扫）与终端 ASCII（没有文件系统时的兜底）。
"""

import zlib

# 版本 → (每块纠错码字数, [(块数, 每块数据码字数), ...])，纠错等级 M
# 数值来自 ISO/IEC 18004 表 9（M 列）。只列到版本 10（URL 远用不到更大）。
_ECC_M = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
# 每个版本的总数据码字数（用于校验上面的表没写错）
_DATA_CAP = {1: 16, 2: 28, 3: 44, 4: 64, 5: 86, 6: 108, 7: 124, 8: 154, 9: 182, 10: 216}

_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
          6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}


def _data_capacity(ver):
    total = 0
    for nblocks, ncodewords in _ECC_M[ver][1]:
        total += nblocks * ncodewords
    return total


def _pick_version(nbytes):
    for v in range(1, 11):
        # 模式(4bit) + 长度(8bit for v1-9) + 数据 + 终止符，按字节算要留出余量
        need_bits = 4 + (8 if v <= 9 else 16) + nbytes * 8
        cap_bits = _data_capacity(v) * 8
        if need_bits + 4 <= cap_bits:
            return v
    raise ValueError("内容太长，本实现只支持到版本 10")


# ---------------- GF(256) 与 Reed–Solomon ----------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D          # 本原多项式 x^8+x^4+x^3+x^2+1
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_ecc(data, nsym):
    """算纠错码字（生成多项式除法）"""
    gen = [1]
    for i in range(nsym):
        # 乘以 (x - a^i)：系数按「高次→低次」存。
        # gen*x 让每项上移一位（索引不变、新多项式多一项在前面），
        # gen*base 让每项落在下一个索引——以前这两个写反了，
        # 生成多项式整条错位，算出来的纠错码字对不上（syndrome 非 0，图扫不出来）。
        nxt = [0] * (len(gen) + 1)
        base = _EXP[i]
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _gf_mul(g, base)
        gen = nxt
    rem = list(data) + [0] * nsym
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j, g in enumerate(gen):
                rem[i + j] ^= _gf_mul(g, coef)
    return rem[len(data):]


# ---------------- 比特流 ----------------
def _bits(data: bytes, ver):
    out = []

    def push(val, n):
        for i in range(n - 1, -1, -1):
            out.append((val >> i) & 1)

    push(0b0100, 4)                     # byte 模式
    push(len(data), 8 if ver <= 9 else 16)
    for byte in data:
        push(byte, 8)
    cap = _data_capacity(ver) * 8
    # 终止符最多 4 个 0
    for _ in range(min(4, cap - len(out))):
        out.append(0)
    while len(out) % 8:
        out.append(0)
    # 填充码字 0xEC / 0x11 交替
    pad = 0
    while len(out) < cap:
        push(0xEC if pad % 2 == 0 else 0x11, 8)
        pad += 1
    codewords = [int("".join(map(str, out[i:i + 8])), 2) for i in range(0, len(out), 8)]
    return codewords


def _interleave(codewords, ver):
    nsym, groups = _ECC_M[ver]
    blocks, ecblocks, idx = [], [], 0
    for nblocks, ncodewords in groups:
        for _ in range(nblocks):
            blk = codewords[idx:idx + ncodewords]
            idx += ncodewords
            blocks.append(blk)
            ecblocks.append(_rs_ecc(blk, nsym))
    # 数据按列交织，再交错误码字
    out = []
    maxlen = max(len(b) for b in blocks)
    for i in range(maxlen):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(nsym):
        for e in ecblocks:
            out.append(e[i])
    return out


# ---------------- 矩阵 ----------------
def _set_finder(m, size, r, c):
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r + dr, c + dc
            if not (0 <= rr < size and 0 <= cc < size):
                continue
            dark = (0 <= dr <= 6 and 0 <= dc <= 6 and
                    (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)))
            m[rr][cc] = 1 if dark else 0


def _base_matrix(ver):
    """功能图形 + 保留区。返回值 (矩阵, 是否功能模块)。

    以前这里有个单独的 _reserved() 用近似范围去猜保留区，边界一错，
    数据位就会写进功能区，整张图扫不出来（本机 zbarimg 解码为空）。
    现在改成**边画边标**：画进去的就是功能模块，不需要第二套坐标。
    """
    size = ver * 4 + 17
    m = [[0] * size for _ in range(size)]
    f = [[False] * size for _ in range(size)]

    def put(r, c, v):
        if 0 <= r < size and 0 <= c < size:
            m[r][c] = v
            f[r][c] = True

    # 三个定位图形（含分隔符）
    for (r0, c0) in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                dark = (0 <= dr <= 6 and 0 <= dc <= 6 and
                        (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)))
                put(r, c, 1 if dark else 0)

    # 定时图形：第 6 行/列，偶数坐标是黑
    for i in range(size):
        if not f[6][i]:
            put(6, i, 1 if i % 2 == 0 else 0)
        if not f[i][6]:
            put(i, 6, 1 if i % 2 == 0 else 0)

    # 校正图形
    for r in _ALIGN[ver]:
        for c in _ALIGN[ver]:
            if (r, c) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, 1 if max(abs(dr), abs(dc)) != 1 else 0)

    # 格式信息两处 + 版本信息（先占位，值稍后写）。
    # **必须跳过 i==6**：那是定时图形的位置，不是格式位。
    # 之前没跳过，(6,8)/(8,6) 被写成 0，定时图形断掉，整张图扫不出来。
    for i in range(9):
        if i != 6:
            put(8, i, 0)
            put(i, 8, 0)
    for i in range(8):
        put(8, size - 1 - i, 0)
        put(size - 1 - i, 8, 0)
    put(size - 8, 8, 1)                 # dark module，固定黑
    if ver >= 7:
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            put(a, b, 0)
            put(b, a, 0)
    return m, f


def _place_data(m, _func, ver, bits, mask=0):
    size = ver * 4 + 17
    res = _func
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if res[row][c]:
                    continue
                bit = bits[idx] if idx < len(bits) else 0
                idx += 1
                if mask == 0 and (row + c) % 2 == 0:
                    bit ^= 1
                m[row][c] = bit
        upward = not upward
        col -= 2


def _format_bits(mask=0, ecc_m=0b00):
    """格式信息：EC 等级(2bit) + 掩码(3bit) + BCH(10bit)，再异或固定掩码。
    返回 LSB 起的 15 个 bit。"""
    data = (ecc_m << 3) | mask
    rem = data << 10
    for i in range(14, 9, -1):
        if (rem >> i) & 1:
            rem ^= 0b10100110111 << (i - 10)
    bits = ((data << 10) | rem) ^ 0b101010000010010
    return [(bits >> (14 - i)) & 1 for i in range(15)]


def _version_bits(ver):
    rem = ver << 12
    for i in range(17, 11, -1):
        if (rem >> i) & 1:
            rem ^= 0b1111100100101 << (i - 12)
    bits = (ver << 12) | rem
    # 版本信息按 LSB 起放置：与 segno 的矩阵逐模块比对得出（MSB 起会整块错位）
    return [(bits >> i) & 1 for i in range(18)]


def _place_format(m, ver):
    """格式信息按 MSB 起放进两处坐标序列（顺序见 coords1/coords2）。

    位序这类东西书面说法常打架，最终以「图能不能被解码器读出来」为准：
    本机 zbarimg 与 segno 矩阵逐模块对比过。
    """
    size = ver * 4 + 17
    bits = _format_bits()
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
               (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    coords2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
               (size - 5, 8), (size - 6, 8), (size - 7, 8),
               (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
               (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
    for bit, (r, c) in zip(bits, coords1):
        m[r][c] = bit
    for bit, (r, c) in zip(bits, coords2):
        m[r][c] = bit
    if ver >= 7:
        vb = _version_bits(ver)
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            m[a][b] = vb[i]
            m[b][a] = vb[i]


def matrix(text, mask=0):
    """返回 QR 矩阵（1=黑, 0=白），含静区外的不含静区。"""
    data = text.encode("utf-8")
    ver = _pick_version(len(data))
    bits = []
    for cw in _interleave(_bits(data, ver), ver):
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    m, func = _base_matrix(ver)
    _place_data(m, func, ver, bits, mask=mask)
    _place_format(m, ver)
    return m


# ---------------- 输出 ----------------
def png_bytes(m, scale=8, border=4):
    """最小 PNG 编码器（灰度 8bit，无外部依赖）"""
    size = len(m)
    w = (size + border * 2) * scale
    rows = []
    for y in range(w):
        row = bytearray([0])            # filter type 0
        rr = y // scale - border
        for x in range(w):
            cc = x // scale - border
            dark = 0 <= rr < size and 0 <= cc < size and m[rr][cc] == 1
            row.append(0 if dark else 255)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, payload):
        return (len(payload).to_bytes(4, "big") + tag + payload +
                zlib.crc32(tag + payload).to_bytes(4, "big"))

    ihdr = (w.to_bytes(4, "big") + w.to_bytes(4, "big") + bytes([8, 0, 0, 0, 0]))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ascii_art(m, border=2):
    """终端用：两块高、一块宽（用半块字符装两行），手机对着屏幕也能扫"""
    size = len(m)
    lines = []
    for _ in range(border):
        lines.append("  " * (size + border * 2))
    for y in range(0, size, 2):
        line = ["  "] * border
        for x in range(size):
            top = m[y][x] == 1
            bot = (y + 1 < size) and m[y + 1][x] == 1
            line.append("██" if top and bot else
                        "▀▀" if top and not bot else
                        "▄▄" if not top and bot else "  ")
        line += ["  "] * border
        lines.append("".join(line))
    for _ in range(border):
        lines.append("  " * (size + border * 2))
    return "\n".join(lines)
