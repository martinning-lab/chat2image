#!/usr/bin/env python3
"""chat2image 客户端（薄客户端，零依赖，只用 Python 标准库）

只做四件事：
  1. 首次运行静默建号（install_id + 指纹 → 换一把属于这台机器的 key）
  2. 提交生图任务并轮询取结果（内部轮询，不要求 Agent 记得回来查）
  3. 下载图片到本地
  4. 打印扣费与余额（把点数翻译成「还能出几张」）

**不含任何上游 key、不含任何价格数字**：
- 上游 key 只在服务端，客户端只有自己的 BASE_URL + 自己的用户 key
- 扣点、单价、开放档位一律读服务端 /v1/pricing，客户端不写死（改价不用发版）

用法（一般由 bin/gen.sh 调）：
  python3 client.py gen --prompt "..." [--resolution 1k|2k|4k] [--aspect 3:4] [--out PATH]
  python3 client.py balance [--json]
  python3 client.py pricing [--json]
  python3 client.py jobs [--limit 20]
  python3 client.py doctor
"""
import argparse
import base64
import hashlib
import qr  # 同目录：纯标准库二维码（见 qr.py）
import upgrade  # 同目录：升级清单/版本比较/验签（只读，见 upgrade.py）
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.2.8"
NAME = "chat2image"
# 本文件在 <包>/scripts/ 下，包根是上一级。.env、version.json 只从包根读。
# （代码审查 P0-1：以前指向 scripts/，开发机跑得通、装到用户机上必然报 NO_BASE_URL）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_DIR = os.path.expanduser(os.environ.get("C2I_CONFIG_DIR", "~/.config/chat2image"))
IDENTITY_FILE = os.path.join(CONFIG_DIR, "identity.json")
CRED_FILE = os.path.join(CONFIG_DIR, "credentials.json")
PENDING_FILE = os.path.join(CONFIG_DIR, "pending.json")
# V1-06 欢迎海报（契约 v1.1 §5）：只记一个布尔，展示过永不再展示；服务端不存每用户状态
WELCOME_FILE = os.path.join(CONFIG_DIR, "welcome.json")
CACHE_DIR = os.environ.get("C2I_CACHE_DIR", os.path.expanduser("~/.cache/chat2image"))

# 自愈失败时给用户的一句人话（UI 2026-11-22 三条）：人类可读的 message 里不出现
# BASE_URL / .env / traceback；机器可读的 error 码仍是 NO_BASE_URL 供排查。
# 最后半句必须留着——「账号和余额在服务器上」是用户敢按这个动作做的唯一理由。
NO_BASE_SAY = ("连不上服务，可能是安装不完整。先按原路径重装一次就好。"
               "重装后会尽量挂回原来的账号，余额在服务器上，不会因为重装而消失。"
               "如果装完发现是新账号、没有余额，把注册邮箱和任意一笔订单号发我，我帮你查。")

DEFAULT_TIMEOUT = 300          # 单张最长等 5 分钟（上游最慢约 4 分钟）
PENDING_WINDOW = 120           # 同参数 120 秒内直接复用上次任务，防连点


# ---------------- 输出 ----------------
def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def die(code, message, **extra):
    out({"success": False, "error": code, "message": message, **extra})
    sys.exit(1)


# ---------------- 配置 ----------------
def _read_dotenv(path):
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_env():
    """配置取值顺序（代码审查 P0-2）：环境变量 → ~/.config 凭据 → 包根 .env。

    为什么要读 ~/.config：删掉 Skill 目录重装、或换个路径装，包里的 .env 就没了，
    但账号还在服务端。key 只认一处真源（credentials.json），.env 只放 BASE_URL，
    避免「两份 key 不一致」这种最难查的问题。
    """
    dotenv = _read_dotenv(os.path.join(SKILL_DIR, ".env"))
    cred = read_json(CRED_FILE, {})
    base = (os.environ.get("C2I_BASE_URL")
            or cred.get("base_url")
            or dotenv.get("BASE_URL") or "")
    key = (os.environ.get("C2I_API_KEY")
           or cred.get("api_key")
           or dotenv.get("C2I_API_KEY") or "")
    if not base:
        # 一条路都取不到地址时才试着自愈（0.2.3）：升级把包根 .env 换掉的情形见 _heal_dotenv
        base = _heal_dotenv()
    return base.rstrip("/"), key


def _heal_source():
    """自愈取值来源：先最近一份 `<包名>.bak-*/.env`，再退到包根 `.env.example`。

    为什么 .bak 优先：升级把旧目录改名成 `<包名>.bak-<旧版本>`，里面那份 .env 是**用户原来
    的地址**。从 .bak 恢复是「把原来的东西放回去」；从 .env.example 恢复是「替他重新选一个
    地址」——用户把 BASE_URL 指向过测试实例时，后者会把他静默改回生产域。
    """
    parent = os.path.dirname(SKILL_DIR.rstrip(os.sep))
    prefix = os.path.basename(SKILL_DIR) + ".bak-"
    cands = []
    try:
        for name in os.listdir(parent):
            if not name.startswith(prefix):
                continue
            p = os.path.join(parent, name, ".env")
            try:
                cands.append((os.path.getmtime(p), p))
            except OSError:
                continue
    except OSError:
        pass
    for _, p in sorted(cands, reverse=True):
        base = (_read_dotenv(p).get("BASE_URL") or "").strip()
        if base:
            return base
    example = _read_dotenv(os.path.join(SKILL_DIR, ".env.example"))
    return (example.get("BASE_URL") or "").strip()


def _heal_dotenv():
    """包根 .env 丢了或没地址时，就地重建，返回重建出的 BASE_URL（0.2.3）。

    为什么放在「启动取值」这一层，而不是放在升级里：
    升级是**旧版本**的 upgrade.py 在跑，写在新版本里的升级保护管不到装它的那一次升级
    （0.2.1、0.2.2 两次都是这么坏的：--json 报 upgraded，接着 balance 报 NO_BASE_URL）。
    放在这里，已经被升级弄坏的机器跑第二条命令就恢复，不依赖下一次升级。

    三条不许破的边界：
    · 触发条件由调用方判定：环境变量、credentials.json、.env 三处合起来都取不到地址才进来。
      「.env 不在」不等于「base 为空」，有凭据的机器本来就能用，不该被多写一份文件。
    · 已有非空 BASE_URL（自建/自改）一个字都不改——被改回内置地址，请求会静默发到别人服务器。
    · 失败一律静默返回空串，维持原来那句 NO_BASE_URL 人话，不许新增 traceback。
    """
    path = os.path.join(SKILL_DIR, ".env")
    lines = []
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        s = line.strip()
        if s.startswith("BASE_URL=") and s.split("=", 1)[1].strip():
            return ""                              # 已有地址：不碰
    base = _heal_source()
    if not base:
        return ""
    tmp = path + ".heal.tmp"
    try:
        kept, replaced = [], False
        for line in lines:
            if line.strip().startswith("BASE_URL="):
                kept.append("BASE_URL=" + base)    # 空值就地补，不追加第二行
                replaced = True
            else:
                kept.append(line)
        if not replaced:
            if not kept:
                kept.append("# chat2image 客户端配置（升级丢了 .env 时自动重建）")
            kept.append("BASE_URL=" + base)
        os.makedirs(SKILL_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return base
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return ""


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------- 设备身份 ----------------
def _parse_machine_guid(raw: str) -> str:
    """从 `reg query ... /v MachineGuid` 的输出里取 GUID。输出形如：
        HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Cryptography
            MachineGuid    REG_SZ    aaaaaaaa-bbbb-...
    """
    for line in (raw or "").splitlines():
        if "MachineGuid" in line and "REG_" in line:
            parts = line.split()
            if len(parts) >= 3 and parts[-1].strip():
                return parts[-1].strip()
    return ""


_MACHINE_GUID_SUBKEY = r"SOFTWARE\Microsoft\Cryptography"


def _winreg_machine_guid() -> str:
    """winreg 直读 MachineGuid（首选）。

    为什么首选这条：`reg query` 要 spawn 一个子进程，在某些安全策略/受限执行环境里会被
    拦下；winreg 是 Python 标准库里的注册表 API，不起子进程。2026-11-23 在一台
    Windows 10（19045）上用 WorkBuddy 自带的 Python 3.13 实测：winreg 直读能拿到
    MachineGuid。非 Windows 上根本没有这个模块，直接返回空串交给下一级。
    64 位系统上 32 位进程会被 WOW64 重定向，所以两个视图都试。
    """
    try:
        import winreg
    except Exception:
        return ""
    for view in (getattr(winreg, "KEY_WOW64_64KEY", 0),
                 getattr(winreg, "KEY_WOW64_32KEY", 0)):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MACHINE_GUID_SUBKEY, 0,
                                winreg.KEY_READ | view) as k:
                val = str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
        except Exception:
            continue
        if val:
            return val
    return ""


def _reg_query_machine_guid() -> str:
    """命令行兜底：只在 winreg 不可用/被拦时才走（0.2.7 之前就只有这一条路）。"""
    for key in (r"HKLM\SOFTWARE\Microsoft\Cryptography",
                r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Cryptography"):
        try:
            raw = subprocess.run(["reg", "query", key, "/v", "MachineGuid"],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        guid = _parse_machine_guid(raw)
        if guid:
            return guid
    return ""


def _win_machine_guid() -> str:
    """注册表 MachineGuid（契约 v1.27「一台设备一次体验额度」那节）。

    顺序是「依赖越少越先试」：① winreg 直读（不起子进程）→ ② reg query（子进程，
    可能被安全策略拦）。③ wmic UUID 在 hw_uuid() 里，只作最外层兜底。

    为什么不是 wmic：新版 Windows 已移除 wmic，取不到就是空串——而空串在设备闸里
    等于「这台机器是新的」。MachineGuid 卸载重装不变，也不受 wmic 移除影响。
    """
    return _winreg_machine_guid() or _reg_query_machine_guid()


def hw_uuid():
    """硬件 UUID：只用来做指纹，不单独作为身份"""
    try:
        if platform.system() == "Darwin":
            raw = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in raw.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        elif platform.system() == "Linux":
            with open("/etc/machine-id", encoding="utf-8") as f:
                return f.read().strip()
        elif platform.system() == "Windows":
            guid = _win_machine_guid()
            if guid:
                return guid
            try:                      # wmic 只作兜底（新版 Windows 已经没有了）
                raw = subprocess.run(["wmic", "csproduct", "get", "UUID"],
                                     capture_output=True, text=True, timeout=5).stdout
                parts = [x.strip() for x in raw.splitlines() if x.strip()]
                if len(parts) > 1 and parts[1]:
                    return parts[1]
            except Exception:
                pass
            # 契约：三种方式都失败不许悄无声息。stdout 只留给 JSON，
            # WARN 走 stderr；服务端那边还会记一条 abuse_events(no_fingerprint)。
            # 2026-11-23：不再提「联系客服」——本版没有可用的人工通道（SKILL.md 也明令禁止）。
            sys.stderr.write("WARN 取不到机器指纹：注册表直读、命令行查询与硬件 UUID 都失败，"
                             "本机本次不会发体验额度；可以直接付费充值使用\n")
    except Exception:
        pass
    return ""


def identity():
    """身份文件放在 ~/.config/chat2image/，不在 Skill 目录里。

    → 删掉 Skill 目录重装、换个路径装，身份都还在，服务端认得出是同一台机器。
    """
    data = read_json(IDENTITY_FILE, {})
    if not data.get("install_id"):
        import uuid
        data = {
            "install_id": str(uuid.uuid4()),
            "schema": 1,
            "created_at": int(time.time()),
        }
        write_json(IDENTITY_FILE, data)
    hw = hw_uuid()
    data["fingerprint"] = hashlib.sha256(
        (os.environ.get("C2I_FP_SALT", "c2i") + hw + platform.system()).encode()
    ).hexdigest()[:32] if hw else ""
    return data


# ---------------- HTTP ----------------
RETRYABLE_CODES = {"RATE_LIMITED", "SERVICE_STOPPED", "UPSTREAM_UNAVAILABLE", "NETWORK", "INTERNAL"}


def _once(method, url, body, key, timeout, raw, headers, raw_body=None, raw_ctype=None):
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", raw_ctype or "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            if raw:
                return r.status, payload
            return r.status, json.loads(payload.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            return e.code, {"raw": payload.decode("utf-8", "replace")[:300]}
    except urllib.error.URLError as e:
        return -1, {"error": {"code": "NETWORK", "message": str(e.reason), "retryable": True}}


def request(method, url, body=None, key=None, timeout=30, raw=False, headers=None, retries=2,
            raw_body=None, raw_ctype=None):
    """带退避重试的请求。

    只对**服务端明确说 retryable 的**错误重试（429/503/网络抖动），退避 1s → 3s，
    最多 2 次（总等 4 秒）。其它错误立刻返回——不能对 402/401 这种做重试，
    那会让用户等 4 秒才看到「余额不足」。
    raw_body/raw_ctype：multipart 上传用（参考图），不走 JSON 编码。
    """
    delay = 1.0
    for attempt in range(retries + 1):
        status, payload = _once(method, url, body, key, timeout, raw, headers,
                                raw_body=raw_body, raw_ctype=raw_ctype)
        if raw or attempt >= retries:
            return status, payload
        code = ""
        if isinstance(payload, dict):
            e = payload.get("error")
            code = e.get("code") if isinstance(e, dict) else ""
            if not (isinstance(e, dict) and e.get("retryable")):
                if code not in RETRYABLE_CODES and status not in (429, 503, -1):
                    return status, payload
        if status not in (429, 503, -1) and code not in RETRYABLE_CODES:
            return status, payload
        time.sleep(delay)
        delay *= 3
    return status, payload


def err_code(resp):
    e = (resp or {}).get("error")
    if isinstance(e, dict):
        return e.get("code") or "UNKNOWN", e.get("message") or ""
    return "UNKNOWN", str(resp.get("message") or e or "")


def err_reason(resp):
    """错误体里的机器码（目前只有 CREDITS_FROZEN 带，契约 §error.reason）。

    取不到就返回 None —— 不要从 message 的中文括号里正则抠：
    那等于让客户端依赖服务端文案，改一个字的文案就静默失效。
    """
    e = (resp or {}).get("error")
    if isinstance(e, dict):
        r = e.get("reason")
        return str(r) if r else None
    return None


# 402 有两种完全不同的原因，给用户的下一步也完全不同：
# 欠费 → 给出差额并引导充值；被风控冻结 → 明说「不是欠费、不用充值」，
# 否则用户会去付钱，钱付了还是出不了图（这是真实冤枉钱，2026-11-20 抓出）。
# 整句话在客户端算好放进 `say`，Agent 只负责原样转述：只给三个数字，
# Agent 不一定把它们组成一句话，那就成了「能力齐了、用户还是听不到」。
_FROZEN_SAY = {
    "credits_frozen": ("这次没出成，也没扣你点数。不是余额问题，不用充值——"
                       "免费体验额度被临时限制了；把注册邮箱和任意一笔订单号发给对接你的人，核实后恢复。"),
    "banned": ("账号已被限制使用，这次没出成、也没扣点数，不用充值。"
               "有疑问请把注册邮箱和任意一笔订单号发给对接你的人。"),
    "fail_streak": ("这张连续失败了几次，系统自动停了一下做保护，没扣你点数、也不用充值。"
                    "过一会儿再试；一直不行就把注册邮箱和任意一笔订单号发给对接你的人。"),
    "user_budget": ("今天这个账号的费用到上限了（防刷保护），没扣你点数，也不用充值。"
                    "明天再试，或找对接你的人放开上限。"),
    "no_user": ("账号信息没对上，这次没出成、也没扣点，也不用充值。"
                "请把注册邮箱和任意一笔订单号发给对接你的人核对。"),
}
# 未知/新增 reason 一律落这句：契约承诺「取值只增不改」，新增取值不得改变客户端行为。
# 硬要求：不许空白、不许显示机器码、**不许回退到「请充值」**。
_NEUTRAL_SAY = "这次没出成，没扣你点数。稍后再试；一直不行就找对接你的人。"
# 欠费但余额没查到时用这句：**绝不填 0**。填 0 会把「真实有 2 点」的用户
# 说成「你有 0 点、还差 3 点」，于是一次本该成功的充值变成了多付钱。
_BALANCE_UNKNOWN_SAY = ("这次没出成、也没扣你点数，而且我这边没查到你的余额，"
                        "所以不报具体数字。在对话里问我一句「余额」，看到数字再决定。")
_FROZEN_NEXT = "把注册邮箱和任意一笔订单号发给对接你的人核实"


def paywall_say(code, reason, required, available, balance_known=True, cashier=None):
    """402 的话术与结构化字段。返回 (say, fields)。

    `cashier` = (收银台链接, 有效分钟数) 或 None（V1-07，老马 2026-11-22 定）：
    余额不足一律给网页收银台，让用户自己选点数；**不再按缺口建单**。
    

    判据是**白名单**：只有明确是 `INSUFFICIENT_CREDITS` 才引导充值。
    写成 `if code == "CREDITS_FROZEN": … else: 欠费话术` 是黑名单——
    将来服务端多一个 402 出口，用户立刻被引去付钱、付了还出不了图。
    契约只承诺了 `reason` 只增不改，**没有承诺 402 的 code 集合只有两种**。
    """
    if code != "INSUFFICIENT_CREDITS":
        if code == "CREDITS_FROZEN":
            say = _FROZEN_SAY.get(reason or "") or _NEUTRAL_SAY
        else:
            say = _NEUTRAL_SAY          # 未知 402 码：不猜、不引导付款
        return say, {"required": None,
                     "available": int(available) if balance_known else None,
                     "shortfall": None}
    required = int(required or 0)
    if not balance_known:
        # 余额查不到：不编数字，交给用户自己查
        return _BALANCE_UNKNOWN_SAY, {"required": required, "available": None, "shortfall": None}
    available = int(available or 0)
    # shortfall 只作结构化字段给管理员看，**不进对客话术**：老马 2026-11-22 定
    # 「余额不足一律弹收银台，不再按缺口生成付款码」。话术里说「还差 N 点」等于
    # 又在按缺口算钱，用户照着付 1 块、图还是出不了（这是他真机撞到的那一单）。
    shortfall = max(0, required - available)
    if available >= required:
        # 余额看起来够却仍被 402 拒绝（并发占点/风控/上游都可能导致）：**不许跟着说「不够」**。
        # 老判据里那句「还差 0 点」就是这种自相矛盾，V1-07 改口径时一起修掉。
        return ("这次没出成，但我查到的余额是够的"
                f"（要 {required} 点、现有 {available} 点）——可能刚才那单还占着点数。"
                "回我一句「余额」看下当前点数，或直接说「重试」。",
                {"required": required, "available": available, "shortfall": shortfall})
    if cashier:
        url, mins = cashier
        valid = f"（约 {mins} 分钟内有效）" if mins else ""
        say = (f"这张图要 {required} 点，你现在有 {available} 点，不够。"
               f"打开这个网页收银台，点数自己选：{url}{valid}。"
               "电脑、手机都能用；选好后页面会出二维码，用**手机支付宝**扫。"
               "付完回我一句「继续」，我把刚才这张接着出。")
    else:
        # 降级：收银台链接没取到。**不编金额、不退回按缺口建单**，只给一句问法。
        say = (f"这张图要 {required} 点，你现在有 {available} 点，不够。"
               "回我一句「充值」，我给你网页收银台的链接，进去自己选点数。"
               "付完回我一句「继续」，我把刚才这张接着出。")
    return say, {"required": required, "available": available, "shortfall": shortfall}


_CASHIER_NEXT = ("把 say 原样发给用户（里面已经有收银台链接），不要自己算差额、不要按缺口建单；"
                "用户付完说「继续」再出同一张")


def _cashier(base, key, timeout=10):
    """取网页收银台链接：(链接, 有效分钟数) 或 None。**错误路径专用**（V1-07）。

    为什么放在这里：402 是「用户已经要了图、被钱挡住」的那一刻，多等一次网络就是
    让「余额不足」变成「卡住不说话」。所以 retries=0、超时 10 秒、任何异常都返回 None，
    让调用方降级成「说一句充值我给链接」——宁可不给链接，也不编金额、更不退回按缺口建单。
    """
    if not base:
        return None
    try:
        st, r = request("POST", f"{base}/v1/topup/session", {}, key=key,
                        timeout=timeout, retries=0)
    except Exception:                                   # noqa: BLE001
        return None
    if st != 200:
        return None
    sess = (r or {}).get("session") or {}
    url = sess.get("cashier_url") or ""
    if not url:
        return None
    exp = int(sess.get("exp") or 0)
    mins = max(1, (exp - int(time.time())) // 60) if exp else 0
    return url, mins


def ensure_key(base):
    """没有 key 就静默建号：用户不需要注册、不需要填任何东西"""
    if not base:
        # 没有服务地址时拿 "/v1/install" 去建请求会崩成 Python traceback（ValueError:
        # unknown url type），用户看到一屏栈、不知道该干什么（2026-11-22 QA 在升级后撞到）。
        # 这里必须给一句人话，并且**不许显示 0 点**——余额没查到≠余额是 0。
        die("NO_BASE_URL", NO_BASE_SAY)
    _, key = load_env()
    if key:
        return key
    ident = identity()
    st, r = request("POST", f"{base}/v1/install",
                    {"install_id": ident["install_id"], "fingerprint": ident.get("fingerprint") or ""})
    if st != 200:
        code, msg = err_code(r)
        # 这里曾经写 `code=code`，和 die(code, message) 的第一个位置参数撞名，
        # 断网时用户第一句「画一张」就看到 Python traceback（代码审查实测）。
        die("ENROLL_FAILED", f"建号失败：{msg}", http=st, upstream_code=code)
    new_key = r.get("api_key")
    if not new_key:
        # 服务端认得这台机器、但这次没给钥匙（契约 v1.32 §3.7(h)-4）。
        # 🔴 只有服务端明确 `recovery_recorded === true` 才说「已经记下了」：
        #    老服务端、自建后端、CDN 缓存旧响应、或被 IP 限频压掉时，这个字段不存在——
        #    把「缺字段」当成已登记，就是在说一句假话（说做过了、其实没做）。
        # 🔴 不提「客服」「管理员」「订单号」：用户找不到我们，那句话等于死路。
        if r.get("recovery_recorded") is True:
            die("KEY_REQUIRED",
                "已经记下了，这台机器有记录。晚点重开一次对话试试。不要去改 .env。")
        die("KEY_REQUIRED",
            "这台机器的凭证丢了。晚点重开一次对话试试。不要去改 .env。")
    base2, _ = load_env()
    # key 只存一处真源；.env 保持只有 BASE_URL，不再写第二份（否则换目录就漂移）
    write_json(CRED_FILE, {"base_url": base2, "api_key": new_key,
                          "user_id": r.get("user_id"), "created_at": int(time.time())})
    return new_key


# ---------------- 业务命令 ----------------
def _make_qr(text):
    """把付款链接渲染成二维码 PNG，返回路径；失败返回 None（不阻断取码）。"""
    try:
        m = qr.matrix(text)
        path = os.path.join(CONFIG_DIR, "pay-qr.png")
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(qr.png_bytes(m, scale=8, border=4))
        os.chmod(path, 0o600)   # 付款码谁拿到谁都能替人付
        return path
    except Exception:
        return None


def cmd_recharge(args):
    """取付款方式：优先走 v1 /v1/topup/order（服务端算价 + 二维码 + 收银台链接）。

    为什么需要这个命令：402 话术一直承诺「回我一句『充值』我给你付款链接」，
    但客户端根本没有取链接的命令——2026-11-20 真机装完第一张图后卡在这里。
    v1 起建单统一走 /v1/topup/order：金额、折扣、上限全由服务端算，客户端只展示。
    """
    base, _ = load_env()
    key = ensure_key(base)
    if (getattr(args, "channel", "alipay") or "alipay") != "alipay":
        die("CHANNEL_UNSUPPORTED", "V1 只支持支付宝当面付（--channel alipay）")
    pkg = getattr(args, "package", None)
    points = getattr(args, "points", None)
    if pkg is None and points is None:
        # 主路径（老马 V1 ④）：给一个网页收银台，用户在页面里选包/输点数
        st, r = request("POST", f"{base}/v1/topup/session", {}, key=key, timeout=30, retries=0)
        if st != 200:
            code, msg = err_code(r)
            die(code or "SESSION_FAILED", msg or f"取收银台链接失败（HTTP {st}）", http=st)
        sess = r.get("session") or {}
        url = sess.get("cashier_url") or ""
        exp = int(sess.get("exp") or 0)
        mins = max(0, exp - int(time.time())) // 60 if exp else 0
        out({"success": True, "cashier_url": url, "session_exp": exp,
             "say": (f"打开这个网页收银台选点数：{url}（约 {mins} 分钟内有效）。"
                     "电脑、手机都能用；选好后页面会出二维码，用**手机支付宝扫**。"
                     "付完回我一句「继续」就行。")})
        return
    if pkg is None:
        points = int(points)
        if points <= 0:
            die("BAD_REQUEST", "点数必须大于 0")
        # 余额够用就别让人白付钱（付费不可逆，宁可多问一句）
        st, r = request("GET", f"{base}/v1/balance", key=key)
        if st == 200:
            avail = int(((r.get("balance") or {}).get("available")) or 0)
            if avail >= points and not args.force:
                die("NO_NEED", f"当前可用 {avail} 点，已经够出图，不需要充值"
                               "（确认要充就加 --force）")
        body = {"points": points}
    else:
        body = {"package_id": int(pkg)}

    # retries=0：建单请求重试一次就可能开出两张单。幂等键是服务端第二道防护
    # （同 key 同参数返回首次结果），但客户端仍不重试——两道都要有。
    idem = "recharge-" + os.urandom(8).hex()
    st, r = request("POST", f"{base}/v1/topup/order", body, key=key, timeout=30,
                    retries=0, headers={"Idempotency-Key": idem})
    if st != 200:
        code, msg = err_code(r)
        die(code or "RECHARGE_FAILED",
            msg or f"取付款方式失败（HTTP {st}），这次没有扣点", http=st)

    order = r.get("order") or {}
    pay_url = order.get("pay_url") or ""
    amount = order.get("amount")
    points = order.get("points") or body.get("points")
    # 是否 dry 问服务端，不看 URL 里是否恰好有 "DRY"
    _sh, _hr = request("GET", f"{base}/v1/health")
    dry = (_hr or {}).get("pay_mode") == "dry" if _sh == 200 else ("DRY" in pay_url)
    if not pay_url:
        die("NO_PAY_URL", "这次没取到付款方式（订单已建，未扣点）。"
                          "请把这句话转告对接人：订单号 "
                          f"{order.get('order_no') or '未知'}，需要人工处理")

    # 优先用服务端二维码（契约 §1.5.3 qr_png_b64）；解码失败才退回本地编码
    qr_path = None
    b64 = order.get("qr_png_b64") or ""
    if b64:
        try:
            raw = base64.b64decode(b64)
            qr_path = os.path.join(CONFIG_DIR, "pay-qr.png")
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(qr_path, "wb") as f:
                f.write(raw)
            os.chmod(qr_path, 0o600)   # 付款码谁拿到谁都能替人付
        except Exception:
            qr_path = None
    if not qr_path:
        qr_path = _make_qr(pay_url)

    # 有效期从 expires_at 算，不写死分钟数（服务端 order_timeout_seconds 可配）
    exp = int(order.get("expires_at") or 0)
    mins = max(0, exp - int(time.time())) // 60 if exp else 0
    cashier = order.get("cashier_url") or ""
    if qr_path:
        say = (f"充值 {points} 点需付 ¥{amount}（约 {mins} 分钟内有效）。"
               f"二维码图在这里：{qr_path} —— 用**手机支付宝扫这张图**，"
               "不要在电脑浏览器里打开链接，那样打不开付款页。"
               + (f"也可以手机打开：{cashier}。" if cashier else "")
               + "付完回我一句「继续」就行。")
    else:
        say = (f"充值 {points} 点需付 ¥{amount}（约 {mins} 分钟内有效）。"
               f"付款码链接：{pay_url} —— 在**手机上**打开或转成二维码用手机支付宝扫，"
               "电脑浏览器打开打不开付款页。付完回我一句「继续」就行。")
    if dry:
        say = (f"【测试模式 dry】充值 {points} 点（¥{amount}）不会真扣钱，"
               f"链接只用于走通流程：{pay_url}。要真付钱请让管理员把支付切到 live。")
    out({"success": True, "order": order, "pay_url": pay_url,
         "qr_path": qr_path,     # 二维码图（PNG），Agent 直接把这张给用户
         "cashier_url": cashier,
         "qr_ascii": (qr.ascii_art(qr.matrix(pay_url)) if (getattr(args, "ascii", False) and qr_path) else None),
         "points": points, "amount": amount,
         "expires_at": order.get("expires_at"),
         "say": say})           # ← 整句给 Agent 原样转述，别让它自己拼

def cmd_pricing(args):
    base, _ = load_env()
    st, r = request("GET", f"{base}/v1/pricing")
    if st != 200:
        die("PRICING_FAILED", "取价目表失败", http=st)
    out({"success": True, **r})


def _account_state(base, key):
    """读账号状态，只认服务端字段。
    返回 (available|None, has_recharged|None)；取不到就是 None —— 契约 v1.22 §1.7 G3：
    读不到按未充值保守处理并在话术里明示，**绝不拿余额或订单自己推断是否充值过**。
    """
    st, r = request("GET", f"{base}/v1/balance", key=key)
    if st != 200:
        return None, None
    bal = r.get("balance") or {}
    hr = r.get("has_recharged")
    return int(bal.get("available") or 0), (hr if isinstance(hr, bool) else None)


def pick_default_resolution(pricing: dict, has_recharged, available):
    """契约 v1.22 §1.7.1 默认档 + §1.7.4「降无可降」。纯函数，便于单测。

    source：
      account       按账号状态选到档（含 G1/G2 静默降级——只报最终档，不提被降过程）
      conservative  G3 读不到账号状态，按未充值给 1K，话术必须明示
      insufficient  1K 在售但余额不够 → 仍给 1K，扣点阶段正常报欠费（§1.7.1 表）
      ask           G6 情况A：1K 不在售但更高档可售可买 → 零生成请求，让用户选
      none          G6 情况B：一个可用档都没有 → NO_USABLE_RESOLUTION
    """
    pts = pricing.get("points") or {}
    # 与 gen 里同一口径（QA D2）：「缺 open 字段」按老服务端兜底 1K，「open 为空数组」才是全下架。
    # 两处不一致会出现同一份响应里 open:["1k"] 却告诉用户「暂无可用的档位」——自己打自己。
    open_list = pricing["open"] if "open" in pricing else ["1k"]

    def need(c):
        return int(pts.get(c) or 0)

    if available is None or has_recharged is None:
        # G3 兜底也要先看 1K 在不在售（UI 抓的死角）：读不到账号状态 ≠ 可以无视档位下架。
        # 1K 不在售还硬给 1K，用户一跑就撞 RESOLUTION_CLOSED——又一种「默认值自己制造错误」。
        if "1k" not in open_list:
            ups = [c for c in ("2k", "4k") if c in open_list]
            if ups:
                return {"resolution": None, "source": "ask", "options": ups, "reason": "1k_closed",
                        "say": "目前 1K 档暂不开放。" + "可选："
                               + "、".join(f"{c.upper()}（{need(c)} 点）" for c in ups)
                               + "（账号状态暂时没查到，选完我按你选的档提交）"}
            return {"resolution": None, "source": "none", "options": [], "reason": "all_closed",
                    "say": "目前暂无可用的清晰度档位，稍后再试"}
        return {"resolution": "1k", "source": "conservative", "options": [], "reason": "balance_unavailable",
                "say": "本次默认 1K（刚才没查到你的账号状态）"}

    def say_for(chosen):
        """「可改 X/Y」只能列**当前在售**的其它档——4K 关着还说可改 4K，就是把用户引去撞
        RESOLUTION_CLOSED。档位表本身也可能只开了 1K，所以这里必须按 open 过滤。"""
        others = [c for c in ("1k", "2k", "4k") if c in open_list and c != chosen]
        return (f"默认 {chosen.upper()}（可改 {'/'.join(x.upper() for x in others)}）"
                if others else f"默认 {chosen.upper()}")

    chain = ["2k", "1k"] if has_recharged else ["1k"]
    for c in chain:
        if c in open_list and available >= need(c):
            return {"resolution": c, "source": "account", "options": [], "reason": None,
                    "say": say_for(c)}
    # 1K 在售、只是钱不够：给 1K，让扣点阶段按老规矩报欠费（不在这里造第二个错误码）
    if "1k" in open_list:
        return {"resolution": "1k", "source": "insufficient", "options": [], "reason": "balance_low",
                "say": say_for("1k")}
    # §1.7.4：1K 不在售，比它高的都更贵 —— 静默升档 = 静默涨价，宁可不画也不能自动升
    ups = [c for c in ("2k", "4k") if c in open_list and available >= need(c)]
    if ups:
        return {"resolution": None, "source": "ask", "options": ups, "reason": "1k_closed",
                "say": "目前 1K 档暂不开放。" + "可选："
                       + "、".join(f"{c.upper()}（{need(c)} 点）" for c in ups)}
    if open_list:
        cheapest = min(open_list, key=need)
        return {"resolution": None, "source": "none", "options": list(open_list),
                "reason": "balance_insufficient",
                "say": f"当前余额不够出图（最低 {cheapest.upper()} 要 {need(cheapest)} 点）"}
    return {"resolution": None, "source": "none", "options": [], "reason": "all_closed",
            "say": "目前暂无可用的清晰度档位，稍后再试"}


def cmd_default_res(args):
    """用户没指定清晰度时，问一次「这一单该用哪个档」。恒为 JSON。"""
    base, _ = load_env()
    key = ensure_key(base)
    st, pr = request("GET", f"{base}/v1/pricing")
    if st != 200:
        die("PRICING_FAILED", "取价目表失败，可能服务不可用", http=st)
    pricing = pr.get("pricing") or {}
    # 与 pick_default_resolution 同口径：缺 open 字段=老服务端，按 ["1k"] 兜底（不许在这里另写一份）
    open_list = pricing["open"] if "open" in pricing else ["1k"]
    available, has_recharged = _account_state(base, key)
    d = pick_default_resolution(pricing, has_recharged, available)
    pts = pricing.get("points") or {}
    # 在售档全貌（含余额够不够）：追问模板要能照 UI 规格写「4K 本次 4 点（余额 3 点不够）」，
    # 但数字必须来自服务端价目 + 余额，不许 Agent 自己算
    detail = [{"resolution": c,
               "points": int(pts.get(c) or 0),
               # 三态：true 够 / false 不够 / null 没查到——null 不许被当成「不够」播报
               "affordable": (None if available is None else available >= int(pts.get(c) or 0))}
              for c in (pricing.get("open") or [])]
    out({"success": True,
         "resolution": d["resolution"],
         "options_detail": detail,
         "points": int(pts.get(d["resolution"]) or 0) if d["resolution"] else None,
         "source": d["source"],
         "reason": d["reason"],
         "options": d["options"],
         "say": d["say"],
         "needs_user_choice": d["resolution"] is None and d["source"] in ("ask", "none"),
         "has_recharged": has_recharged,
         "balance_available": available,
         "open": open_list,   # 与 resolution 同源：缺字段时是 ["1k"]，不许这里再写一份 []
         "degraded_open_default": "open" not in pricing,
         "pricing_version": pricing.get("version")})


def _welcome_mirror() -> dict:
    """本地只当**镜像缓存**，不是判据（契约 v1.10 §5.6.7）。

    真源在服务端 users.welcomed_at：本地说 welcomed 只是省一次请求；
    本地说没有、服务端说 already_welcomed 时以服务端为准（会把镜像补写回来）。
    旧版镜像字段是 shown:true —— 读的时候兼容一下，免得老用户升级后重弹一次。
    """
    st = read_json(WELCOME_FILE, {})
    if st.get("welcomed") is True or st.get("shown") is True:
        return {"welcomed": True, "welcomed_at": int(st.get("welcomed_at") or 0)}
    return {"welcomed": False, "welcomed_at": 0}


def _welcome_write_mirror(welcomed_at: int):
    """只有在「服务端确认已欢迎」或「ack 成功」之后才写；失败路径永远不写。"""
    write_json(WELCOME_FILE, {"welcomed": True, "welcomed_at": int(welcomed_at or 0),
                              "synced_at": int(time.time())})


def _welcome_skip(reason: str):
    """静默降级：出图流程不受影响，不写标记，也不给用户看任何错误。"""
    out({"success": True, "action": "skip", "reason": reason})
    return 0


def cmd_welcome(args):
    """契约 v1.12 §5.7.7：判定读服务端；交付形态由服务端决定（开页面 / 老式发图）。

    动作：
      open_page  —— 页面模式（0.2.8 起默认）：把服务端下发的 url **原样**交给侧边栏浏览器打开
      show/skip  —— legacy 模式（≤0.2.7 那套）与升级过渡期
    🔴 不再 ack：送达由页面自己证明（真被 GET 到才写 welcomed_at），客户端没有"我发过了"的话语权。
    """
    if args.ack or args.mark_shown:
        # 契约 v1.12 §5.7.8：ack 已退役（服务端那边也只是个不写库的桩）。老包里的 SKILL.md
        # 还会喊 --ack，这里静默回一句"已退役"——不报错、不写本地镜像，免得把"喊过"当"送达"。
        out({"success": True, "action": "ack", "deprecated": True})
        return 0

    # ① 镜像说已欢迎 → 直接跳过，**一个请求都不发**（契约保留这条 fast path）
    if _welcome_mirror()["welcomed"]:
        out({"success": True, "action": "skip", "reason": "already_shown"})
        return 0

    try:
        base, _ = load_env()
        if not base:
            # 包内 .env 与 ~/.config 凭据都没有时 base 是空串，ensure_key 会拿 "/v1/install"
            # 去建请求 → ValueError traceback。欢迎是附赠动作，环境不全也必须是静默 skip。
            return _welcome_skip("no_credentials")
        key = ensure_key(base)
    except (SystemExit, ValueError):
        return _welcome_skip("no_credentials")
    except Exception:                 # 网络超时等：附赠动作，一律静默，不许把 traceback 甩给用户
        return _welcome_skip("no_credentials")

    # 元信息超时 2 秒（客户端字面量，服务端读不到本机环境变量）
    tmo = float(os.environ.get("C2I_WELCOME_TIMEOUT", "2"))
    try:
        st, r = request("GET", f"{base}/v1/welcome", key=key, timeout=tmo, retries=0)
    except Exception:
        return _welcome_skip("fetch_failed")
    if st != 200 or not isinstance(r, dict):
        return _welcome_skip("fetch_failed")

    # ② 服务端说不用发：already_welcomed 把镜像补上（以服务端为准）；in_flight 只跳过
    # 升级过渡期可能是「新客户端 + 老服务端」：老响应没有 should_show，就按有没有海报判断，
    # 否则新客户端会一直 skip，欢迎永远发不出去（这是 0.2.8 前最常见的混搭）。
    if "should_show" not in r:
        r = dict(r, should_show=bool(r.get("welcome")), reason="ok" if r.get("welcome") else "no_asset")
    if r.get("should_show") is not True:
        reason = r.get("reason") or "skip"
        if reason == "already_welcomed":
            _welcome_write_mirror(0)
        return _welcome_skip(reason)

    # ② 页面交付（契约 v1.12 §5.7.7）：有 page.url 就交给 Agent 去侧边栏浏览器打开。
    # 这条必须在下载图之前——页面模式下不再发图，图由页面里的 <img> 自己拉（同源、可缓存）。
    page = r.get("page") or {}
    if r.get("deliver") == "page" and page.get("url"):
        fb_copy = r.get("copy") or None
        fb_text = (fb_copy or {}).get("text") or ""
        if fb_text and ("{" in fb_text or "}" in fb_text):
            fb_copy = None           # 文案没替换干净就不当兜底，页面自己能正常就正常
        out({"success": True, "action": "open_page",
             "url": page["url"],                       # 🔴 原样用：不拼域名、不改参数、不截断
             "ttl": int(page.get("ttl") or 0),
             # legacy 兜底物料：页面打不开时 Agent 还能退回老形态（不许删，判据 W-P44 同源）
             "fallback": {"welcome": r.get("welcome"), "copy": fb_copy}})
        return 0

    copy = r.get("copy") or None
    copy_text = (copy or {}).get("text") or ""
    if copy_text and ("{" in copy_text or "}" in copy_text):
        # 服务端已经承诺替换干净；万一没换干净，宁可不发也不把花括号递给用户
        return _welcome_skip("bad_copy")

    w = r.get("welcome")
    img = None
    if w:
        for k in ("version", "image_url", "image_sha256", "width", "height"):
            if w.get(k) in (None, ""):
                return _welcome_skip("bad_payload") if not copy_text else _welcome_skip("bad_payload")
        # 下载图（超时独立：体积比元信息大，失败一律静默；有文案时降级纯文字，不算失败）
        itmo = float(os.environ.get("C2I_WELCOME_IMG_TIMEOUT", "5"))
        try:
            st2, raw = request("GET", w["image_url"], key=None, timeout=itmo, raw=True, retries=0)
        except Exception:
            st2, raw = 0, None
        if st2 == 200 and raw:
            if hashlib.sha256(raw).hexdigest() != w["image_sha256"]:
                # 来路不明的图绝不展示；有文案就降级纯文字
                img = None
            else:
                ext = os.path.splitext(w["image_url"].split("?")[0])[1] or ".png"
                os.makedirs(CACHE_DIR, exist_ok=True)
                path = os.path.join(CACHE_DIR, f"welcome-{int(w['version'])}{ext}")
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(raw)
                os.replace(tmp, path)
                img = {"version": int(w["version"]), "image_path": path,
                       "image_url": w["image_url"],      # 原样透传，客户端不拼
                       "width": int(w["width"]), "height": int(w["height"]),
                       "alt": "Chat2image 欢迎海报"}     # 固定值，不许塞文案（一个 ) 就能撕开 Markdown

    if not img and not copy_text:
        return _welcome_skip("no_material")

    out({"success": True, "action": "show",
         # 交付形态（契约 §5.6.6）：有图＝图 1 条 + 文案 1 条；无图＝只发文案 1 条
         "deliver": "image_then_copy" if img else "copy_only",
         "image": img,
         "image_path": (img or {}).get("image_path"),
         "image_url": (img or {}).get("image_url"),
         "alt": (img or {}).get("alt"),
         "width": (img or {}).get("width"),
         "height": (img or {}).get("height"),
         "version": (img or {}).get("version"),
         "copy_text": copy_text,
         "copy_version": (copy or {}).get("version"),
         "copy_sha256": (copy or {}).get("sha256"),
         # 老形态（legacy/过渡期）：发完不用确认——送达由服务端看，客户端没有话语权
         "next": "send_now"})
    return 0


def cmd_balance(args):
    base, _ = load_env()
    key = ensure_key(base)
    st, r = request("GET", f"{base}/v1/balance", key=key)
    if st != 200:
        code, msg = err_code(r)
        # 查不到就说查不到，绝不显示一个缓存的旧余额
        die(code, f"查余额失败（不是 0 点）：{msg}", http=st)
    bal = r.get("balance", {})
    hr = r.get("has_recharged")
    st2, pr = request("GET", f"{base}/v1/pricing")
    per = 1
    if st2 == 200:
        per = int((pr.get("pricing", {}).get("points") or {}).get("1k") or 1)
    avail = int(bal.get("available", 0))
    if args.json:
        out({"success": True, "balance": bal, "can_make_1k_images": avail // per,
             "has_recharged": hr,
             "pricing_version": (pr.get("pricing") or {}).get("version")})
        return
    print(f"可用 {avail} 点（生成中 {bal.get('holding', 0)} 点，总计 {bal.get('total', 0)} 点）")
    print(f"约可出 {avail // per} 张 1K 图")
    if hr is True:
        print("默认清晰度：2K（充值过）")
    elif hr is False:
        print("默认清晰度：1K（未充值）")
    else:
        print("默认清晰度：1K（服务端没返回账号状态，按未充值处理）")


def _idem_key(ident, prompt, aspect, resolution):
    raw = f"{ident['install_id']}|{' '.join(prompt.split())}|{aspect}|{resolution}|{int(time.time() // 300)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _recent_pending(prompt, aspect, resolution):
    """连点保护：同参数 120 秒内直接复用上次任务，根本不发请求。

    不靠「让 Agent 记得别点两次」——Agent 没有可靠的回合内记忆，判定必须落在进程能读的文件上。
    """
    p = read_json(PENDING_FILE, {})
    if (p.get("prompt") == prompt and p.get("aspect") == aspect
            and p.get("resolution") == resolution
            and int(time.time()) - int(p.get("ts", 0)) < PENDING_WINDOW):
        return p
    return None


def cmd_gen(args):
    base, key0 = load_env()
    if not base:
        die("NO_BASE_URL", NO_BASE_SAY)
    key = ensure_key(base)
    ident = identity()
    prompt = args.prompt.strip()
    if not prompt:
        die("BAD_PROMPT", "提示词为空")

    st, pr = request("GET", f"{base}/v1/pricing")
    if st != 200:
        die("PRICING_FAILED", "取价目表失败，可能服务不可用", http=st)
    pricing = pr.get("pricing") or {}
    # 注意：open 是空数组 ≠ 没这个字段。全部下架时服务端返 []，
    # 写成 `pricing.get("open") or ["1k"]` 会让客户端误以为 1K 还开着（G6B 死角）。
    # 缺字段=老服务端，按 ["1k"] 兜底并打 degraded 标记（与 pick_default_resolution 同口径）
    open_missing = "open" not in pricing
    open_list = pricing["open"] if not open_missing else ["1k"]
    default_info = None
    resolution = args.resolution
    if resolution is None:
        # 用户没指定清晰度 → 契约 §1.7 按账号状态选默认档（G1/G2 静默降级、G3 保守明示）
        available, has_recharged = _account_state(base, key)
        default_info = pick_default_resolution(pricing, has_recharged, available)
        resolution = default_info["resolution"]
        if resolution is None:
            # §1.7.4：零生成请求。ask=让用户选档；none=NO_USABLE_RESOLUTION。
            # next 必须按 source/reason 分支（审查必修2）：全下架时没有档位可列，
            # 余额不足时必须给充值入口——不能永远抄情况 A 那句「列在售档给用户选」。
            src = default_info["source"]
            if src == "ask":
                nxt = ("把 say 和 options 里的在售档位与各自扣点列给用户，让他选一个；"
                       "他选完再带 --resolution 提交，别自己替他挑（静默升档=静默涨价）")
            elif default_info["reason"] == "balance_insufficient":
                # V1-07：同样不按缺口建单，直接给收银台（链接取不到就说「充值」给链接）
                _c = _cashier(base, key)
                nxt = ("明示「当前余额不够出图」，列出 options 里的可售档位与扣点，"
                       "然后给网页收银台"
                       + (f"：{_c[0]}（用户自己选点数，付完说「继续」再出图）" if _c
                          else "：让用户说「充值」，我再取链接给他；别自己编金额"))
            else:
                nxt = ("明示「目前暂无可用的清晰度档位，稍后再试」。options 为空：没有档位可列、"
                       "充值也解决不了——不要编出可选档位")
            out({"success": False,
                 "error": "NO_USABLE_RESOLUTION" if src == "none" else None,
                 "needs_user_choice": True,
                 "message": default_info["say"],
                 "say": default_info["say"],
                 "options": default_info["options"],
                 "reason": default_info["reason"],
                 "open": open_list,
                 "next": nxt})
            sys.exit(2)
    if resolution not in open_list:
        # 显式请求不降级：用户要的档没开就照实说，不许偷偷换成别的档
        die("RESOLUTION_CLOSED", f"{resolution} 暂未开放，可选：{','.join(open_list)}")
    points = int((pricing.get("points") or {}).get(resolution) or 0)

    # 图生图：先把参考图一张张传到中转站（不是直传上游——上游 key 只在服务端），
    # 拿 upload_id 随建单一起发。任何一张传失败就整单停，不悄悄少一张。
    ref_ids = []
    if args.image:
        MAX_BYTES = 5 * 1024 * 1024
        # 与服务端白名单一致：内容校验只认 PNG/JPEG，放行 webp/gif 只会让用户白传
        EXT_OK = {".jpg", ".jpeg", ".png"}
        for p in args.image:
            if not os.path.exists(p):
                die("REF_NOT_FOUND", f"参考图不存在: {p}")
            size = os.path.getsize(p)
            if size > MAX_BYTES:
                die("REF_TOO_LARGE", f"参考图 {os.path.basename(p)} 有 {size // 1024}KB，"
                                     f"超过 {MAX_BYTES // (1024 * 1024)}MB 上限，请先压缩")
            if os.path.splitext(p)[1].lower() not in EXT_OK:
                die("REF_BAD_FORMAT", f"不支持的图片格式 {p}，可用 jpg/png")
            with open(p, "rb") as f:
                data = f.read()
            boundary = "----c2i-client-" + os.urandom(12).hex()
            mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                  f"filename=\"{os.path.basename(p)}\"\r\nContent-Type: application/octet-stream\r\n\r\n")
            body = mp.encode() + data + f"\r\n--{boundary}--\r\n".encode()
            st, r = request("POST", f"{base}/v1/uploads", key=key, timeout=60,
                            raw_body=body, raw_ctype=f"multipart/form-data; boundary={boundary}",
                            headers={"Idempotency-Key": f"up-{hashlib.sha256(data).hexdigest()[:16]}"})
            if st != 200:
                code, msg = err_code(r)
                die(code or "UPLOAD_FAILED", msg or f"参考图上传失败（HTTP {st}）", http=st,
                    image=os.path.basename(p))
            ref_ids.append(r["upload_id"])

    pending = _recent_pending(prompt, args.aspect, resolution)
    if pending and pending.get("job_id") and not ref_ids:
        job_id = pending["job_id"]
    else:
        payload = {"prompt": prompt, "resolution": resolution, "aspect_ratio": args.aspect}
        if ref_ids:
            payload["ref_upload_ids"] = ref_ids
        st, r = request("POST", f"{base}/v1/jobs", payload,
                        key=key, timeout=30,
                        headers={"Idempotency-Key": _idem_key(ident, prompt, args.aspect, resolution)})
        if st == 402:
            code, msg = err_code(r)
            # 取不到码**不猜**欠费：猜错就是把不欠费的人引去付钱
            code = code or "UNKNOWN"
            reason = err_reason(r)
            balance = {}
            stb, rb = request("GET", f"{base}/v1/balance", key=key)
            balance_known = stb == 200
            if balance_known:
                balance = rb.get("balance", {}) or {}
            avail = int(balance.get("available") or 0)
            # V1-07：只有「明确欠费」才去取收银台；冻结/未知码一律不引导付款。
            # 顺序（代码审查 2026-11-22）：**先判余额够不够，再决定取不取 session**。
            # 余额明明够却被 402 拒（并发占点/风控）时再去取 session，等于让用户白等
            # 最多 10 秒、还白建一个结账会话——好路径被拖慢，属于不该有的代价。
            _need_cashier = (code == "INSUFFICIENT_CREDITS" and balance_known
                             and int(avail) < int(points or 0))
            cashier = _cashier(base, key) if _need_cashier else None
            say, fields = paywall_say(code, reason, points, avail,
                                      balance_known=balance_known, cashier=cashier)
            out({"success": False, "error": code,
                 "message": msg or "这次没有生成",
                 "balance": balance,
                 **fields,
                 # 冻结时 need_points 无意义（不欠费），给 None 而不是 0，免得被当成"需要 0 点"
                 "need_points": points if (code == "INSUFFICIENT_CREDITS"
                                           and balance_known) else None,
                 "reason": reason,          # 机器码只给管理员看，不进对客话术
                 "say": say,                # ← 对客整句，Agent 原样转述
                 "next": _FROZEN_NEXT if code == "CREDITS_FROZEN"
                         else (_CASHIER_NEXT
                               if (code == "INSUFFICIENT_CREDITS" and balance_known)
                               else "先问我一句「余额」，看清数字再决定"),
                 # 结构化字段：Agent/后台能看到链接本身，用户没点也能再发一次
                 "cashier_url": cashier[0] if cashier else None})
            sys.exit(2)
        if st != 200:
            code, msg = err_code(r)
            die(code, msg or "提交任务失败", http=st)
        job_id = r["job"]["id"]
        write_json(PENDING_FILE, {"prompt": prompt, "aspect": args.aspect,
                                  "resolution": resolution, "job_id": job_id,
                                  "ts": int(time.time())})

    # 内部轮询：不要要求 Agent 30 秒后再查一次（回合结束就丢上下文，任务会变孤儿）
    deadline = time.time() + (args.timeout or DEFAULT_TIMEOUT)
    job = {}
    while time.time() < deadline:
        st, r = request("GET", f"{base}/v1/jobs/{job_id}", key=key)
        if st != 200:
            code, msg = err_code(r)
            die(code, msg or "查任务失败", http=st)
        job = r.get("job") or {}
        if job.get("status") in ("succeeded", "failed", "expired", "canceled"):
            break
        time.sleep(2)
    else:
        die("JOB_TIMEOUT", f"等了 {args.timeout or DEFAULT_TIMEOUT} 秒还没出图，可稍后用 jobs 查")

    if job.get("status") != "succeeded":
        stb, rb = request("GET", f"{base}/v1/balance", key=key)
        out({"success": False, "error": "GENERATION_FAILED",
             "message": job.get("error") or f"生成失败（{job.get('status')}）",
             "points_refunded": True, "job_id": job_id,
             "balance": (rb.get("balance") or {}) if stb == 200 else {},
             "retry_hint": "说「重试」我就再出一次"})
        sys.exit(3)

    url = job.get("download_url")
    out_path = args.out
    if not out_path:
        # 默认仍写 ~/Downloads（用户行为不变）；C2I_OUT_DIR 只给测试/自动化改道用。
        # 2026-11-19 老马在 Downloads 里发现 294 张 mock 纯色图：自测没传 --out，
        # 每跑一次就往用户目录扔一张。测试一律用它指到临时目录。
        out_dir = os.environ.get("C2I_OUT_DIR") or "~/Downloads"
        out_path = os.path.join(os.path.expanduser(out_dir),
                                f"{NAME}-{job_id}-{int(time.time())}.png")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    st, img = request("GET", base + url, raw=True, timeout=120)
    if st != 200 or not img:
        die("DOWNLOAD_FAILED", "图片下载失败（链接 15 分钟有效，请重新获取链接）", http=st)
    with open(out_path, "wb") as f:
        f.write(img)
    os.chmod(out_path, 0o644)

    stb, rb = request("GET", f"{base}/v1/balance", key=key)
    bal = (rb.get("balance") or {}) if stb == 200 else {}
    avail = int(bal.get("available", 0))
    per = int((pricing.get("points") or {}).get("1k") or 1)
    points_actual = job.get("points_spent", points)
    actual = job.get("actual_resolution") or resolution
    downgraded = actual != resolution
    # 契约 v1.8 §3.3(d)-3.1：降档必须说清「本来该扣多少、已经退了多少」，不能只让用户看到扣得少
    req_points = int((pricing.get("points") or {}).get(resolution) or points)
    act_points = int((pricing.get("points") or {}).get(actual) or points_actual)
    out({
        "success": True,
        "image_path": out_path,
        "job_id": job_id,
        "resolution": resolution,
        "actual_resolution": actual,
        "downgraded": downgraded,
        "points_spent": job.get("points_spent", points),
        "balance": bal,
        "ai_generated": True,
        "ai_label": "本图为 AI 生成内容",
        "report": (f"本次消耗 {job.get('points_spent', points)} 点 → 剩余 {avail} 点"
                   f"（约还能出 {avail // max(per, 1)} 张 1K 图）"),
        "downgrade_note": (f"本次按 {actual.upper()} 出图，扣 {act_points} 点"
                           f"（原本 {resolution.upper()} 应扣 {req_points} 点，已退 "
                           f"{max(0, req_points - act_points)} 点）" if downgraded else None),
        "default_resolution": default_info,   # None = 用户显式指定；否则含 source/say，按 §1.7 播报
        "degraded_open_default": open_missing,  # 老服务端缺 open 字段、按 1K 兜底；排障先看这个
        "pricing_version": pricing.get("version"),
    })


def cmd_jobs(args):
    base, _ = load_env()
    key = ensure_key(base)
    st, r = request("GET", f"{base}/v1/jobs?limit={min(50, max(1, args.limit))}", key=key)
    if st != 200:
        code, msg = err_code(r)
        die(code, msg or "查任务列表失败", http=st)
    out({"success": True, **r})


def cmd_doctor(args):
    """自检：全绿才算装好。只报「有/没有」，绝不打印 key 值。"""
    ok, warn, bad, bad_codes = [], [], [], []

    def red(code, text):
        """红项 = 人话 + 机器码。机器码给升级第 8 步的回滚闸用（Y-1），人话给用户看。"""
        bad_codes.append(code)
        bad.append(text)

    base, key = load_env()
    info = {"success": True, "app": NAME, "version": VERSION,
            "skill_dir": SKILL_DIR, "config_dir": CONFIG_DIR}

    vj = read_json(os.path.join(SKILL_DIR, "version.json"), {})
    if vj.get("name") == NAME and vj.get("version") == VERSION:
        ok.append(f"版本文件 OK（{NAME} {VERSION}）")
    else:
        red("VERSION_FILE", f"version.json 不对：{vj}")

    if sys.version_info >= (3, 8):
        ok.append(f"Python {sys.version.split()[0]}")
    else:
        red("PYTHON_OLD", f"Python 版本过低：{sys.version.split()[0]}（要 ≥3.8）")

    if base:
        ok.append(f"中转站地址 {base}")
    else:
        red("NO_BASE_URL", "BASE_URL 未配置")

    if base:
        st, r = request("GET", f"{base}/v1/health", timeout=10)
        if st == 200:
            ok.append(f"服务可达（{r.get('version')}，mock={r.get('mock_provider')}）")
        else:
            red("HEALTH", f"服务不可达（HTTP {st}）")
        st, pr = request("GET", f"{base}/v1/pricing", timeout=10)
        if st == 200:
            p = pr.get("pricing") or {}
            ok.append(f"价目表可读（1K={p.get('points', {}).get('1k')} 点，版本 {p.get('version')}）")
        else:
            red("PRICING", "价目表读不到")

    if key:
        ok.append("已持有本机 key（不显示内容）")
        if base:
            st, r = request("GET", f"{base}/v1/balance", key=key, timeout=10)
            if st == 200:
                b = r.get("balance", {})
                ok.append(f"余额可查：可用 {b.get('available')} 点 / 生成中 {b.get('holding')}")
            elif st == 401:
                red("KEY_INVALID", "key 无效（可能被吊销），需要重新建号或换 key")
            else:
                warn.append(f"余额查不到（HTTP {st}）")
    else:
        warn.append("还没有 key —— 第一次出图时会自动建号，无需手动配置")

    # P-13②：把「已停用的备份数」打出来。升级那一步是旧客户端执行的，那一跳不会 defuse，
    # 用户跑一条命令才知道到底修没修到现场；不打印就只能靠猜。
    _stashed = _defused_bak_count()
    if _stashed:
        ok.append(f"旧版备份已停用 {_stashed} 份（不会再被当成同一个 Skill）")

    # P-11：技能目录里若有多份都叫 chat2image 的说明，平台可能加载到旧的那份。
    # 文案按 UI 2026-11-22 的要求：说「发生了什么、你该做什么」，不暴露 name 字段这种内部词。
    # Y-2（2026-11-22 运维官真机查出）：以前这句无条件写「已停用…重开一次对话即可生效」，
    # 目录名不是 chat2image 时 defuse 一份都没停，那句就是假话——**只在真停了时才可以那么说**。
    _dupes = _skill_registry_dupes()
    _live = {os.path.realpath(SKILL_DIR)}
    _others = [d for d in _dupes if os.path.realpath(os.path.join(_parent_dir(), d)) not in _live]
    if _others:
        red("DUPES", f"还有 {len(_others)} 份说明文件也叫 {NAME}，没能停用（{', '.join(_others)}）；"
                     f"平台可能加载到旧的那份，看起来像“升级了但没变化”。"
                     f"你只需做一件事：把上面这几个目录整个删掉，然后重开一次对话。")

    # 目录可写
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        probe = os.path.join(CONFIG_DIR, ".doctor-probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        ok.append("配置目录可写")
    except OSError as e:
        red("CONFIG_DIR", f"配置目录不可写：{e}")

    # 包里绝不能有上游 key
    for env_name in ("TOAPIS_API_KEY", "IMAGE2FREE_API_KEY", "GRSAI_API_KEY"):
        if os.environ.get(env_name):
            # 2026-11-23 PM 定：对外文案不提上游服务商名字（那是供货链 / 竞争壁垒），
            # 检测仍按变量名判断，一个字符没放宽。
            warn.append("环境里存在上游服务商使用的 key（客户端不需要，也不会用到，可以删掉）")
    env_path = os.path.join(SKILL_DIR, ".env")
    if os.path.exists(env_path):
        content = open(env_path, encoding="utf-8").read()
        leaked = [k for k in ("TOAPIS_API_KEY", "IMAGE2FREE_API_KEY", "GRSAI_API_KEY") if k in content]
        if leaked:
            red("UPSTREAM_KEY", ".env 里有上游服务商的 key（必须删掉：客户端用不到，留着只是隐患）")
        if os.stat(env_path).st_mode & 0o077:
            # 只警告不够：用户哪天手工把 key 填进来，松权限就是隐患。直接收紧（不打内容、不报错中断）
            try:
                os.chmod(env_path, 0o600)
                ok.append(".env 权限原为宽松，已自动收紧为 600")
            except OSError as e:
                warn.append(f".env 权限偏松且自动收紧失败：{e}")

    info.update({"ok": ok, "warnings": warn, "errors": bad, "error_codes": bad_codes,
                 "status": "ok" if not bad else "broken"})
    if args.json:
        out(info)
        # Y-1（2026-11-22 代码审查 + QA 各自复核）：以前 --json 恒 rc=0，
        # 升级第 8 步拿它当回滚闸，status=broken 也放行。退出码现在和人性版一致。
        if bad:
            sys.exit(1)
        return
    print(f"{NAME} {VERSION} · 自检")
    print(f"目录：{SKILL_DIR}")
    for line in ok:
        print(f"  ✅ {line}")
    for line in warn:
        print(f"  ⚠️  {line}")
    for line in bad:
        print(f"  ❌ {line}")
    print()
    if bad:
        print("有红项，把上面 ❌ 那几行原文贴给维护者，别自己猜。")
        sys.exit(1)
    print("全绿，直接说你要什么图就行。")


def local_version_info():
    """本地版本以包内 `version.json` 为准（契约 §4 第 1 步）；缺了才退到脚本常量。

    为什么不直接用 `VERSION` 常量：装了新包但常量忘了改，客户端就会永远说
    「已是最新」——那种错用户永远看不到，只会一直用旧版。
    """
    v = read_json(os.path.join(SKILL_DIR, "version.json"), {})
    ver = str(v.get("version") or VERSION)
    try:
        schema = int(v.get("schema") or 1)
    except (TypeError, ValueError):
        schema = 1
    return ver, schema


# 每种结论对应的一句话。**用户唯一会读到的就是这句**（契约 §2），所以：
# 不出现安装路径、不出现 ~/.config、不出现 sha256 明文、不出现堆栈。
_UPDATE_SAY = {
    "up_to_date": "已经是最新版 {to}，不用升级。",
    "no_release": "当前没有可升级版本，你手上的 {cur} 就是最新对外版本。",
    "downgrade_blocked": "你手上的 {cur} 比线上发布的 {to} 还新（内测版？），我不给你降级。",
}
_UPDATE_SAY_REINSTALL = {
    "schema": "新版 {to} 的包结构变了，不能自动升。按包内 INSTALL.md 手动重装一次，"
              "数据和余额都跟着账号走。",
    "too_old": "你现在是 {cur}，版本差太远，不能一步跳到 {to}。按包内 INSTALL.md 手动重装一次，"
               "余额跟着账号走。",
    "breaking": "新版 {to} 有重要结构变化，不自动升级。按包内 INSTALL.md 手动重装一次，"
                "余额跟着账号走。",
    "protocol": "线上清单格式我不认识，这次不自动升级；按包内 INSTALL.md 手动重装一次。",
}


def cmd_check_update(args):
    """只读：拉清单、比版本、**不下载不写盘**（契约 §2）。

    退出码：0 = 有新版或已最新（拿到结论就算成功）；2 = 网络/清单失败。
    """
    base, _ = load_env()
    cur, cur_schema = local_version_info()

    def emit(action, to, changes, say, failed=False, detail=None):
        if args.json:
            out({"success": not failed, "action": action, "from": cur, "to": to,
                 **({"changes": changes} if changes is not None else {}),
                 **({"detail": detail} if detail else {}), "say": say})
        else:
            print(say)
        if failed:
            sys.exit(2)

    if not base:
        emit("no_base_url", None, None,
             "这个技能还没配服务地址，查不了更新；当前版本正常可用。",
             failed=True, detail="NO_BASE_URL")
        return

    payload, code, msg = upgrade.fetch_json(f"{base}/v1/version")
    if payload is None:
        # 断网/清单坏了都是「拿不到结论」，按契约退出 2。**不自动换源**：换源改变不了这个事实。
        emit("no_source", None, None,
             "现在连不上更新服务器，当前版本正常可用，不用管它。",
             failed=True, detail=f"{code}: {msg}")
        return

    d = upgrade.decide(cur, cur_schema, payload)
    action, to, reason = d["action"], d["to"], d.get("reason") or ""

    if action == "manifest_broken":
        emit(action, to, None,
             "线上更新清单有问题，这次没拿到干净的版本信息；当前版本正常可用。"
             "把这句话原文转给维护者。", failed=True, detail=f"MANIFEST_BROKEN: {reason}")
        return
    if action in ("up_to_date", "no_release", "downgrade_blocked"):
        emit(action, to, [], _UPDATE_SAY[action].format(cur=cur, to=to or cur))
        return
    if action == "manual_reinstall":
        emit(action, to, [], _UPDATE_SAY_REINSTALL.get(reason, _UPDATE_SAY_REINSTALL["schema"])
             .format(cur=cur, to=to or "新版"))
        return

    # update_available：取「这次改了」的前 3 条。取不到不阻断升级（不是错误），
    # 但要说清楚「没取到」，不能假装这版没改动。
    changes, _summary = [], ""
    log, _c, _m = upgrade.fetch_json(f"{base}/v1/changelog")
    if log is not None:
        changes, _summary = upgrade.pick_changes(log, to)
    if changes:
        say = (f"有新版本 {cur} → {to}。这次改了：{'；'.join(changes)}。"
               "要不要升？回我「升级」我就升；不升也不影响现在用。")
    else:
        say = (f"有新版本 {cur} → {to}。这次改了什么暂时没取到。"
               "要不要升？回我「升级」我就升；不升也不影响现在用。")
    emit("update_available", to, changes, say)


def _take_upgrade_lock(path):
    """占住升级锁。返回 False = 另一个升级正在跑。

    进程已经不在（上次被 kill 留下的锁）→ 判为陈旧锁，清掉重占；
    否则用户会被"永远有升级在跑"卡住，只能自己去删文件。
    """
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                pid = int(open(path, encoding="utf-8").read().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                return False
            try:
                os.remove(path)      # 陈旧锁
            except OSError:
                return False
        except OSError:
            return False
    return False


def cmd_upgrade(args):
    """契约 §4 十步。**只在用户明确说「升级」之后才跑**，从不静默升。

    退出码：0 = 成功或已最新；1 = 中止（验签/自检/回滚，旧版完好）；2 = 网络失败。
    硬规则：只写 Skill 目录与系统临时目录，**绝不写 ~/.config**、不 sudo、不跑包内脚本（I8/I10）。
    """
    if getattr(args, "check", False):
        return cmd_check_update(args)

    base, _ = load_env()
    cur, cur_schema = local_version_info()
    parent = os.path.dirname(os.path.abspath(SKILL_DIR))
    stage = ""

    def finish(rc, action, to, say, **extra):
        if args.json:
            out({"success": rc == 0, "action": action, "from": cur, "to": to,
                 "restart_required": False, "say": say, **extra})
        else:
            print(say)
        sys.exit(rc)

    if not base:
        finish(2, "no_base_url", None, "这个技能还没配服务地址，升不了；当前版本正常可用。")

    payload, code, msg = upgrade.fetch_json(f"{base}/v1/version")
    if payload is None:
        finish(2, "no_source", None, "现在连不上更新服务器，当前版本正常可用，不用管它。")

    d = upgrade.decide(cur, cur_schema, payload)
    action, to, reason = d["action"], d["to"], d.get("reason") or ""
    allow_down = bool(getattr(args, "allow_downgrade", False))

    if action in ("up_to_date", "no_release"):
        finish(0, action, to, _UPDATE_SAY[action].format(cur=cur, to=to or cur))
    if action == "manifest_broken":
        finish(2, action, to, "线上更新清单有问题，这次没升级；当前版本正常可用。")
    if action == "downgrade_blocked" and not allow_down:
        finish(0, action, to, _UPDATE_SAY["downgrade_blocked"].format(cur=cur, to=to))
    if action == "manual_reinstall":
        finish(0, action, to, _UPDATE_SAY_REINSTALL.get(reason, _UPDATE_SAY_REINSTALL["schema"])
               .format(cur=cur, to=to or "新版"))

    latest = payload.get("latest") or {}
    urls = latest.get("urls") or []
    if not urls:
        finish(2, "no_source", to, "线上清单没给下载地址，这次没升级；当前版本正常可用。")

    # ---- 第 0 步：基线（I8 判据）----
    # 基线只保护「用户**已有**的配置」（P2-1，2026-11-22）：
    # 「装完从没出过图」的机器上配置目录根本不存在，第 8 步 doctor 会建目录、8b 的 balance 会建号，
    # 这两件事都是正常的初始化。拿「空 → 有」去比指纹，只会把一个好升级判成「配置被改动了」。
    # 所以：目录本来就有才比指纹；key/identity 本来就有才比它们。
    cfg_present = os.path.isdir(CONFIG_DIR)
    fp0 = upgrade.config_fingerprint(CONFIG_DIR) if cfg_present else ""
    ident0 = str(read_json(IDENTITY_FILE, {}).get("install_id") or "")
    key0 = (load_env()[1] or "")[:11]

    # ---- 并发锁：双击两次就是两个进程同时改名，后者会报「改不动旧目录」——
    # 那个提示会把人引去查权限，实际是"已经升完了"。一把锁比一句更准的话省事。
    lock_path = os.path.join(parent, ".c2i-upgrade.lock")
    if not _take_upgrade_lock(lock_path):
        finish(1, "already_running", to,
               f"已经有一个升级在跑了。等它跑完再试，当前版本正常可用。")

    tmpdir = tempfile.mkdtemp(prefix="c2i-upgrade-")
    try:
        # ---- 第 4 步：下载到系统临时目录（绝不在 Skill/config 目录里下）----
        zpath = os.path.join(tmpdir, "package.zip")
        okd, whyd = upgrade.download_to(str(urls[0]), zpath)
        if not okd:
            finish(2, "download_failed", to,
                   f"下载没成功，还是 {cur}，功能正常；过一会儿再试。")

        # ---- 第 5 步：先哈希、后签名；任一不过都不解压（I9）----
        okv, vcode, whyv = upgrade.verify_package(zpath, latest)
        if not okv:
            if vcode == "UPDATE_SIG_INVALID":
                finish(1, "sig_invalid", to,
                       f"这个升级包的签名不对，已经停止，没有安装。当前仍是 {cur}。")
            finish(1, "hash_mismatch", to,
                   f"升级包不完整或已损坏，没有安装。当前仍是 {cur}。")

        # ---- 第 6 步：解压 + 装前自检 ----
        # 解压到 **Skill 目录同级**：跨文件系统 rename 会失败，那样"原子替换"就是假的
        stage = os.path.join(parent, f".c2i-stage-{to}-{os.getpid()}")
        if os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        okx, whyx = upgrade.safe_extract(zpath, stage)
        if okx:
            root = upgrade.find_package_root(stage)
            oks, whys = upgrade.selfcheck(root, to)
        if not (okx and oks):
            finish(1, "selfcheck_failed", to,
                   f"新版包自检没通过（{(whyx if not okx else whys)}），没有替换。当前仍是 {cur}。")

        # ---- 第 7 步：原子替换 ----
        okr, bak, whyr = upgrade.atomic_replace(SKILL_DIR, root, base=base)
        if not okr:
            finish(1, "replace_failed", to, f"升级没成功，还是 {cur}，功能正常。{whyr}")

        # ---- 第 8 步：装后自检（本轮只跑 doctor，不跑包内脚本）----
        # Y-1（2026-11-22 代码审查 + QA 各自复核）：以前只取 rc，而 `doctor --json` 恒 rc=0，
        # 这道回滚闸等于不存在（status=broken 照样「升级成功」）。现在按 JSON 判，但只拦
        # 「这个包本身坏了」那几类——一次网络抖动就把好升级回滚掉，比不拦更坏。
        _rc, outdoc = upgrade.run_client(SKILL_DIR, ["doctor", "--json"], timeout=90)
        docfatal = upgrade.postcheck_fatal(outdoc)
        # ---- 第 8b 步见第 9 步之后：balance 可能建号（写 credentials.json），
        # 会把第 9 步的配置基线冲掉，顺序反了就把好升级回滚掉（P2-1）----
        # ---- 第 9 步：I8 复核（配置目录/identity/key 前缀，任一变了就回滚）----
        fp1 = upgrade.config_fingerprint(CONFIG_DIR)
        ident1 = str(read_json(IDENTITY_FILE, {}).get("install_id") or "")
        key1 = (load_env()[1] or "")[:11]
        cfg_changed = cfg_present and fp0 != fp1
        ident_changed = bool(ident0) and ident0 != ident1
        key_changed = bool(key0) and key0 != key1
        if docfatal or cfg_changed or ident_changed or key_changed:
            okrb, whyrb = upgrade.rollback(SKILL_DIR, bak)
            if not okrb:
                finish(1, "rollback_failed", cur,
                       f"自动回滚也失败了，请按包内 INSTALL.md 手动重装一次。当前目录里可能不是可用版本。"
                       f"{whyrb}")
            finish(1, "rolled_back", cur,
                   f"升级没成功，已经回到 {cur}，功能正常。原因："
                   f"{'自检没通过' if docfatal else '配置被改动了'}。")

        # ---- 第 9b 步（原 8b）：真跑一次 balance ----
        # doctor 全绿只说明"文件都在"。2026-11-22 实测：升级完 .env 丢了，doctor 因环境变量顶替
        # 照样绿，用户却连不上服务端。能不能取到余额，才算升级成功的证据。
        # 顺序（P2-1，2026-11-22 PM 定）：必须放在第 9 步基线复核**之后**——balance 在「装完
        # 从没出过图」的机器上会自动建号、写 credentials.json，先跑它会让基线变化，把一个好升级
        # 判成「配置被改动了」然后回滚。0.2.0→0.2.3 没撞上，是因为 0.2.0 的客户端根本没有 8b；
        # 0.2.3→0.2.4 这一跳会真撞上。
        okbal, outbal = upgrade.run_client(SKILL_DIR, ["balance", "--json"], timeout=45)
        if not okbal and "BASE_URL" in (outbal or "").upper():
            okrb, whyrb = upgrade.rollback(SKILL_DIR, bak)
            if not okrb:
                finish(1, "rollback_failed", to,
                       f"新版连不上服务端，自动回滚也失败了，请按包内 INSTALL.md 手动重装。{whyrb}")
            finish(1, "rolled_back", cur,
                   f"升级没成功，已经回到 {cur}，功能正常。原因：升级后读不到服务地址（.env）。")

        # ---- 第 10 步：只留最近两个 .bak ----
        # 从 1 改成 2（0.2.4）：.bak-<旧版本> 里躺的是用户升级前那份带注释的 .env，
        # 只留一个的话，下一次升级就把它冲掉，0.3.0 迁移时拿不到完整来源。
        upgrade.keep_latest_bak(parent, 2)
        # P-10：这次升级产生的 .bak 也别再像 Skill——否则用户下次开对话，
        # 平台可能解析到这份旧说明，看起来像「升级了但没变化」。
        upgrade.defuse_all_baks(parent, NAME, active_dir=SKILL_DIR)
        # sig_verified：验签失败在第 5 步就中止了，能走到这里说明签名验过。
        # 以前 --json 里没有这个字段，"验签通过"只能靠代码审查背书（QA 2026-11-22 指出）。
        finish(0, "upgraded", to, f"已升级到 {to}（原 {cur}）。",
               sig_verified=True, balance_ok=bool(okbal))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _defuse_legacy_baks():
    """把同级目录里历史遗留的 `<包名>.bak-*` 去技能化（P-10，2026-11-22）。

    为什么必须在客户端启动时做：0.2.4 之前装上的机器，`.bak-0.2.0/` 里那份旧 SKILL.md
    的 name 也叫 chat2image——平台会解析到它，模型照着旧指令用 image_path 发本地文件，
    图就发不出去。而那次升级是**旧客户端**执行的，写在新版里的收尾逻辑管不到它，
    所以只能在新版第一次跑起来时把历史遗留一并处理掉。
    静默、best-effort：失败只是维持现状，不新增任何用户可见错误。
    """
    parent = _parent_dir()
    try:
        # 识别按 frontmatter（upgrade.skill_md_name），并排除现役目录——
        # 不能再靠 `<NAME>.bak-` 目录名前缀：安装目录被改名成 skill 时一份都匹配不上（Y-2）。
        upgrade.defuse_all_baks(parent, NAME, active_dir=SKILL_DIR)
    except Exception:                                  # noqa: BLE001
        pass


def _parent_dir():
    """安装目录的同一级（`~/.workbuddy/skills`）。备份、现役目录都躺在这一层。"""
    return os.path.dirname(SKILL_DIR.rstrip(os.sep))


def _defused_bak_count():
    """数同级目录里"已停用"的**旧版备份**（`<安装目录名>.bak-<版本>`）。

    两个条件缺一不可：
      · 名字里有 `.bak-` —— 备份只有 `keep_latest_bak` 造，命名固定；回滚残留的
        `.failed-<stamp>` 同样会被我们加后缀停用，但它不是备份，数进来就会说
        「已停用 1 份」而实际一份备份都没有（代码审查 W-1，2026-11-22 实测复现）。
      · 有我们加的标记文件、没有原名 SKILL.md —— 这才是「已停用」。
    Y-2 之前这里还要求目录名以 `<NAME>.bak-` 开头，安装目录被改名后这个数恒为 0；
    现在只认「看起来像备份（.bak-）」+「确实被停用了」，与目录名叫什么无关。
    """
    parent = _parent_dir()
    n = 0
    try:
        for name in os.listdir(parent):
            if ".bak-" not in name:
                continue
            d = os.path.join(parent, name)
            if os.path.realpath(d) == os.path.realpath(SKILL_DIR):
                continue
            if os.path.exists(os.path.join(d, "SKILL.md" + upgrade.BAK_MARK)):
                n += 1
    except OSError:
        return 0
    return n


def _skill_registry_dupes():
    """列出同级目录里 frontmatter 的 name 等于 NAME 的目录（含自己）。

    与 upgrade.defuse_all_baks 共用 upgrade.skill_md_name 这一套识别（Y-2）：
    两边各写一份的话，「谁算冒充」就会出现两种口径，doctor 与 defuse 互相打脸。
    """
    parent = _parent_dir()
    got = []
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return got
    for n in names:
        if upgrade.skill_md_name(os.path.join(parent, n)) == NAME:
            got.append(n)
    return got


def main():
    _defuse_legacy_baks()
    ap = argparse.ArgumentParser(prog=NAME)
    # 契约 §4 第 6 步要能跑 `client.py --version` 做装前自检（子命令不能当这个用）
    ap.add_argument("--version", action="version", version=f"{NAME} {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen")
    g.add_argument("--prompt", required=True)
    g.add_argument("--resolution", default=None, choices=["1k", "2k", "4k"],
                   help="不给则按账号默认（未充值 1K / 充值过 2K，契约 §1.7）；给了就是显式请求，不降级")
    g.add_argument("--aspect", default="3:4")
    g.add_argument("--image", action="append",
                   help="参考图路径（图生图，可多次传，最多 4 张、单张 ≤5MB）")
    g.add_argument("--out")
    g.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    g.set_defaults(func=cmd_gen)

    b = sub.add_parser("balance")
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_balance)

    rc = sub.add_parser("recharge")
    rc.add_argument("--points", type=int, default=None, help="要充几点（不给则打开网页收银台自己选）")
    rc.add_argument("--package", type=int, default=None,
                    help="买指定积分包（与 --points 二选一，包价由服务端返回）")
    rc.add_argument("--channel", default="alipay", choices=["alipay", "wxpay"],
                    help="支付渠道，默认支付宝当面付")
    rc.add_argument("--force", action="store_true", help="余额明明够也照充")
    rc.add_argument("--ascii", action="store_true",
                    help="额外输出终端 ASCII 二维码（默认只给 PNG 路径）")
    rc.set_defaults(func=cmd_recharge)

    p = sub.add_parser("pricing")
    p.set_defaults(func=cmd_pricing)

    dr = sub.add_parser("default-res", help="用户没指定清晰度时，按账号状态给出本次默认档（恒为 JSON）")
    dr.set_defaults(func=cmd_default_res)

    j = sub.add_parser("jobs")
    j.add_argument("--limit", type=int, default=20)
    j.set_defaults(func=cmd_jobs)

    d = sub.add_parser("doctor")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)

    wc = sub.add_parser("welcome", help="看要不要给用户看欢迎页（已欢迎过则零网络请求）")
    wc.add_argument("--ack", action="store_true",
                    help="（已退役，0.2.8 起送达由服务端看页面是否真被打开；保留参数不报错）")
    wc.add_argument("--mark-shown", action="store_true",
                    help="（老名字，等同 --ack；保留只为兼容旧调用）")
    wc.add_argument("--version", type=int, default=0, help="（已弃用，保留参数兼容）")
    wc.set_defaults(func=cmd_welcome)

    cu = sub.add_parser("check-update", help="看有没有新版（只读：不下载、不安装）")
    cu.add_argument("--json", action="store_true")
    cu.set_defaults(func=cmd_check_update)

    ug = sub.add_parser("upgrade", help="升级到最新版（**只在用户明确说「升级」后跑**）")
    ug.add_argument("--check", action="store_true", help="等价 check-update")
    ug.add_argument("--allow-downgrade", action="store_true",
                    help="允许装比当前更旧的版本（仍要验签通过）")
    ug.add_argument("--json", action="store_true")
    ug.set_defaults(func=cmd_upgrade)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
