#!/usr/bin/env python3
"""chat2image 升级工具集（零依赖，只用 Python 标准库）

契约《生图Skill商业化-开源与自动升级-契约v1》§3/§4。本文件只做**只读**的事：
  1. 拉 `GET /v1/version` 与 `GET /v1/changelog`（匿名、不重试、不换源）
  2. 语义化版本比较
  3. RSA-2048 PKCS#1 v1.5 + SHA-256 验签（标准库 `pow()`，不引 cryptography）

**这里不写盘**：解压、原子 rename、回滚由 `upgrade` 命令负责（见 client.py）。
单独拆一个文件是为了两件事好测：验签是纯函数；清单解析不需要起实例。
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

UPDATE_PROTOCOL = "chat2image-update-v1"

# 🔴 公钥 pin 在这里（契约 §3）。私钥只在部署机，**不进仓库、不进包、不进 CI 变量明文**。
#
# 这把钥 2026-11-21 由运维官在部署机生成：
#   私钥 /srv/chat2image/secrets/update-signing.key（600 root:root，不出机器）
#   公钥指纹 = SPKI DER 的 sha256 = e1f5d216ec0109cf1dcbff44ecb33b4486fcfc8fa07ad693e86376627e19d257
# 发布前的自检会从下面这段模数**重新编码 DER 并核对指纹**——
# 所以这不是「抄了一串数字」，是被验证过与运维官报的指纹逐字一致的那一把。
#
# 换钥规则见契约 §3：轮换 = 先发一版能验旧钥的过渡版（同时接受新旧两把），v0 不做轮换机制。
PUBKEY_N_HEX = (
    "d8b02f2763dc2fb1b054f19932bfd41d4b3822d1e535cd2d86e4b4341a4bccebba37a9cfbb5454cbeb1a75110b656b0ec055"
    "906420afefb3cdfe8d8d03288fe6fc1449cee1a0ef2622b1167777e66fa7a5f39207086d188349eab44e2cf05fd1edbc85a0"
    "cf62db4ba02e092507a30584a1d6a04817f0c6a02d268837ad97a8827f86fa884327645badc5348dc2b354d291763c498cf4"
    "8739847b1a2a31f67ac8ff6c9b5f698cdfbeb2bfcc8cb6d60728e95c3c8f2676d51d1c187a981875e38ff327776e003d8c78"
    "9796403f7184b6515ae6cb7365423e90f7586df7ecd9722e222ea01e2f5da2468882b1bd2cc3a5b57b678b4aeb441cdc3ebb"
    "4495cc277d0b"
)
PUBKEY_N = int(PUBKEY_N_HEX, 16)
PUBKEY_E = 65537

# SHA-256 的 DigestInfo DER 前缀（RFC 8017 §9.2 注 1）：
#   SEQUENCE { SEQUENCE { OID 2.16.840.1.101.3.4.2.1, NULL }, OCTET STRING(32) }
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


# ---------------- 版本 ----------------
def parse_version(v):
    """`1.2.3` → (1,2,3)。解析不了返回 None —— 调用方必须当成「比不了」，不是「相等」。"""
    parts = []
    for p in str(v or "").strip().split("."):
        if not p.isdigit():
            return None
        parts.append(int(p))
    return tuple(parts) if parts else None


def cmp_version(a, b):
    """返回 -1 / 0 / 1；任一侧解析不了返回 None。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


# ---------------- 验签（契约 §3）----------------
def signed_message(version, sha256, file):
    """签名对象（规范化串，`\\n` 分隔，末尾无换行）。改一个字节都算另一份。"""
    return f"{UPDATE_PROTOCOL}\n{version}\n{sha256}\n{file}".encode("utf-8")


def verify_signature(n, e, sig_b64, message):
    """严格 PKCS#1 v1.5 + SHA-256 验签。**任何一处不严格都返回 False**。

    严格的意思（代码审查要求的四道关，缺一条就等于放行伪造包）：
      ① 签名长度必须正好是模长 k 字节（少一个字节不是「补零」，是不同签名；
         多一个字节更不能容忍），`s < n`；
      ② 填充必须是 `00 01 FF…FF 00`，且 FF 至少 8 字节（RFC 8017）；
      ③ 填充后的 DigestInfo 前缀逐字节比对（不是「以 sha256 摘要结尾」就算过）；
      ④ 摘要比较用 `hmac.compare_digest`（常量时间）。
    宽松解析是手写 RSA 验签最常见的洞：只比尾部 32 字节，等于允许攻击者
    自己拼 DigestInfo —— 那样任何人的签名都能改成「我们发的」。
    """
    if not n or not e:
        return False
    try:
        raw = base64.b64decode(sig_b64, validate=True)
    except (ValueError, TypeError, base64.binascii.Error):
        return False
    k = (int(n).bit_length() + 7) // 8
    if len(raw) != k:                       # ① 长度严格
        return False
    s = int.from_bytes(raw, "big")
    if s >= int(n):
        return False
    em = pow(s, int(e), int(n)).to_bytes(k, "big")

    if em[0:2] != b"\x00\x01":              # ② 填充头
        return False
    sep = em.find(b"\x00", 2)
    if sep < 10:                            # em[2:sep] 至少 8 个 FF
        return False
    if em[2:sep] != b"\xff" * (sep - 2):    # 逐字节，不许夹别的
        return False
    expect = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    return hmac.compare_digest(em[sep + 1:], expect)   # ③④ 前缀+摘要，常量时间


def verify_pinned(n, e, sig_b64, message):
    """用 pin 在客户端里的那把公钥验签；没 pin 就 False（绝不「默认通过」）。"""
    if not n or not e:
        return False
    return verify_signature(n, e, sig_b64, message)


# ---------------- 网络（只读）----------------
def fetch_json(url, timeout=8):
    """只读 GET。**不重试、不换源**（契约 §4 第 2 步：失败就退出 2，换源改变不了事实）。

    返回 `(payload, code, message)`：
      成功 → `(dict, None, None)`
      失败 → `(None, 码, 人话)`；服务端错误码（RATE_LIMITED / MANIFEST_BROKEN）照搬，
             本地情况用 `NO_SOURCE`（连不上）/ `BAD_RESPONSE`（不是 JSON）。
    """
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "chat2image-check-update",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        code, msg = "NO_SOURCE", f"服务端返回 {e.code}"
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            ent = (body or {}).get("error")
            if isinstance(ent, dict) and ent.get("code"):
                code, msg = str(ent["code"]), str(ent.get("message") or msg)
        except (ValueError, OSError):
            pass
        return None, code, msg
    except (urllib.error.URLError, OSError) as e:
        return None, "NO_SOURCE", f"连不上更新服务器（{type(e).__name__}）"
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None, "BAD_RESPONSE", "服务端返回的不是 JSON"
    if not isinstance(payload, dict):
        return None, "BAD_RESPONSE", "服务端返回格式不对"
    return payload, None, None


# ---------------- 判定 ----------------
ACTIONS = ("no_release", "up_to_date", "update_available", "manual_reinstall",
           "downgrade_blocked", "manifest_broken")

_REQUIRED = ("version", "sha256", "file", "urls", "sig")


def decide(local_version, local_schema, payload):
    """把「清单 + 本地版本」变成唯一结论。纯函数：不联网、不写盘、不猜。

    返回 `{"action": …, "to": …, "reason": …}`。三条判断**不许退化成同一个结果**
    （代码审查指出：`current 比 latest 新` / `min_supported 太高` / `撤版` 三种
    完全不同的处境，都写成「已是最新」就是骗用户）。
    """
    cur = str(local_version)
    if not isinstance(payload, dict):
        return {"action": "manifest_broken", "to": None, "reason": "清单为空"}
    if payload.get("update_protocol") != UPDATE_PROTOCOL:
        # 协议对不上：不知道对方给的是什么格式，只能让人手动处理
        return {"action": "manual_reinstall", "to": None, "reason": "protocol"}
    latest = payload.get("latest")
    if latest is None:
        return {"action": "no_release", "to": None, "reason": "latest:null"}
    if not isinstance(latest, dict):
        return {"action": "manifest_broken", "to": None, "reason": "latest 不是对象"}
    miss = [k for k in _REQUIRED if not latest.get(k)]
    if miss:
        return {"action": "manifest_broken", "to": None, "reason": "清单缺字段：" + ",".join(miss)}
    to = str(latest.get("version"))
    rel = cmp_version(to, cur)
    if rel is None:
        return {"action": "manifest_broken", "to": to, "reason": "版本号不是 1.2.3 形式"}
    if rel == 0:
        return {"action": "up_to_date", "to": to, "reason": ""}
    if rel < 0:
        return {"action": "downgrade_blocked", "to": to, "reason": "线上版本更旧"}
    # 以下都是「线上确实更新」，区别只在能不能自动升
    try:
        latest_schema = int(latest.get("schema"))
    except (TypeError, ValueError):
        return {"action": "manifest_broken", "to": to, "reason": "schema 不是整数"}
    if latest_schema != int(local_schema):
        return {"action": "manual_reinstall", "to": to, "reason": "schema"}
    min_sup = latest.get("min_supported")
    if min_sup:
        need = cmp_version(min_sup, cur)
        if need is None:
            return {"action": "manifest_broken", "to": to, "reason": "min_supported 格式不对"}
        if need > 0:            # 本地比 min_supported 还旧：v0 不做链式升级
            return {"action": "manual_reinstall", "to": to, "reason": "too_old"}
    if latest.get("breaking"):
        return {"action": "manual_reinstall", "to": to, "reason": "breaking"}
    return {"action": "update_available", "to": to, "reason": ""}


def pick_changes(changelog, version, limit=3):
    """从 `/v1/changelog` 里取这一版的改动（契约 §2：前 3 条）。取不到就给空表。"""
    items = (changelog or {}).get("items")
    if not isinstance(items, list):
        return [], ""
    for it in items:
        if isinstance(it, dict) and str(it.get("version")) == str(version):
            changes = [str(x) for x in (it.get("changes") or []) if str(x).strip()]
            return changes[:limit], str(it.get("summary") or "")
    return [], ""


# ---------------- 升级执行（契约 §4 十步里的机械部分）----------------
import shutil          # noqa: E402
import subprocess      # noqa: E402
import tempfile        # noqa: E402
import zipfile         # noqa: E402

REQUIRED_FILES = ("SKILL.md", "bin/gen.sh", "bin/balance.sh",
                  "scripts/client.py", "version.json")


def config_fingerprint(root):
    """配置目录指纹（契约 §5.1）。返回 'absent' 或 hex。

    只算文件名 + 内容，不算 mtime：doctor 之类读操作会碰 mtime，
    那不算"配置被改"，但内容变了一定算。
    """
    if not os.path.isdir(root):
        return "absent"
    h = hashlib.sha256()
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        h.update(name.encode())
        h.update(b"\0")
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_to(url, dest, timeout=180):
    """下载到系统临时目录（契约 §4 第 4 步：**绝不在 Skill 目录或 config 目录下载**）。"""
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "chat2image-upgrade"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def verify_package(path, latest):
    """先 sha256、再签名（契约 §3「顺序不能反」）。任何一条不过都不能解压。"""
    got = sha256_file(path)
    if got != str(latest.get("sha256") or "").lower():
        return False, "UPDATE_HASH_MISMATCH", f"包哈希不符（清单 {str(latest.get('sha256'))[:12]}…）"
    ok = verify_pinned(PUBKEY_N, PUBKEY_E, latest.get("sig") or "",
                       signed_message(latest.get("version"), latest.get("sha256"),
                                      latest.get("file")))
    if not ok:
        return False, "UPDATE_SIG_INVALID", "签名校验失败"
    return True, "", ""


def safe_extract(zip_path, dest):
    """解压到 dest。拒绝路径穿越与符号链接（包里塞 ../ 是常见投毒手法）。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
                    return False, f"包内含可疑路径：{name}"
                # 符号链接在 zip 里靠外部属性标记；客户端不认，直接拒绝
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    return False, f"包内含符号链接：{name}"
            zf.extractall(dest)
            # 🔴 恢复权限位：`zipfile.extractall` **不还原 Unix 模式**，
            # 打出来的 bin/gen.sh 是 0755，解出来会变成 0644 —— 升级"成功"、doctor 也过，
            # 用户第一次出图就 Permission denied（代码审查 2026-11-21 P1 实测）。
            for info in zf.infolist():
                mode = (info.external_attr >> 16) & 0o777
                target = os.path.join(dest, info.filename)
                if mode and os.path.isfile(target):
                    os.chmod(target, mode)
        before = _exec_missing(dest)
        ensure_exec_bits(dest)
        if before and not _exec_missing(dest):
            return True, ""          # 已修正
        return True, ""
    except (zipfile.BadZipFile, OSError) as e:
        return False, f"解压失败：{type(e).__name__}"


def _exec_missing(root):
    """还没恢复可执行位的脚本（bin/*.sh、install.sh）。"""
    out = []
    for rel in ("install.sh",):
        p = os.path.join(root, rel)
        if os.path.isfile(p) and not os.access(p, os.X_OK):
            out.append(rel)
    bindir = os.path.join(root, "bin")
    if os.path.isdir(bindir):
        for n in sorted(os.listdir(bindir)):
            p = os.path.join(bindir, n)
            if n.endswith(".sh") and os.path.isfile(p) and not os.access(p, os.X_OK):
                out.append(f"bin/{n}")
    return out


def ensure_exec_bits(root):
    """兜底：包内脚本一律 0755。

    为什么要有这一层（而不是只信 zip 里的 external_attr）：有的打包工具/中间层
    会丢模式位；SKILL.md 教 Agent 直接 `bin/gen.sh --prompt`，不是 `bash bin/gen.sh`，
    所以缺了这一位就是"装好了但一用就报错"。
    """
    fixed = []
    for rel in ("install.sh",):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            os.chmod(p, 0o755)
            fixed.append(rel)
    b, = (os.path.join(root, "bin"),) if os.path.isdir(os.path.join(root, "bin")) else (None,)
    if b:
        for n in sorted(os.listdir(b)):
            p = os.path.join(b, n)
            if n.endswith(".sh") and os.path.isfile(p):
                os.chmod(p, 0o755)
                fixed.append(f"bin/{n}")
    return fixed


def find_package_root(extract_dir):
    """包里通常有一层目录（chat2image-x.y.z/）：找出真正带 version.json 的那层。"""
    if os.path.exists(os.path.join(extract_dir, "version.json")):
        return extract_dir
    entries = [e for e in os.listdir(extract_dir)
               if os.path.isdir(os.path.join(extract_dir, e))]
    for e in entries:
        cand = os.path.join(extract_dir, e)
        if os.path.exists(os.path.join(cand, "version.json")):
            return cand
    return extract_dir


def selfcheck(root, expect_version):
    """装前自检（契约 §4 第 6 步）。任一不过都不许替换。"""
    vj = {}
    try:
        with open(os.path.join(root, "version.json"), encoding="utf-8") as f:
            vj = json.load(f)
    except (OSError, ValueError):
        return False, "包内没有可读的 version.json"
    if str(vj.get("version")) != str(expect_version):
        return False, f"包内版本 {vj.get('version')} 与清单 {expect_version} 不一致"
    missing = [p for p in REQUIRED_FILES if not os.path.exists(os.path.join(root, p))]
    if missing:
        return False, "包内缺文件：" + "、".join(missing)
    # 可执行位也要自检：缺了位的包装上去"能装不能用"，而且 doctor 看不出来
    noexec = _exec_missing(root)
    if noexec:
        return False, "包内脚本没有可执行位：" + "、".join(noexec)
    ok, out = run_client(root, ["--version"], timeout=10)
    if not ok:
        return False, "包里的 client.py 跑不起来：" + out[:120]
    return True, ""


def run_client(root, args, timeout=60, env_extra=None):
    """在指定包目录里跑它自己的 client.py（隔离执行，超时即失败）。"""
    script = os.path.join(root, "scripts", "client.py")
    env = dict(os.environ)
    env.update(env_extra or {})
    try:
        p = subprocess.run([sys.executable, script, *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode == 0, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"{type(e).__name__}"


RESTORE_NAME = "chat2image-RESTORE-ME.sh"


# 第 8 步回滚闸只拦这几类：它们意味着「这次装上的包本身有问题」，回滚有意义。
# 其余（服务不可达、价目表读不到、key 无效、目录不可写、备份停用失败）都是环境/账号/
# 网络问题，回滚到旧版一样存在，却会把一份好升级退回去——所以只上报，不回滚。
INSTALL_BLOCKING_CODES = ("VERSION_FILE", "PYTHON_OLD", "NO_BASE_URL", "UPSTREAM_KEY")


def postcheck_fatal(doc_text):
    """装后自检要不要回滚：看 doctor 给的机器码，不看 rc。

    Y-1（2026-11-22 代码审查与 QA 各自复核）：以前 `okdoc` 取 rc，而 `doctor --json`
    恒 rc=0，这道闸等于不存在（`status=broken` 照样放行）。这里按 JSON 判：
    只拦 INSTALL_BLOCKING_CODES；拿不到 JSON（doctor 自己崩了）也当该回滚。
    """
    text = (doc_text or "").strip()
    if not text.startswith("{"):
        return True
    try:
        doc = json.loads(text)
    except ValueError:
        return True
    if not isinstance(doc, dict):
        return True
    codes = doc.get("error_codes")
    if not isinstance(codes, list):
        return doc.get("status") != "ok"      # 没机器码的老包：宁可严一点
    return any(c in INSTALL_BLOCKING_CODES for c in codes)


BAK_MARK = ".c2i-not-a-skill"


def skill_md_name(path):
    """读 `<path>/SKILL.md` frontmatter 里的 `name:`；读不到返回 None。

    平台只认这个字段（不看目录名），所以「哪些目录在冒充同一个 Skill」必须按它识别。
    0.2.4 用 `<NAME>.bak-` 目录名前缀匹配，安装目录被改名成 `skill` 时一份都匹配不上，
    而升级造备份是按实际目录名造的——两边身份证不一致，Y-2（2026-11-22 运维官真机查出）。
    doctor 的 `_skill_registry_dupes` 与这里的 defuse 都必须走这一个函数，不许各写一份。
    """
    sm = os.path.join(path, "SKILL.md")
    if not os.path.isfile(sm):
        return None
    try:
        with open(sm, encoding="utf-8", errors="replace") as f:
            head = f.read(800)
    except OSError:
        return None
    for line in head.splitlines():
        t = line.strip()
        if t.startswith("name:"):
            return t.split(":", 1)[1].strip().strip("'\"")
    return None


def defuse_bak(path):
    """让备份目录不再被平台当成 Skill（P-10，2026-11-22 QA 真机查出）。

    平台认的是 SKILL.md 里的 frontmatter（`name: chat2image`），不看目录名；所以
    `chat2image.bak-0.2.0/` 里那份旧说明会被当成同一个 Skill 解析，模型照着旧指令
    「用 image_path 发本地文件」，图就发不出去——**所有修复对升级过的用户等于没到**。

    做法：只把备份里的 SKILL.md 改名（加这个后缀），目录位置、.env、version.json、
    一键恢复脚本、keep_latest_bak 全部不用动——比把目录搬走小得多，也不会打断
    client._heal_source() 那条「从 .bak 里取回用户原来地址」的自愈路径。
    回滚时由 rollback() 负责把它改回来（见那里的注释），不会还给用户一个没说明的 Skill。
    """
    src = os.path.join(path, "SKILL.md")
    if not os.path.exists(src):
        return False
    try:
        os.replace(src, src + BAK_MARK)
        return True
    except OSError:
        return False


def undefuse_bak(path):
    """回滚前把 SKILL.md 改回原名；没被动过就当成功。"""
    marked = os.path.join(path, "SKILL.md" + BAK_MARK)
    if not os.path.exists(marked):
        return
    try:
        os.replace(marked, os.path.join(path, "SKILL.md"))
    except OSError:
        pass


def defuse_all_baks(parent, skill_name, active_dir=None):
    """把 parent 下所有「声明 name 等于 skill_name、且不是现役目录」的目录去技能化。

    识别按 frontmatter（`skill_md_name`），与 doctor 同源——不再用目录名前缀：
    0.2.4 用 `<skill_name>.bak-` 匹配，安装目录被叫成别的名字时静默失效（Y-2）。
    `active_dir` 是现役目录，绝不碰；不传就不排除（老调用方兼容）。
    返回本次**新**停用的目录名（已停用的不会重复计数，第二遍跑返回空表）。
    """
    active = os.path.realpath(active_dir) if active_dir else None
    done = []
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return done
    for n in names:
        p = os.path.join(parent, n)
        if active and os.path.realpath(p) == active:
            continue
        if not os.path.isdir(p):
            continue
        if skill_md_name(p) != skill_name:
            continue
        if defuse_bak(p):
            done.append(n)
    return done


def write_restore_script(parent, skill_name):
    """在父目录放一个「一键恢复」脚本，**只在两次 rename 之间**存在。

    为什么要它：两次 rename 之间被杀/断电，Skill 目录就没了、只剩 `.bak-*`，
    而安装目录里的任何代码（包括 client.py 自己）都跑不起来——没有任何自动路径能救。
    这条窗口只有相邻两行、极窄，但后果是「用户装不上也修不了」，必须给人留一条路：
    一条 `sh <父目录>/chat2image-RESTORE-ME.sh` 把最新备份改回去。
    """
    path = os.path.join(parent, RESTORE_NAME)
    body = f"""#!/bin/sh
# chat2image 升级被中断时自动生成：把最新备份改回安装目录。
# 只在"上一次升级在改名过程中被打断"时存在；正常升级完成后会自动删除。
set -e
cd "$(dirname "$0")"
skill="./{skill_name}"
if [ -d "$skill" ]; then
  echo "安装目录还在（$skill），不需要恢复。"; exit 0
fi
bak="$(ls -1dt {skill_name}.bak-* 2>/dev/null | head -1)"
if [ -z "$bak" ]; then
  echo "找不到备份（{skill_name}.bak-*），请按包内 INSTALL.md 手动重装。"; exit 1
fi
# P-10：备份里的 SKILL.md 被加了后缀（避免平台把备份当 Skill 加载），恢复前改回原名
if [ -f "$bak/SKILL.md{BAK_MARK}" ]; then
  mv "$bak/SKILL.md{BAK_MARK}" "$bak/SKILL.md"
fi
mv "$bak" "$skill"
echo "已从 $bak 恢复到 $skill。"
rm -f "$0"
"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path
    except OSError:
        return None



def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _env_base(raw) -> str:
    """从 .env 内容里取 BASE_URL（不解析别的键，够用且不引入依赖）。"""
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("BASE_URL="):
            return line.split("=", 1)[1].strip()
    return ""


def install_dotenv(skill_dir, saved, base=""):
    """把 `.env` 在新目录里安顿好（升级换的是整个目录，.env 就在里面）。

    2026-11-22 QA 端到端实测：升级完 `doctor` 报 `BASE_URL 未配置` —— 因为旧目录被
    rename 成 .bak、新目录里没有 .env，客户端等于连不上服务端。用户看到的却是"升级成功"。

    顺序：①旧 .env 原样搬回（最准，用户的地址就是它）②新包自带 .env.example → 用已知 base 生成
         ③调用方给的 base → 直接写一行。
    写完必须能读到非空 BASE_URL，读不到就算失败，交给调用方回滚——不报"成功但用不了"。
    """
    path = os.path.join(skill_dir, ".env")
    data = saved
    if not data:
        data = _read_bytes(os.path.join(skill_dir, ".env.example")) or b""
    if not data:
        if not base:
            return False, "新旧目录都没有 .env，也没有拿到中转站地址"
        data = f"BASE_URL={base}\n".encode()
    if not _env_base(data) and base:
        data = data.rstrip() + f"\nBASE_URL={base}\n".encode()
    try:
        with open(path, "wb") as f:
            f.write(data)
        # 0600（P2-2，2026-11-22）：自愈路径（client._heal_dotenv）写的是 0600，升级路径漏了。
        # 同一个文件两条写入口权限不一致，等于看运气；.env 里将来可能放用户的键，不许组内可读。
        os.chmod(path, 0o600)
    except OSError as e:
        return False, f".env 写不回去（{e.__class__.__name__}）"
    if not _env_base(_read_bytes(path)):
        return False, ".env 里没有有效的 BASE_URL"
    return True, ""


def atomic_replace(skill_dir, new_dir, base=""):
    """① SkillDir → SkillDir.bak-<旧版本>（同父目录 rename）② new_dir → SkillDir。

    两次 rename 之间失败的可能性只来自权限/只读盘；一旦第二步失败，立刻把 bak 改回去，
    保证「要么旧版完好，要么新版可用」，不会出现半装。
    """
    parent = os.path.dirname(os.path.abspath(skill_dir))
    old_version = "unknown"
    try:
        with open(os.path.join(skill_dir, "version.json"), encoding="utf-8") as f:
            old_version = str(json.load(f).get("version") or "unknown")
    except (OSError, ValueError):
        pass
    saved_env = _read_bytes(os.path.join(skill_dir, ".env"))   # 先留证，再做任何 rename
    bak = os.path.join(parent, f"{os.path.basename(skill_dir)}.bak-{old_version}")
    if os.path.exists(bak):
        shutil.rmtree(bak, ignore_errors=True)
    # 先在父目录放好「一键恢复」，再动目录：顺序不能反，否则真被杀就没人知道该怎么办
    write_restore_script(parent, os.path.basename(skill_dir))
    try:
        os.rename(skill_dir, bak)
    except OSError as e:
        return False, None, f"改不动旧目录（{e.__class__.__name__}）：旧版完整未动"
    try:
        os.rename(new_dir, skill_dir)
    except OSError as e:
        os.rename(bak, skill_dir)          # 退回去，宁可「还是旧版」
        _drop_restore_script(parent)
        return False, None, f"新版本就位失败（{e.__class__.__name__}），已退回旧版"

    # 换的是**整个目录**，而 .env（BASE_URL）就住在里面：不搬过去，升完就连不上服务端，
    # 用户看到的却是"升级成功"（2026-11-22 QA 端到端实测）。搬不回来就整体退回旧版——
    # 「还是旧版」永远好过「新版装上了但连不上」。
    okdot, whydot = install_dotenv(skill_dir, saved_env, base)
    if not okdot:
        os.rename(skill_dir, new_dir)      # 新目录退回原处（不删，留给排查）
        os.rename(bak, skill_dir)          # 旧版就位
        _drop_restore_script(parent)
        return False, None, f"升级会让客户端连不上服务端（{whydot}），已退回旧版"

    _drop_restore_script(parent)
    return True, bak, ""


def _drop_restore_script(parent):
    try:
        os.remove(os.path.join(parent, RESTORE_NAME))
    except OSError:
        pass


def rollback(skill_dir, bak):
    """契约 §4.1：新目录改名 .failed-<版本>（不直接删，留证据），再把 bak 改回来。"""
    # 名字要唯一：同一秒内连做两次回滚会撞名，第二次 rename 失败 → 回滚失败，
    # 而这时新目录还在、旧版还在 .bak 里，用户就被卡在"装了一半"的状态（测试抓到过）。
    stamp = int(time.time() * 1000)
    failed = f"{skill_dir}.failed-{stamp}"
    n = 0
    while os.path.exists(failed):
        n += 1
        failed = f"{skill_dir}.failed-{stamp}-{n}"
    try:
        # P-10：备份里的 SKILL.md 被加了后缀（让它不再被平台当成 Skill），回滚要改回原名，
        # 否则用户拿回的是一个打不开的 Skill——那比升级失败更难救。
        undefuse_bak(bak)
        if os.path.exists(skill_dir):
            os.rename(skill_dir, failed)
        os.rename(bak, skill_dir)
        return True, ""
    except OSError as e:
        return False, f"自动回滚失败（{e.__class__.__name__}）：请手动重装"


def keep_latest_bak(parent, keep=1):
    """只保留最近 keep 个 .bak-*（契约 §4 第 10 步），更早的删掉。返回删掉的名字。"""
    try:
        names = [n for n in os.listdir(parent) if ".bak-" in n]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(parent, n)), reverse=True)
    removed = []
    for n in names[keep:]:
        shutil.rmtree(os.path.join(parent, n), ignore_errors=True)
        removed.append(n)
    return removed
