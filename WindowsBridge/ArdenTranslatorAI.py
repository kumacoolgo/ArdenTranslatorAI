# ArdenTranslator AI Windows Bridge
# Standard-library only at runtime: tkinter, urllib, sqlite3, threading.
# Build to EXE with build_exe.cmd (PyInstaller).
#
# v1.0.15：EXE 从「纯悬浮窗」升级成「管理界面 + 一键部署」。
#   - 双击 EXE 先出管理界面：客户端目录 / 模型 / ZHIPU_API_KEY / 悬浮窗外观。
#   - 点「开始」= 部署插件到 Interface\AddOns（删旧重建，保证最新）+ 存配置
#     + 关管理窗 + 直接弹出翻译悬浮窗，一步到位。
#   - 只支持智谱（GLM）。openai / gemini 两条分支连同它们的函数一起删掉了。
#
# v1.0.16：API Key **只**存在 Windows 用户环境变量里，绝不写进 config.json。
#   - 管理界面填的 Key 直接写进用户环境变量（注册表 HKCU\Environment）。
#   - save_config() 会主动剥掉 compat.api_key，杜绝任何路径把明文落盘。
#   - 老配置里如果躺着明文 Key，首次启动会自动搬进环境变量并从磁盘删掉。
#
# v1.0.17：EXE 可以安全地分发给别人 —— 里面不含任何 Key。
#
# ============================ v1.1（本次） ============================
# 按《ArdenTranslatorAI v1.1 / v1.2 修改方案》完成第一阶段六项。最大的变化是
# 把「所有逻辑塞在一个文件里」拆成几个可离线测试的模块：
#
#   protocol.py     消息协议：一次解析、一次解码（V11-01）
#   jobs.py         有界优先级队列 + 固定 worker（V11-02）
#   cache.py        版本化缓存 key + singleflight 合并（V11-03）
#   dedupe.py       会话水位 + 有界 LRU 去重（V11-04）
#   transport.py    通道健康状态机（V12-03 的骨架）
#   diagnostics.py  版本/协议范围/脱敏导出（V11-06、V12-06 的骨架）
#
# 逐项对应：
#   V11-01  dispatch_payload 不再「先整体 unesc 再 split 再 unesc」——
#           改成调用 protocol.parse_payload()，每个字段只解一次。
#           修掉了「字面 %2E 被解成 .」和「正文里的 | 破坏切分」两个真 bug。
#   V11-02  每条消息不再各起线程；改成一个容量 100 的有界队列 + 固定 2 个 worker。
#           满队列不阻塞读取线程，按优先级丢最旧的低优先级消息并计数。
#   V11-03  缓存 key 加入 target / prompt 版本 / 术语哈希 / base_url；
#           新增 inflight 合并：同内容 20 条同时到达只发 1 次 HTTP。
#   V11-04  Lua 的 SESSION 改成防碰撞纯数字串（秒级时间 + reload 计数 + 随机尾数）；
#           去重改成「会话水位 + 有界 LRU」，不再 set.clear() 导致整文件重放。
#   V11-05  保持 autoFlush=false；/atai test、/atai diag 不再偷偷改日志开关；
#           flushtest 需要显式确认且会恢复原状态。
#   V11-06  deploy_addon 改成「校验 → 同卷临时目录 → 校验 → 备份 → 原子替换 →
#           失败回滚」；.spec 里写死的 C:/Users/... 绝对路径改成相对路径。
#
# 不可退化的部分**全部保留**：英/日/俄翻译、跳过纯中文、公会/队伍/团队/密语/
# 公共频道与屏蔽、Ctrl+H / Ctrl+Q、/atai status|test|diag、
# ATAI2 与 @ATAI1@（含历史 5 段）兼容解析、双通道。

from __future__ import annotations
import codecs
import json
import os
import queue
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import hashlib
from pathlib import Path
from urllib import request, error
import tkinter as tk
from tkinter import filedialog, messagebox

# v1.1 本地模块（PyInstaller 通过 .spec 的 hiddenimports 收集）。
import dedupe as _dedupe
import diagnostics as _diag
import jobs as _jobs
import protocol as _protocol
import transport as _transport
from cache import (SingleFlight, TranslationCache, cache_key, normalize_text,
                   terms_fingerprint, normalize_base_url)

APP_NAME = "ArdenTranslator AI"
# v1.0.14：修「公共频道完全翻译不了」——插件 esc() 现在把点号转义成 %2E，
# 这里 unesc() 负责还原。原因见插件头部的 k) 条目。
#
# v1.1：解析逻辑整体搬到 protocol.py（一次性解析，每字段只解码一次），
# 这里保留 unesc 作为对外 API 的薄封装，避免旧调用点失效。
VERSION = "1.1.1"
# 协议兼容范围（V11-06 要求安装包能显示）。桥接端能听懂：
#   1 = @ATAI1@ 5 段（历史）
#   2 = @ATAI1@ / ATAI2 6 段（当前）
PROTOCOL_SUPPORTED = _diag.PROTOCOL_SUPPORTED
MARKER = _protocol.MARKER_CHATLOG
MISS_MARKER = _protocol.MARKER_MISS
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "ArdenTranslatorAI"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
CACHE_PATH = DATA_DIR / "cache.sqlite3"

# 插件文件随 EXE 一起打包（PyInstaller --add-data），点「开始」时从这里拷进
# 客户端的 Interface\AddOns。这样 EXE 永远自带一份**最新**插件，用户不需要
# 再手工复制任何文件。
ADDON_NAME = "ArdenTranslatorAI"
ADDON_FILES = ("ArdenTranslatorAI.lua", "ArdenTranslatorAI.toc")

# 只支持智谱。base_url 固定，UI 上不暴露，避免用户改坏。
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4-flash-250414"
DEFAULT_API_KEY_ENV = "ZHIPU_API_KEY"

DEFAULT_CONFIG = {
    # 只有智谱一路。provider 字段保留只是为了写进缓存 key（换模型要重翻）。
    "provider": "zhipu",
    # Any OpenAI-compatible /chat/completions endpoint. 默认指向智谱：
    #   Zhipu GLM (key from https://bigmodel.cn/usercenter/proj-mgmt/apikeys)
    #     Free text models, measured 2026-10:
    #       glm-4-flash-250414  ~0.8s, non-reasoning  <- best for live chat
    #       glm-4.5-flash       reasoning, slow, needs a bigger max_tokens
    #       glm-4.7-flash       reasoning, frequently overloaded (429/timeout)
    # 想换成本地 Ollama 之类的兼容端点，改 base_url 即可（UI 不提供入口）。
    # "extra_body" 会原样并进请求体，作为逃生口，例如
    # "extra_body": {"max_tokens": 2048} 或给推理模型用
    # "extra_body": {"thinking": {"type": "disabled"}}。
    "compat": {
        "base_url": ZHIPU_BASE_URL,
        "model": DEFAULT_MODEL,
        "api_key_env": DEFAULT_API_KEY_ENV,
        # v1.0.16 起**这里没有 api_key 字段**，而且 save_config() 会主动剥掉它。
        # Key 只存在 Windows 用户环境变量里（见 set_user_env / effective_api_key），
        # 配置文件里永远不出现明文。
        "extra_headers": {},
        "extra_body": {}
    },
    "wow_path": "",
    "overlay": {
        "x": 40,
        "y": 520,
        "width": 500,
        "height": 200,
        "alpha": 0.88,
        "font_size": 9,
        "max_lines": 7
    },
    "translation": {
        "target": "Simplified Chinese",
        "skip_chinese": True,
        # Free tiers cap concurrency: 3 simultaneous requests to Zhipu already
        # produced a 429. 2 keeps bursts moving without tripping the limit.
        # v1.1：这个值现在是**固定 worker 线程数**（不再是信号量），被夹在 1~4。
        "max_workers": 2,
        # v1.1（V11-02）：有界队列容量。上限恒定，满了按优先级丢最旧的低优先级。
        # 规范建议值 100。
        "queue_capacity": 100,
        # 任务从**入队那一刻**起算的寿命：超过就丢掉，省一次 API 调用。
        # 公共频道刷得快、价值衰减也快，给 30 秒；其他（公会/密语）给 120 秒。
        "queue_ttl_seconds": 30,
        "queue_ttl_seconds_other": 120,
        "request_timeout_seconds": 12,
        "retry_attempts": 3,
        "retry_backoff_seconds": 0.8,
        # On startup, rewind instead of jumping straight to EOF: the bridge gets
        # restarted mid-session all the time, and silently dropping everything
        # that arrived while it was down looks exactly like a broken pipeline.
        # Only lines stamped within this many seconds are translated; 0 = off.
        "startup_replay_seconds": 600,
        # 不希望被 AI 翻译的词（先替换成占位符，翻完再还原）。
        # 前四个是 ArdenWoW 这台服的专有名词，**换服请改成你自己服务器/职业/技能的名字**：
        # 私服的自定义职业、服务器名被机翻后往往面目全非。
        # 后面几个是所有 WoW 通用的缩写，一般不用动。
        # v1.1：这个列表参与缓存 key（哈希），改了会自动重翻旧消息。
        "protected_terms": [
            "ArdenWoW", "Ascension", "Runemaster", "Geo Primalist",
            "Alliance", "Horde", "PvP", "PvE", "DPS", "AOE",
            "LFG", "LFM", "BiS", "DoT", "HoT", "CC"
        ]
    },
    # 悬浮窗左上角「屏蔽」面板写入这里。被屏蔽的消息在**翻译前**就被丢掉，
    # 所以既不会显示，也不会产生 API 调用（省钱）。
    #   labels   ：按分类屏蔽，取值是显示标签，如 "公会"/"小队"/"密语"/"说话"…
    #   channels ：按具体公共频道屏蔽，取值是频道名，如 "2. Newcomers"；
    #              特殊值 "*" 表示屏蔽所有公共频道。
    "mute": {
        "labels": [],
        "channels": []
    }
}

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _read_user_env(name):
    """Read a Windows user-level environment variable straight from the
    registry.

    Setting a user env var does NOT update processes that are already running,
    so a bridge launched from a pre-existing shell or Explorer session would
    otherwise report "未设置 XXX" even though the variable is configured.
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value, _ = winreg.QueryValueEx(k, name)
        return str(value or "").strip()
    except Exception:
        return ""


def get_env(name):
    """Process environment first, then the Windows user registry."""
    return (os.getenv(name) or "").strip() or _read_user_env(name)


def resolve_provider(cfg):
    """Return (provider, model, api_key, base_url, extra_headers, extra_body)
    so every caller reads the same rules.

    v1.0.15：只剩智谱一路（走 OpenAI 兼容的 /chat/completions）。provider 字段
    仍然返回，因为它参与翻译缓存的 key —— 换模型/换端点要重翻，不能命中旧缓存。
    """
    compat = cfg.get("compat") or {}
    provider = (cfg.get("provider") or "zhipu").strip().lower() or "zhipu"
    model = (compat.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return (provider, model, effective_api_key(cfg),
            (compat.get("base_url") or ZHIPU_BASE_URL).strip() or ZHIPU_BASE_URL,
            compat.get("extra_headers") or {},
            compat.get("extra_body") or {})


def effective_api_key(cfg) -> str:
    """API Key 的**唯一**来源：Windows 用户环境变量。

    v1.0.16 起不再读 config.json —— 明文 Key 落盘是明确被否掉的做法。
    变量名由 compat.api_key_env 指定，默认 ZHIPU_API_KEY。
    """
    compat = cfg.get("compat") or {}
    env = (compat.get("api_key_env") or DEFAULT_API_KEY_ENV).strip() or DEFAULT_API_KEY_ENV
    return get_env(env)


def api_key_env_name(cfg) -> str:
    compat = cfg.get("compat") or {}
    return (compat.get("api_key_env") or DEFAULT_API_KEY_ENV).strip() or DEFAULT_API_KEY_ENV


def set_user_env(name, value):
    """把变量写进当前用户的注册表环境（HKCU\\Environment）。

    为什么不用 `setx`：那是个外部进程，会弹黑框、有 1024 字符截断的历史问题，
    而且在打包成 windowed EXE 之后根本没有控制台。直接写注册表更干净。

    写完顺手广播一次 WM_SETTINGCHANGE，让**新开的**程序（包括资源管理器、
    新终端）立刻看到；本进程自己不用等广播 —— get_env() 会直接读注册表。
    """
    name = (name or "").strip()
    if not name:
        return False
    value = str(value or "")
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            if value:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
    except Exception:
        return False
    # 本进程立刻生效，避免刚设完还要重启才认。
    try:
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
    except Exception:
        pass
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        res = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 3000, ctypes.byref(res))
    except Exception:
        pass          # 广播失败不影响功能（我们读的是注册表，不是缓存）
    return True


def migrate_plaintext_key():
    """把老版本留在 config.json 里的明文 Key 搬进环境变量，并从磁盘上抹掉。

    v1.0.15 及以前，管理界面填的 Key 会存进 compat.api_key。现在只允许放环境
    变量，所以首次启动时做一次迁移：**环境变量里已经有值就不覆盖**（尊重用户
    在系统里设的），然后无论有没有搬，都把配置文件里的那一份删掉。
    """
    try:
        cfg = load_config()
    except Exception:
        return False
    compat = cfg.get("compat") or {}
    plain = (compat.get("api_key") or "").strip()
    if not plain:
        return False
    env = api_key_env_name(cfg)
    moved = False
    if not get_env(env):
        moved = set_user_env(env, plain)
    compat.pop("api_key", None)
    try:
        save_config(cfg)          # save_config 自己也会再剥一遍，双保险
    except Exception:
        pass
    return moved or True


def is_local_base_url(base_url):
    return any(h in (base_url or "") for h in LOCAL_HOSTS)


# Transient conditions worth retrying: free tiers rate-limit aggressively, and
# a busy shared endpoint will hand back 5xx now and then.
RETRYABLE_MARKERS = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")


def is_retryable(err):
    s = str(err)
    if any(m in s for m in RETRYABLE_MARKERS):
        return True
    return "timed out" in s.lower() or "timeout" in s.lower()

EVENT_LABELS = {
    "CHAT_MSG_GUILD": "公会",
    "CHAT_MSG_OFFICER": "官员",
    "CHAT_MSG_PARTY": "小队",
    "CHAT_MSG_PARTY_LEADER": "小队",
    "CHAT_MSG_RAID": "团队",
    "CHAT_MSG_RAID_LEADER": "团队",
    "CHAT_MSG_RAID_WARNING": "团队",
    "CHAT_MSG_WHISPER": "密语",
    "CHAT_MSG_SAY": "说话",
    "CHAT_MSG_YELL": "喊话",
    "CHAT_MSG_BATTLEGROUND": "战场",
    "CHAT_MSG_BATTLEGROUND_LEADER": "战场",
    "CHAT_MSG_CHANNEL": "频道",
}

# 悬浮窗「屏蔽」面板里列出的分类，顺序即显示顺序。
MUTE_LABELS = ["公会", "小队", "团队", "密语", "说话", "喊话", "战场", "官员"]

def is_channel_event(event: str) -> bool:
    """判断标签段是「公共频道名」还是「WoW 事件名」。

    插件 v1.0.13 起：公共频道消息的第 4 段直接填**频道名**（如 "2. Newcomers"），
    其余事件仍填 CHAT_MSG_*。所以「不以 CHAT_MSG_ 开头」就等于「这是个频道名」。
    """
    return bool(event) and not event.startswith("CHAT_MSG_")

def event_label(event: str) -> str:
    """悬浮窗里方括号中显示的短标签。

    频道名原样显示；事件名映射成中文（未知事件退回原始名，绝不抛错）。
    """
    if is_channel_event(event):
        return event
    return EVENT_LABELS.get(event, event)

def is_muted(event: str, mute) -> bool:
    """该消息是否被用户屏蔽。屏蔽在翻译前生效，所以能省下 API 调用。"""
    if not mute:
        return False
    if is_channel_event(event):
        chans = mute.get("channels") or []
        return "*" in chans or event in chans
    return event_label(event) in (mute.get("labels") or [])

TRANSLATOR_INSTRUCTIONS = """You are a low-latency World of Warcraft chat translator.
Translate the supplied player chat into natural Simplified Chinese.
The source may be English, Japanese, Russian, slang, abbreviations, typos, or WoW jargon.
Text inside square brackets [...] is a linked item, quest, spell or achievement name;
translate it too, concisely, so the reader understands it.
Keep player names, addon names, server names, expansion names and protected placeholders unchanged.
Keep the tone short and conversational, like in-game chat.
Do not explain. Return only the translation.
"""

def load_config():
    """读取配置，并把新字段合并进来。

    v1.1（V11-06 第 4 条）：坏配置要给**可恢复提示 + 备份**，不能悄悄全量重置。
    """
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    raw = ""
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        cfg = json.loads(raw)
        if not isinstance(cfg, dict):
            raise ValueError("配置根节点不是对象")
    except Exception as e:
        # 配置坏了：备份原文件（保留用户手工改过的痕迹），再用默认值起步，
        # 并把这件事记下来，启动时提示用户 —— 不静默重置。
        backup = None
        try:
            if raw:
                backup = CONFIG_PATH.with_suffix(".json.bad")
                backup.write_text(raw, encoding="utf-8")
        except Exception:
            backup = None
        try:
            save_config(DEFAULT_CONFIG)
        except Exception:
            pass
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["_recovered_from_bad_config"] = {
            "backup": str(backup) if backup else "",
            "error": f"{type(e).__name__}: {e}",
        }
    # shallow/default merge
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
        elif isinstance(v, dict) and isinstance(cfg[k], dict):
            for sk, sv in v.items():
                cfg[k].setdefault(sk, sv)
    return cfg

def save_config(cfg):
    """写入 config.json。**写之前先剥掉任何明文 Key**（v1.0.16 硬要求）。

    放在这里而不是散在各个调用点：调用方太多（悬浮窗屏蔽面板、目录选择、
    管理界面…），只要有一处漏了，明文就会落盘。守住出口最可靠。
    """
    compat = cfg.get("compat")
    if isinstance(compat, dict):
        compat.pop("api_key", None)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def unesc(s: str) -> str:
    """还原插件 esc() 做的转义。

    v1.1：实现搬到 protocol.py（解析器要保证「每个字段只解一次」），这里保留
    同名薄封装，方便外部脚本/测试继续按老名字调用。
    """
    return _protocol.unesc(s)


def parse_payload(raw, *, source):
    """v1.1（V11-01）：一次性解析一条线上 payload。见 protocol.py。"""
    return _protocol.parse_payload(raw, source=source)


# v1.1：TranslationCache 搬到 cache.py（版本化 key + singleflight）。
# 老代码里 self.cache 的调用点改成 self.cache.get(key) / put(key, value) 形式，
# key 由 cache_key() 统一算出来。

# WoW chat-log lines look like "10/8 11:45:57.003  <text>".
_LOG_TS = re.compile(r"^(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})")

def parse_log_time(line: str):
    """Epoch seconds (local) for a chat-log line, or None if it has no stamp.

    Used by the startup catch-up: after a restart we rewind a little and only
    translate lines that are recent, so restarting the bridge mid-game no
    longer silently drops everything that arrived while it was down.
    """
    mt = _LOG_TS.match(line.lstrip("\ufeff"))
    if not mt:
        return None
    month, day, hh, mm, ss = (int(g) for g in mt.groups())
    # time.mktime normalises out-of-range fields instead of complaining
    # (month 13 rolls into next January), so range-check them ourselves --
    # otherwise garbage timestamps silently become plausible dates.
    if not (1 <= month <= 12 and 1 <= day <= 31 and hh <= 23 and mm <= 59 and ss <= 59):
        return None
    year = time.localtime().tm_year
    try:
        return time.mktime((year, month, day, hh, mm, ss, 0, 0, -1))
    except (ValueError, OverflowError):
        return None

def detect_language(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\u0400-\u04ff]", text):
        return "ru"
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if han > 0 and han >= max(2, latin // 2):
        return "zh"
    if latin > 0:
        return "en"
    return "other"

def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    return text

# WoW 的物品 / 任务 / 法术链接在聊天里长这样：
#   |cFFFFFF00|Hquest:453:25|h[Finding the Shadowy Figure]|h|r
# 直接丢给模型会翻出一堆乱码（"|c" 之类）。这里把标记剥掉，只留
# 玩家实际看到的文字：[Finding the Shadowy Figure]
_WOW_LITERAL_PIPE = "\x00"
def strip_wow_markup(text: str) -> str:
    if not text or "|" not in text:
        return text
    t = text.replace("||", _WOW_LITERAL_PIPE)          # 保护字面竖线
    t = re.sub(r"\|c[0-9a-fA-F]{6,8}", "", t)          # 颜色开始
    t = re.sub(r"\|H[^|]*\|h", "", t)                  # 链接开始
    t = re.sub(r"\|T[^|]*\|t", "", t)                  # 内嵌贴图
    t = t.replace("|h", "").replace("|r", "").replace("|n", "")
    t = t.replace(_WOW_LITERAL_PIPE, "|")
    return re.sub(r"\s+", " ", t).strip()

def protect_terms(text: str, terms):
    mapping = {}
    out = text
    # longest first avoids partial collisions
    for i, term in enumerate(sorted(set(terms), key=len, reverse=True)):
        if not term:
            continue
        pat = re.compile(re.escape(term), re.IGNORECASE)
        m = pat.search(out)
        if m:
            placeholder = f"ZXQTERM{i}QXZ"
            actual = m.group(0)
            out = pat.sub(placeholder, out)
            mapping[placeholder] = actual
    return out, mapping

def restore_terms(text: str, mapping):
    for placeholder, actual in mapping.items():
        text = text.replace(placeholder, actual)
    return text

# v1.1：TranslationCache 已迁移到 cache.py（版本化 key + singleflight），
# 由文件顶部的 import 直接提供。下面不再定义同名类，避免两套实现并存。

def http_json(url, headers, payload, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e
    except Exception as e:
        raise RuntimeError(str(e)) from e

# v1.0.15：openai_translate() / gemini_translate() 已删除 —— 现在只走智谱，
# 也就是下面这个 OpenAI 兼容的 /chat/completions 客户端。
def compat_translate(api_key, model, text, timeout, base_url, extra_headers=None, extra_body=None):
    """Any OpenAI-compatible /chat/completions endpoint.

    默认打智谱 GLM；同时覆盖 Groq、OpenRouter、DeepSeek、SiliconFlow、Ollama
    等所有讲 chat-completions 这套形状的服务端（改 config.json 的 base_url 即可）。
    注意这**不是** OpenAI 的 Responses API。
    """
    if not base_url:
        raise RuntimeError("base_url 未配置")
    if not model:
        raise RuntimeError("模型名未配置")
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": TRANSLATOR_INSTRUCTIONS},
            {"role": "user", "content": text},
        ],
        "temperature": 0.15,
        "max_tokens": 200,
        "stream": False,
    }
    if extra_body:
        payload.update(extra_body)
    obj = http_json(url, headers, payload, timeout)
    choices = obj.get("choices") or []
    if not choices:
        raise RuntimeError(f"兼容接口返回中没有 choices: {str(obj)[:300]}")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")
    if not content:
        # Reasoning models (GLM-4.5/4.7, DeepSeek-R1, ...) put their thinking in
        # reasoning_content and can burn the whole token budget there, leaving
        # content empty with finish_reason="length". That reads as a silent
        # failure, so say what actually happened.
        if message.get("reasoning_content"):
            raise RuntimeError(
                f"模型只输出了思考内容（finish_reason={choice.get('finish_reason')}），"
                f"token 预算被思考耗尽。请换用非推理模型（如 glm-4-flash-250414），"
                f"或在 compat.extra_body 里加大 max_tokens。")
        raise RuntimeError(f"兼容接口返回中没有文本: {str(choice)[:300]}")
    return content.strip()

def _candidate_logs_dirs(wow_path) -> list[Path]:
    """列出「这个客户端可能把 Logs 放哪」的全部候选，按可信度排序。

    为什么需要这个（v1.1.1 修「找不到 Logs\\」）
    ---------------------------------------------------------------------------
    WoW 客户端的目录布局**不止一种**。实测过的两个：

      A) 传统布局（ArdenWoW 等）：
           <root>\\Logs\\WoWChatLog.txt
           <root>\\Interface\\AddOns\\

      B) 启动器套壳布局（Grimfall 等，Electron 启动器 + resources 子目录）：
           <root>\\resources\\Wotlk\\Logs\\WoWChatLog.txt
           <root>\\resources\\Wotlk\\Interface\\AddOns\\

    旧实现只认 A，于是用户选了 B 的**任何一层**都找不到日志 —— 悬浮窗直接显示
    「未找到 Logs\\」，而实际上数据一条不少地在写。这是最难查的一类问题：
    节目单全对（插件在跑、payload 格式正确），只是取件地址错了。

    所以这里不再猜「唯一正确路径」，而是**把所有已知布局都列成候选**，
    谁真实存在就用谁。用户选 <root> 或 <root>\\resources\\Wotlk 都能work。
    """
    raw = str(wow_path or "").strip().strip('"')
    if not raw:
        return []
    root = Path(raw)
    candidates: list[Path] = [root / "Logs"]

    # 叠一层常见的启动器子目录。/resources/Wotlk 是 Grimfall/Ascension 这类
    # Electron 启动器最常见的形态；也顺带试几个大小写与近义名。
    for sub in ("resources/Wotlk", "resources/wotlk", "Resources/Wotlk",
                "Wotlk", "wotlk"):
        candidates.append(root / sub / "Logs")

    # 反过来：用户可能**已经**选到了 resources\Wotlk，那就要往上找一层，
    # 免得他从 <root>\resources\Wotlk 退出去才能选对。
    if root.name.lower() in ("wotlk", "wow", "client"):
        candidates.append(root.parent / "Logs")
        candidates.append(root.parent.parent / "Logs")

    # 去重、保持顺序（同一候选可能出现两次）。
    seen = set()
    out = []
    for c in candidates:
        k = str(c).lower()
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


# MissingFiles 的真实文件名不止一种。实测：
#   ArdenWoW  -> MissingFiles.txt
#   Grimfall  -> MissingFiles.log
# 只认 .txt 的话，通道 B（**唯一实时的那条**）会永远「尚未生成」。
MISSING_FILES_NAMES = ("MissingFiles.txt", "MissingFiles.log")


def find_missing_files(wow_path) -> Path | None:
    """在候选 Logs 目录里找 MissingFiles.txt / .log。找不到返回 None。"""
    for d in _candidate_logs_dirs(wow_path):
        for name in MISSING_FILES_NAMES:
            p = d / name
            if p.exists():
                return p
    return None


def find_chat_log(wow_path: str) -> Path | None:
    """在候选 Logs 目录里找 WoWChatLog.txt（自己处理路径，不递归）。

    注意：这里**不**递归遍历整个客户端目录 —— 客户端动辄几十 GB，
    递归会卡住启动。只用已知的布局候选，快且可预测。
    """
    for d in _candidate_logs_dirs(wow_path):
        for name in ("WoWChatLog.txt", "wowchatlog.txt"):
            p = d / name
            if p.exists():
                return p
    return None


def find_logs_dir(wow_path) -> Path | None:
    """返回第一个**真实存在**的 Logs 目录（用于「客户端目录已就绪」判定）。

    即使两个日志文件都还没生成，只要 Logs\\ 目录在，就说明路径选对了 ——
    这时该显示「等待数据」而不是「未找到 Logs\\」，后者会误导用户去重选目录。
    """
    for d in _candidate_logs_dirs(wow_path):
        if d.is_dir():
            return d
    return None


# ---------------------------------------------------------------------------
# 插件一键部署（v1.0.15）
#
# 目标：用户点一次「开始」就够了 —— 不用再手工把 ArdenTranslatorAI 文件夹拖进
# Interface\AddOns。EXE 里自带一份插件（PyInstaller --add-data），启动时按
# 「删旧 → 重建 → 写入」的顺序覆盖，所以永远不会出现「EXE 更新了但插件还是旧的」
# 这种最难查的错配。
# ---------------------------------------------------------------------------

def plugin_source_dir():
    """EXE 内置插件目录；开发态（直接跑 .py）回退到仓库里的那份。"""
    bundled = HERE / "plugin"
    if (bundled / ADDON_FILES[0]).exists():
        return bundled
    dev = Path(__file__).resolve().parent.parent / ADDON_NAME
    if (dev / ADDON_FILES[0]).exists():
        return dev
    return None


def addons_dir(wow_path) -> Path:
    """定位 `Interface\\AddOns`，兼容两种客户端布局（见 _candidate_logs_dirs）。

    关键：**已经装过插件的那个位置优先**。用户从 A 布局的客户端换到 B 布局时，
    两边可能都有 Interface\\AddOns；以「插件已经在那里」为准，避免把插件塞进
    一个游戏根本不读的目录（那样最难查：文件都在，游戏里就是不生效）。
    """
    raw = Path(str(wow_path).strip().strip('"'))
    candidates = [raw / "Interface" / "AddOns"]
    for sub in ("resources/Wotlk", "resources/wotlk", "Resources/Wotlk",
                "Wotlk", "wotlk"):
        candidates.append(raw / sub / "Interface" / "AddOns")
    if raw.name.lower() in ("wotlk", "wow", "client"):
        candidates.append(raw.parent / "Interface" / "AddOns")

    # 1) 已经装了插件的那个位置最可信。
    for c in candidates:
        if (c / ADDON_NAME).is_dir():
            return c
    # 2) 退而求其次：AddOns 目录存在、且里面有 Blizzard_* 官方插件
    #    （说明这是真客户端，不是我们凭空造的路径）。
    for c in candidates:
        if c.is_dir() and any(p.name.startswith("Blizzard_")
                              for p in c.iterdir() if p.is_dir()):
            return c
    # 3) 兜底：任意存在的 AddOns 目录。
    for c in candidates:
        if c.is_dir():
            return c
    # 4) 都没有：返回第一个候选，交给 deploy 的校验去报错。
    return candidates[0]


def check_wow_path(wow_path):
    """返回 (ok, message)。宽松但足够挡住「指错目录」这种最常见的失误。

    v1.1.1：不再要求 Interface 与 Logs 在**同一层**。两种布局都接受：
      A) <root>\\Interface + <root>\\Logs
      B) <root>\\resources\\Wotlk\\Interface + <root>\\resources\\Wotlk\\Logs
    判定标准改成「在这棵子树里能找到 Interface\\AddOns」。
    """
    raw = str(wow_path or "").strip().strip('"')
    if not raw:
        return False, "还没有选择客户端根目录。"
    root = Path(raw)
    if not root.exists():
        return False, f"目录不存在：{root}"
    if not root.is_dir():
        return False, f"这不是一个目录：{root}"

    # 从用户选的这一层往下找 Interface\AddOns（只找已知布局，不做全盘递归）。
    probe = [root] + [root / sub for sub in
                      ("resources/Wotlk", "resources/wotlk", "Resources/Wotlk",
                       "Wotlk", "wotlk")]
    for base in probe:
        if (base / "Interface" / "AddOns").is_dir():
            return True, ""
        if (base / "Interface").is_dir():
            # Interface 在但 AddOns 不在 —— 仍然算对的目录，AddOns 可以创建。
            return True, ""
    if root.name.lower() in ("wotlk", "wow", "client") and (root.parent / "Interface").is_dir():
        return True, ""

    return False, ("这不是魔兽世界客户端目录 —— 找不到 Interface 文件夹。\n"
                   f"你选的是：{root}\n"
                   "请选到包含 Interface（和 Logs）的那一层。\n"
                   "提示：有些启动器（如 Grimfall）会放在 "
                   "resources\\Wotlk\\ 下面，选到那一层也可以。")


def _read_toc_version(path: Path) -> str:
    """从 .toc 里读 ## Version:（校验用）。读不到返回 ""。"""
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if s.startswith("## Version:"):
                return s.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def deploy_addon(wow_path):
    """把内置插件部署到 wow_path\\Interface\\AddOns\\ArdenTranslatorAI。

    v1.1（V11-06 第 1 条）：改成「校验 → 同卷临时目录写入 → 校验 → 备份 →
    原子替换 → 失败自动恢复」。旧实现是「先 rmtree 再重建」，中途断电/复制失败
    就会留下一个**空目录**，游戏加载到半个插件 —— 比没装更糟。

    步骤：
      1. 校验 wow_path（含 Interface）。
      2. 在 **AddOns 同卷**下建临时目录（保证 rename 是原子操作，跨盘 rename 不行），
         把源文件全写进去。
      3. 校验临时目录里的 .lua/.toc 齐全、能读出 .toc 版本。
      4. 把**现有**插件目录改名为 .bak-<时间戳>（而不是直接删）。这样即使用户
         手工在里面加了东西，也留着备份。
      5. 临时目录 rename 成正式目录（原子）。
      6. 全流程任一步失败 -> 把 .bak 改回正式目录，恢复原状。

    返回 (ok, message, dest)。
    """
    ok, why = check_wow_path(wow_path)
    if not ok:
        return False, why, None

    src = plugin_source_dir()
    if src is None:
        return False, ("找不到内置插件文件。\n"
                       "如果你是直接跑 .py 源码：确认仓库里的 ArdenTranslatorAI\\ "
                       "文件夹和 WindowsBridge\\ 在同一层。"), None
    missing = [f for f in ADDON_FILES if not (src / f).exists()]
    if missing:
        return False, f"内置插件不完整，缺少：{', '.join(missing)}\n（来源：{src}）", None

    dest = addons_dir(wow_path) / ADDON_NAME
    # 安全闸：只允许删/写「.../Interface/AddOns/ArdenTranslatorAI」这一条路径。
    # 手滑把 wow_path 指到别处时，也绝不会去动一个不相关的目录。
    if dest.name != ADDON_NAME or dest.parent.name != "AddOns":
        return False, f"内部错误：目标路径异常，已中止（{dest}）", None

    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = dest.parent / (ADDON_NAME + ".new-" + stamp)
    backup = dest.parent / (ADDON_NAME + ".bak-" + stamp)
    moved_old = False

    def _cleanup(path):
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _cleanup(tmp)

        # ---- 2. 写进同卷临时目录 ----
        tmp.mkdir(parents=True)
        copied = []
        for f in sorted(src.iterdir()):
            if f.is_file():
                shutil.copyfile(f, tmp / f.name)
                copied.append(f.name)

        # ---- 3. 校验临时目录 ----
        for f in ADDON_FILES:
            if not (tmp / f).exists():
                raise RuntimeError(f"临时目录校验失败：{f} 不存在")
        new_ver = _read_toc_version(tmp / "ArdenTranslatorAI.toc")
        if not new_ver:
            raise RuntimeError("临时目录校验失败：.toc 里读不到版本号")

        # ---- 4. 备份现有插件（改名，不删） ----
        if dest.exists():
            _cleanup(backup)
            dest.rename(backup)
            moved_old = True

        # ---- 5. 原子替换 ----
        tmp.rename(dest)

        # 替换后再校验一次（极端情况下 rename 成功但内容不对，提前发现）。
        for f in ADDON_FILES:
            if not (dest / f).exists():
                raise RuntimeError(f"写入后校验失败：{f} 不存在")

        # 成功：删掉备份（只在写完之后删，前面删了就失去回滚能力）。
        if moved_old:
            _cleanup(backup)
        return True, (f"插件已更新到 v{new_ver}（{len(copied)} 个文件）\n{dest}"), dest

    except Exception as e:
        # ---- 6. 回滚 ----
        _cleanup(tmp)
        try:
            if moved_old and backup.exists():
                if dest.exists():
                    _cleanup(dest)
                backup.rename(dest)
            elif moved_old and not backup.exists() and not dest.exists():
                pass
        except Exception:
            pass
        return False, (f"部署插件失败（已尝试恢复旧版本）："
                       f"{type(e).__name__}: {e}"), None


class Overlay:
    def __init__(self, root, cfg, app=None):
        self.root = root
        self.cfg = cfg
        self.app = app          # 供左上角设置面板读写屏蔽配置、列出已发现的频道
        self._settings_win = None
        ov = cfg["overlay"]
        root.title(f"{APP_NAME} {VERSION}")
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", float(ov.get("alpha", 0.88)))
        except Exception:
            pass
        root.geometry(f'{ov["width"]}x{ov["height"]}+{ov["x"]}+{ov["y"]}')
        root.configure(bg="#111111")
        root.overrideredirect(True)

        self.header = tk.Label(root, text=f"{APP_NAME}  v{VERSION}   (Ctrl+H 隐藏/显示 · Ctrl+Q 退出)",
                               anchor="w", fg="#7fd7ff", bg="#111111",
                               font=("Segoe UI", 9, "bold"))
        # 左边留出 30px 给左上角的 ⚙ 设置按钮，否则会压住标题文字。
        self.header.pack(fill="x", padx=(30, 8), pady=(5, 0))

        # 左上角设置按钮：像游戏里那样按频道屏蔽 / 解开屏蔽。
        self.settings_btn = tk.Label(root, text="\u2699", fg="#7fd7ff", bg="#111111",
                                     font=("Segoe UI", 11), cursor="hand2")
        self.settings_btn.place(x=7, y=3, anchor="nw")
        self.settings_btn.bind("<Button-1>", lambda e: self.open_settings())
        self.settings_btn.bind("<Enter>", lambda e: self.settings_btn.configure(fg="#ffffff"))
        self.settings_btn.bind("<Leave>", lambda e: self.settings_btn.configure(fg="#7fd7ff"))

        # Live tail status. Without this the overlay is silent both when nobody
        # is chatting and when the pipeline is broken, which is impossible to
        # tell apart -- the single most confusing failure mode of this design.
        self.status = tk.Label(root, text="正在初始化…", anchor="w", fg="#6f8fa0",
                               bg="#111111", font=("Consolas", 8))
        self.status.pack(fill="x", padx=8, pady=(1, 0))

        # Borderless windows have no system close button and do not show up in
        # the taskbar, so provide an explicit one.
        self.close_btn = tk.Label(root, text="\u00d7", fg="#ff8b8b", bg="#111111",
                                  font=("Segoe UI", 12, "bold"), cursor="hand2")
        self.close_btn.place(relx=1.0, x=-8, y=3, anchor="ne")
        self.close_btn.bind("<Button-1>", lambda e: self.root.destroy())

        self.text = tk.Text(root, wrap="word", state="disabled", relief="flat", borderwidth=0,
                            bg="#111111", fg="#f2f2f2",
                            insertbackground="#f2f2f2",
                            font=("Microsoft YaHei UI", int(ov.get("font_size", 13))))
        self.text.pack(fill="both", expand=True, padx=8, pady=(2, 7))
        self.text.tag_configure("meta", foreground="#8fbed8")
        self.text.tag_configure("err", foreground="#ff8b8b")
        self.text.tag_configure("cache", foreground="#9b9b9b")

        self.drag_x = 0
        self.drag_y = 0
        self.header.bind("<Button-1>", self.start_drag)
        self.header.bind("<B1-Motion>", self.do_drag)
        root.bind("<Escape>", lambda e: self.hide())
        root.bind("<Control-h>", lambda e: self.toggle())
        root.bind("<Control-q>", lambda e: self.root.destroy())
        self.visible = True

    def start_drag(self, e):
        self.drag_x = e.x
        self.drag_y = e.y

    def do_drag(self, e):
        x = self.root.winfo_x() + e.x - self.drag_x
        y = self.root.winfo_y() + e.y - self.drag_y
        self.root.geometry(f"+{x}+{y}")

    def hide(self):
        self.root.withdraw()
        self.visible = False

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.visible = True

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    def add(self, sender, channel, original, translation, cached=False):
        self.text.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.text.insert("end", f"[{stamp}] [{channel}] {sender}: ", "meta")
        self.text.insert("end", translation + ("  [缓存]" if cached else "") + "\n")
        self.text.see("end")
        # keep bounded
        max_lines = int(self.cfg["overlay"].get("max_lines", 7)) * 3
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > max_lines:
            self.text.delete("1.0", f"{lines-max_lines}.0")
        self.text.configure(state="disabled")

    def error(self, msg):
        self.text.configure(state="normal")
        self.text.insert("end", f"[错误] {msg}\n", "err")
        self.text.see("end")
        self.text.configure(state="disabled")

    def settings_open(self):
        return self._settings_win is not None and self._settings_win.winfo_exists()

    def settings_stale(self, chans):
        """面板开着时，若又发现了新频道，就重建一次列表（否则新频道没处勾）。"""
        return self.settings_open() and set(chans) != getattr(self, "_settings_snapshot", set())

    def open_settings(self):
        """左上角 ⚙ —— 按频道「屏蔽 / 解开屏蔽」。

        屏蔽在桥接端的**翻译前**生效：被屏蔽的频道既不显示、也不送去翻译，
        所以能实打实省下 API 额度。改动立刻写进 config.json，重启后依然有效。
        """
        if self.app is None:
            return
        if self.settings_open():
            self._refresh_settings()
            self._settings_win.lift()
            self._settings_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("频道设置")
        win.configure(bg="#1b1b1b")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        self._settings_vars = {}
        self._refresh_settings()

    def _refresh_settings(self):
        win = self._settings_win
        if win is None or not win.winfo_exists():
            return
        for w in win.winfo_children():
            w.destroy()

        mute = self.app.cfg.setdefault("mute", {})
        off_labels = set(mute.get("labels") or [])
        off_chans = set(mute.get("channels") or [])
        self._settings_vars = {}

        head = ("Microsoft YaHei UI", 10, "bold")
        body = ("Microsoft YaHei UI", 9)
        cb_kw = dict(bg="#1b1b1b", fg="#e8e8e8", selectcolor="#2a2a2a",
                     activebackground="#1b1b1b", activeforeground="#ffffff",
                     highlightthickness=0, bd=0, font=body)

        tk.Label(win, text="按分类屏蔽", fg="#7fd7ff", bg="#1b1b1b",
                 font=head).pack(anchor="w", padx=14, pady=(12, 4))
        grid = tk.Frame(win, bg="#1b1b1b")
        grid.pack(anchor="w", padx=14)
        for i, lab in enumerate(MUTE_LABELS):
            v = tk.BooleanVar(value=(lab in off_labels))
            self._settings_vars[("label", lab)] = v
            tk.Checkbutton(grid, text=lab, variable=v, command=self._save_settings,
                           **cb_kw).grid(row=i // 4, column=i % 4,
                                         sticky="w", padx=(0, 12))

        tk.Label(win, text="公共频道", fg="#7fd7ff", bg="#1b1b1b",
                 font=head).pack(anchor="w", padx=14, pady=(14, 4))
        cbox = tk.Frame(win, bg="#1b1b1b")
        cbox.pack(anchor="w", padx=14)

        v_all = tk.BooleanVar(value=("*" in off_chans))
        self._settings_vars[("channel", "*")] = v_all
        tk.Checkbutton(cbox, text="全部公共频道", variable=v_all,
                       command=self._save_settings, fg="#ffd479",
                       bg="#1b1b1b", selectcolor="#2a2a2a",
                       activebackground="#1b1b1b", activeforeground="#ffffff",
                       highlightthickness=0, bd=0, font=body).pack(anchor="w")

        chans = sorted(self.app.seen_channels)
        self._settings_snapshot = set(chans)
        if chans:
            for ch in chans:
                v = tk.BooleanVar(value=(ch in off_chans))
                self._settings_vars[("channel", ch)] = v
                tk.Checkbutton(cbox, text=ch, variable=v, command=self._save_settings,
                               **cb_kw).pack(anchor="w")
        else:
            tk.Label(cbox, text="（还没收到过公共频道消息）", fg="#7a7a7a",
                     bg="#1b1b1b", font=body).pack(anchor="w")

        tk.Label(win, text="取消勾选 = 屏蔽：该频道的消息不再翻译，也不再显示（省 API 额度）",
                 fg="#9b9b9b", bg="#1b1b1b", font=("Microsoft YaHei UI", 8),
                 wraplength=340, justify="left").pack(anchor="w", padx=14, pady=(12, 4))
        tk.Button(win, text="关闭", command=win.destroy, bg="#2a2a2a", fg="#e8e8e8",
                  activebackground="#3a3a3a", activeforeground="#ffffff",
                  relief="flat", font=body, padx=16).pack(pady=(2, 12))

    def _save_settings(self):
        """把面板勾选写回 config.json —— 立即生效，重启后依然有效。"""
        if self.app is None:
            return
        labels, channels = [], []
        for (kind, name), v in self._settings_vars.items():
            if v.get():
                (labels if kind == "label" else channels).append(name)
        self.app.cfg["mute"] = {"labels": sorted(labels), "channels": sorted(channels)}
        save_config(self.app.cfg)

    def set_status(self, text):
        self.status.configure(text=text)

class App:
    """悬浮窗 + 后台管道。

    v1.1 的数据流（替换掉旧的「每条消息起一个线程」）：

        tail_loop(A)  ─┐
                       ├─ parse_payload ─ filter ─ dedupe ─> BoundedJobQueue ─> worker×2
        miss_loop(B)  ─┘                                             │
                                                                     └─ cache/singleflight ─ HTTP

    只有 UI 更新走 self.uiq，且**只**在 Tkinter 主线程（pump_ui）里消费。
    """

    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.overlay = Overlay(root, self.cfg, self)
        self.cache = TranslationCache(CACHE_PATH)
        # inflight 合并：同内容同时到达只发一次 HTTP（V11-03）。
        self.flight = SingleFlight()
        self.uiq = queue.Queue()
        self.stop = threading.Event()
        # 见过的公共频道名（"已有的频道"），左上角屏蔽面板据此列出可屏蔽项。
        self.seen_channels = set()

        tr = self.cfg["translation"]
        self.workers = max(1, min(4, int(tr.get("max_workers", 2))))
        self.queue_capacity = max(1, int(tr.get("queue_capacity", 100)))

        # v1.1（V11-04）：两通道共用同一个去重门（跨通道去重靠它）。
        self.deduper = _dedupe.Deduper(lru_capacity=20000, session_capacity=16)

        # v1.1（V11-02）：有界队列 + 固定 worker。
        self.jobs = _jobs.BoundedJobQueue(
            maxsize=self.queue_capacity,
            max_workers=self.workers,
            on_job=self.run_job,
        )

        # 通道健康状态机（V12-03 骨架）。
        self.health = {
            "A": _transport.ChannelHealth("通道A(聊天日志)"),
            "B": _transport.ChannelHealth("通道B(MissingFiles)"),
        }
        # v1.1.1：状态的起点由「Logs 目录在不在」决定，而不是等第一条消息。
        # 否则用户明明选对了目录、只是还没进游戏，状态行会一直显示
        # 「未找到 Logs\」把他引向重选目录 —— 那句提示本身是错的。
        _logs_dir = find_logs_dir(self.cfg.get("wow_path", ""))
        self.logs_dir_found = _logs_dir is not None
        self.health["A"].set_configured(self.logs_dir_found)
        self.health["B"].set_configured(self.logs_dir_found)

        # 延迟统计（V12-06 骨架）。桥接侧：入队 -> 展示。
        self.lat = {
            "queue": _diag.LatencyTracker(),
            "translate": _diag.LatencyTracker(),
            "total": _diag.LatencyTracker(),
        }

        # Counters behind the overlay's status line.
        self.stat = {
            "size": 0,
            "marker": 0,       # log lines carrying our bridge marker
            "accepted": 0,     # parsed, deduped, sent to the translator
            "translated": 0,
            "errors": 0,
            "muted": 0,        # 被屏蔽面板挡下、未送去翻译的条数
            "replayed": 0,     # recovered by the startup catch-up
            "grew_at": time.time(),
            # 通道 B（MissingFiles）的独立计数，两条通道的健康状况要分开看
            "miss_size": 0,
            "miss_marker": 0,
            "miss_grew_at": time.time(),
            # 解析拒收/丢弃（V11-01、V11-02 要求可观测）
            "parse_rejected": 0,
            "dropped": 0,
        }
        self._last_status = 0.0
        self.catching_up = False
        self.catchup_cutoff = 0.0
        self.startup_size = 0

        # 通道 B 的状态
        self.miss_path = None
        # 上一次整读 MissingFiles 的内容指纹（同大小但内容变化也要能发现）。
        self._miss_fp = None

        self.log_path = find_chat_log(self.cfg.get("wow_path", ""))
        if self.log_path:
            self.start_tailer()
        elif (self.cfg.get("wow_path") or "").strip():
            # A path is already configured, so the log files just do not exist
            # yet (chat logging off, or the game has not run since). tail_loop
            # tolerates a missing file, so watch the expected path instead of
            # nagging the user with a folder picker on every launch.
            #
            # v1.1.1：**不能**再硬写 "Logs/WoWChatLog.txt"。先用布局解析器
            # 找真实的 Logs 目录（Grimfall 这类启动器套壳布局是
            # resources\Wotlk\Logs）；连目录都还没生成才回退到默认拼法。
            logs_dir = find_logs_dir(self.cfg.get("wow_path", ""))
            if logs_dir is None:
                cands = _candidate_logs_dirs(self.cfg.get("wow_path", ""))
                logs_dir = cands[0] if cands else Path(self.cfg["wow_path"]) / "Logs"
            self.log_path = logs_dir / "WoWChatLog.txt"
            self.start_tailer()
        else:
            root.after(300, self.choose_wow_path)

        # worker 最后启动：先把路径/状态都准备好，避免 worker 拿到任务时
        # self.log_path 还是 None。
        self.jobs.start()

        # 配置是从坏文件恢复出来的 -> 明确告诉用户，而不是静默重置。
        rec = self.cfg.get("_recovered_from_bad_config")
        if rec:
            self.overlay.error("配置文件损坏，已用默认值启动。")
            self.overlay.add("system", "配置", "",
                             f"原文件已备份到：{rec.get('backup') or '(备份失败)'}")

        self.check_api_key()
        root.after(100, self.pump_ui)

    def check_api_key(self):
        provider, model, key, base_url, _, _ = resolve_provider(self.cfg)
        env = api_key_env_name(self.cfg)

        def fatal(msg, env_hint=None):
            """配置硬伤：既要写进悬浮窗，也要弹窗。

            换新机器时最常见的就是「忘了填 Key」，而悬浮窗只有巴掌大、还容易被
            游戏窗口盖住 —— 光写在那儿用户根本注意不到，只会觉得「装了没用」。
            所以这里额外弹一个模态框。用 root.after 推迟，等悬浮窗先画出来再弹，
            否则会出现「弹窗背后的窗口还没绘制」的观感。
            """
            self.overlay.error(msg)
            lines = [msg]
            if env_hint:
                lines += [
                    "",
                    "修复方法（二选一）：",
                    "1) 重新运行本程序，在管理界面里填好 ZHIPU_API_KEY 再点开始；",
                    "2) 按 Win+R 输入 powershell 回车，粘贴下面这行执行",
                    "   （把 sk-xxx 换成你自己的 Key）：",
                    "",
                    f'[Environment]::SetEnvironmentVariable("{env_hint}","sk-xxx","User")',
                    "",
                    "设完后重启本程序即可，不需要重启电脑。",
                ]
            text = "\n".join(lines)
            self.root.after(500, lambda: messagebox.showwarning(APP_NAME, text))

        if not base_url:
            fatal("base_url 未配置，无法调用翻译接口。")
            return
        if not model:
            fatal("模型名未配置，无法调用翻译接口。")
            return
        if not key and not is_local_base_url(base_url):
            fatal(f"未检测到 API Key（{env}），翻译会全部失败。", env)
            return
        self.overlay.add("system", "启动", "",
                         f"model={model}"
                         + ("  [本地，无需 Key]" if is_local_base_url(base_url) else ""))
        self.overlay.add("system", "监听", "",
                         str(self.log_path) if self.log_path else "(尚未确定日志路径)")
        self.overlay.add("system", "提示", "",
                         "底部若显示「无新行」→ 游戏里执行 /atai test 验证链路")

    def choose_wow_path(self):
        # 只在「配置里根本没有 wow_path」时才走到这儿（正常流程是管理界面里选好
        # 再点开始）。不要写死 ArdenWoW —— 任何 WoW 3.3.5 风格客户端都适用。
        messagebox.showinfo(APP_NAME, "还没有设置魔兽世界客户端根目录。\n"
                                      "请选择包含 Interface 与 Logs 的那一层。\n"
                                      "（有些启动器如 Grimfall 放在 resources\\Wotlk 下面，"
                                      "选到那一层也可以。）")
        chosen = filedialog.askdirectory(title="选择魔兽世界客户端目录（含 Interface 与 Logs）")
        if not chosen:
            self.overlay.error("没有选择客户端目录。重新启动后可再次选择。")
            return
        self.cfg["wow_path"] = chosen
        save_config(self.cfg)
        self.log_path = find_chat_log(chosen)
        if not self.log_path:
            self.overlay.error("未找到 WoWChatLog.txt，请在游戏里执行 /atai on 并说一句/收到一条聊天。")
            # still monitor expected path, file may be created later
            logs_dir = find_logs_dir(chosen)
            if logs_dir is None:
                cands = _candidate_logs_dirs(chosen)
                logs_dir = cands[0] if cands else Path(chosen) / "Logs"
            self.log_path = logs_dir / "WoWChatLog.txt"
        self.start_tailer()

    def start_tailer(self):
        threading.Thread(target=self.tail_loop, daemon=True).start()
        # 通道 B 就在聊天日志旁边，但**文件名不一定叫 .txt**：
        #   ArdenWoW -> MissingFiles.txt
        #   Grimfall -> MissingFiles.log
        # 先按已知名字找；都还没有就按聊天日志所在目录 + 首个候选名兜底
        # （客户端第一次进游戏时会自己建）。
        found = find_missing_files(self.cfg.get("wow_path", ""))
        if found:
            self.miss_path = found
        elif self.log_path:
            self.miss_path = self.log_path.parent / MISSING_FILES_NAMES[0]
        if self.miss_path:
            threading.Thread(target=self.miss_loop, daemon=True).start()

    def miss_loop(self):
        """通道 B：Logs\\MissingFiles.txt —— 插件 SetTexture 写进来的实时通道。

        为什么不能像聊天日志那样按偏移量 tail：这个文件是**集合**语义，
        客户端每次都整体重写（实测大小会涨也会跌：9882 -> 9016 字节），
        偏移量随时会指到中间。所以只能整读。

        v1.1（V11-04）关键改动：**不再**「首读时光记下已见集合、之后整文件去重」。
        旧做法在 seen_miss 被 clear() 之后会把历史记录整批重放。现在：
          * 每一轮都整读（不变），但每条记录先过 Deduper 的**会话水位**——
            属于旧会话或 seq 倒退的行在进入去重表之前就被丢掉；
          * 去重表是有界 LRU，永不整体清空；
          * 用内容指纹判断「文件变了没有」，因为同大小也可能换了内容
            （V12-03 明确要求不能只看 size）。
        """
        p = self.miss_path
        while not self.stop.is_set():
            try:
                if p is not None and p.exists():
                    # 前后 stat 比对：拿到一个稳定快照，避免读到写一半的内容。
                    snap = self._stable_read(p)
                    if snap is None:
                        time.sleep(0.4)
                        continue
                    text, fp, size = snap
                    if size != self.stat["miss_size"]:
                        self.stat["miss_size"] = size
                        self.stat["miss_grew_at"] = time.time()
                        self.health["B"].note_file_change()
                    # 同大小但内容变了（排序变化 / 同长度替换）也要处理。
                    if fp != self._miss_fp:
                        self._miss_fp = fp
                        self.health["B"].note_file_change()
                        for raw in text.splitlines():
                            self.handle_miss_line(raw)
                    self.health["B"].set_configured(True)
            except Exception as e:
                self.health["B"].note_error(f"{type(e).__name__}: {e}")
                self.uiq.put(("error", f"读取 MissingFiles 失败: {e}"))
                time.sleep(1.0)
            time.sleep(0.4)

    @staticmethod
    def _stable_read(path, tries: int = 3):
        """稳定读取：连读两次，size 与 mtime 都没变才认。

        返回 (text, fingerprint, size) 或 None（还在写，下一轮再试）。
        这样「读到写了一半的文件」不会被当成内容变化。
        """
        prev = None
        for _ in range(max(1, tries)):
            try:
                st1 = path.stat()
            except OSError:
                return None
            try:
                raw = path.read_bytes()
            except OSError:
                return None
            try:
                st2 = path.stat()
            except OSError:
                return None
            if st1.st_size == st2.st_size == len(raw) and st1.st_mtime == st2.st_mtime:
                text = raw.decode("utf-8", errors="replace")
                fp = hashlib.sha1(raw).hexdigest()
                return text, fp, len(raw)
            prev = (st1, st2)
            time.sleep(0.05)
        # 三次都没稳定 -> 先返回最后一次读到的内容，别把通道卡死。
        if prev is not None and raw is not None:
            text = raw.decode("utf-8", errors="replace")
            return text, hashlib.sha1(raw).hexdigest(), len(raw)
        return None

    def handle_miss_line(self, line):
        """处理 MissingFiles.txt 的一行；返回 True 表示这是一条新 payload。

        客户端会给贴图名自动补 .blp / .tga 各记一行，所以同一 payload 会来两次，
        去掉后缀后靠去重门挡第二次。抽成独立方法是为了能离线测试。
        """
        raw = line.strip()
        if not raw:
            return False
        if not raw.startswith(MISS_MARKER):
            # 这个文件里本来就有大量无关记录（字体名、listfile 之类），
            # 只统计带 marker 的行，避免被噪音刷屏。
            return False
        self.health["B"].note_line(has_marker=True)
        payload = _protocol.strip_miss_suffix(raw)
        return self._ingest(payload, source="miss")

    def tail_loop(self):
        p = self.log_path
        pos = 0
        first_open = True
        # Incremental decoder keeps multi-byte characters intact even when a
        # single character straddles two reads. "replace" means a genuinely
        # broken byte never raises.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""   # trailing partial line, carried to the next read
        while not self.stop.is_set():
            try:
                if not p.exists():
                    time.sleep(0.4)
                    continue
                size = p.stat().st_size
                if size != self.stat["size"]:
                    self.stat["size"] = size
                    self.stat["grew_at"] = time.time()
                if first_open:
                    first_open = False
                    # Seed "last growth" from the file's own mtime, otherwise a
                    # freshly started bridge always claims the log is current.
                    try:
                        self.stat["grew_at"] = min(p.stat().st_mtime, time.time())
                    except OSError:
                        pass
                    # Recover the last few minutes instead of jumping to EOF.
                    replay_s = int(self.cfg["translation"].get("startup_replay_seconds", 600) or 0)
                    if replay_s > 0 and size > 0:
                        self.catchup_cutoff = time.time() - replay_s
                        self.startup_size = size
                        self.catching_up = True
                        pos = max(0, size - 65536)
                    else:
                        pos = size
                if size < pos:
                    # file was truncated or recreated
                    pos = 0
                    decoder.reset()
                    pending = ""
                if size > pos:
                    with p.open("rb") as f:
                        f.seek(pos)
                        chunk = f.read(size - pos)
                        pos = f.tell()
                    text = pending + decoder.decode(chunk)
                    # Only hand complete lines to the parser; a line still being
                    # written must not be treated as finished or it gets cut off.
                    if text.endswith("\n"):
                        pending = ""
                    else:
                        cut = text.rfind("\n")
                        if cut == -1:
                            pending = text
                            text = ""
                        else:
                            pending = text[cut + 1:]
                            text = text[:cut + 1]
                    for line in text.splitlines():
                        self.handle_log_line(line)
                    if self.catching_up and pos >= self.startup_size:
                        self.catching_up = False
                        n = self.stat["replayed"]
                        self.uiq.put(("info",
                                      f"启动回补：找回 {n} 条近期消息（更早的已跳过）"
                                      if n else
                                      "启动完成：日志里暂无近期消息，等新消息即可"))
            except Exception as e:
                self.health["A"].note_error(f"{type(e).__name__}: {e}")
                self.uiq.put(("error", f"读取聊天日志失败: {e}"))
                time.sleep(1.0)
            time.sleep(0.10)

    def handle_log_line(self, line):
        if MARKER not in line:
            return
        self.health["A"].note_line(has_marker=True)
        if self.catching_up:
            # Lines older than the catch-up window arrived while the bridge was
            # not running; replaying a whole session would be worse than
            # dropping it, so only recent ones get translated.
            ts = parse_log_time(line)
            if ts is None or ts < self.catchup_cutoff:
                return
            self.stat["replayed"] += 1
        # 注意：**不要**在这里再 stat["marker"] += 1。
        # v1.1 起 _ingest() 才是计数的唯一出口（它知道解析是否成功）。
        # 此处若也加一次，通道 A 的每一行会被计两次，状态行数字直接翻倍
        # —— 这正是写自检时被 [9] 的 "marker counter moved" 抓到的。
        i = line.find(MARKER)
        self.dispatch_payload(line[i:])

    def dispatch_payload(self, payload, require_six=False):
        """兼容入口：解析 + 走完整管道。两条通道共用。

        v1.1（V11-01）：解析逻辑整体搬到 protocol.parse_payload()，
        这里只剩「推断 source 后转调」这一层。`require_six` 参数保留是为了
        不破坏旧调用点/旧测试，但语义已经由 source 决定（通道 B 恒为 6 段）。
        """
        source = "miss" if require_six else self._guess_source(payload)
        return self._ingest(payload, source=source)

    @staticmethod
    def _guess_source(payload):
        if isinstance(payload, str) and payload.startswith(MISS_MARKER):
            return "miss"
        return "chatlog"

    def _ingest(self, payload, *, source):
        """解析 → 过滤 → 去重 → 入队。**绝不阻塞**（V11-02 硬要求）。

        只做轻量工作：重活（HTTP）在 worker 线程里，UI 更新在 Tkinter 主线程里。
        """
        msg = parse_payload(payload, source=source)
        if msg is None:
            # 坏包只计数，不抛异常 —— tail/miss 线程绝不能被一条脏行杀掉。
            self.stat["parse_rejected"] += 1
            self.health["B" if source == "miss" else "A"].note_invalid()
            return False

        if source == "miss":
            self.stat["miss_marker"] += 1
        else:
            self.stat["marker"] += 1

        # 记下见过的公共频道名，供左上角「屏蔽」面板列出（"已有的频道"）。
        if msg.is_channel:
            self.seen_channels.add(msg.label)

        # 屏蔽判定放在**最前面**：既不显示、也不翻译 —— 省一次 API 调用。
        if is_muted(msg.label, self.cfg.get("mute")):
            self.stat["muted"] += 1
            self.health["B" if source == "miss" else "A"].note_rejected()
            return False

        text = normalize_text(strip_wow_markup(msg.text))
        if not text:
            return False

        # 去重（会话水位 + 有界 LRU + 跨通道）。
        if not self.deduper.seen(msg, source=source):
            self.health["B" if source == "miss" else "A"].note_rejected()
            return False

        lang = detect_language(text)
        if lang == "zh" and self.cfg["translation"].get("skip_chinese", True):
            return False
        if lang not in ("en", "ja", "ru"):
            return False

        self.stat["accepted"] += 1
        self.health["B" if source == "miss" else "A"].note_valid()

        tr = self.cfg["translation"]
        ttl = (float(tr.get("queue_ttl_seconds", 30))
               if _jobs.priority_for(msg.label) >= _jobs.PRIORITY_CHANNEL
               else float(tr.get("queue_ttl_seconds_other", 120)))
        job = _jobs.TranslationJob(
            message_id="%s:%s:%s" % (msg.protocol, msg.session or "-", msg.seq),
            session=msg.session,
            seq=msg.seq,
            source=source,
            channel=msg.label,
            sender=msg.sender,
            text=text,
            enqueued_at=time.time(),
            priority=_jobs.priority_for(msg.label),
            # 入队时的配置快照：换 Key/模型/目标语言时不影响已排队的旧任务
            # （V12-05 要求，v1.1 先把快照带上）。
            settings_snapshot=self._settings_snapshot(),
            ttl_seconds=ttl,
        )
        ok = self.jobs.put(job)
        if not ok:
            self.stat["dropped"] += 1
        return ok

    def _settings_snapshot(self):
        provider, model, key, base_url, eh, eb = resolve_provider(self.cfg)
        tr = self.cfg["translation"]
        return {
            "provider": provider, "model": model, "key": key,
            "base_url": base_url, "extra_headers": dict(eh or {}),
            "extra_body": dict(eb or {}),
            "target": tr.get("target", "Simplified Chinese"),
            "protected_terms": list(tr.get("protected_terms", [])),
        }

    # ---- worker 侧（V11-02 / V11-03） ----
    def run_job(self, job: _jobs.TranslationJob):
        """在固定 worker 线程里执行一次翻译。异常一律吞掉并计数 ——
        一条消息失败不能让 worker 退出。"""
        t_enq = job.enqueued_at
        snap = job.settings_snapshot or self._settings_snapshot()
        provider = snap.get("provider", "zhipu")
        model = snap.get("model", DEFAULT_MODEL)
        key = snap.get("key", "")
        base_url = snap.get("base_url", ZHIPU_BASE_URL)
        eh = snap.get("extra_headers") or {}
        eb = snap.get("extra_body") or {}
        terms = snap.get("protected_terms") or []

        text = job.text
        ckey = cache_key(provider=provider, base_url=base_url, model=model,
                         target=snap.get("target"), text=text, terms=terms)

        # ---- 缓存命中 ----
        cached = self.cache.get(ckey)
        if cached:
            self.stat["translated"] += 1
            self.jobs.mark_translated()
            self.lat["total"].add(time.time() - t_enq)
            self.uiq.put(("translation", job.sender, event_label(job.channel),
                          text, cached, True, job.display_order))
            return

        # ---- singleflight：同内容同时在途只发一次 HTTP ----
        protected, mapping = protect_terms(text, terms)

        def do_request():
            return self.call_provider(provider, model, key, base_url, eh, eb, protected)

        t0 = time.time()
        try:
            translated, _owner = self.flight.run(ckey, do_request)
        except Exception as e:
            # inflight 在 SingleFlight 内部已经释放，下一次同内容可以重试。
            self.stat["errors"] += 1
            self.jobs.mark_failed()
            self.uiq.put(("error", f"{provider}/{model}: {e}"))
            # 失败不留占位（v1.1 保持旧显示风格；V12-02 的占位是可选项）。
            return
        self.lat["translate"].add(time.time() - t0)

        try:
            translated = restore_terms((translated or "").strip(), mapping)
        except Exception:
            translated = (translated or "").strip()
        if not translated:
            self.jobs.mark_failed()
            self.stat["errors"] += 1
            return

        self.cache.put(ckey, translated)
        self.stat["translated"] += 1
        self.jobs.mark_translated()
        self.lat["total"].add(time.time() - t_enq)
        self.uiq.put(("translation", job.sender, event_label(job.channel),
                      text, translated, False, job.display_order))

    def call_provider(self, provider, model, key, base_url, extra_headers, extra_body, text):
        """Call the (Zhipu-compatible) endpoint, retrying transient failures with
        exponential backoff. Free tiers rate-limit bursts, and guild chat does
        arrive in bursts, so a 429 must not simply lose the message."""
        timeout = int(self.cfg["translation"].get("request_timeout_seconds", 12))
        attempts = max(1, int(self.cfg["translation"].get("retry_attempts", 3)))
        backoff = float(self.cfg["translation"].get("retry_backoff_seconds", 0.8))

        def once():
            if not key and not is_local_base_url(base_url):
                raise RuntimeError(f"未设置 {api_key_env_name(self.cfg)}")
            return compat_translate(key, model, text, timeout, base_url,
                                    extra_headers, extra_body)

        for attempt in range(attempts):
            try:
                return once()
            except Exception as e:
                if attempt >= attempts - 1 or not is_retryable(e):
                    raise
                # jitter avoids a thundering herd when several messages land together
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, 0.25))

    def shutdown(self):
        """有序停止：先停 worker（不再发新请求），再停 tail 线程。"""
        self.stop.set()
        try:
            self.jobs.stop(timeout=5.0)
        except Exception:
            pass
        try:
            self.cache.close()
        except Exception:
            pass

    def pump_ui(self):
        """UI 泵。**只在 Tkinter 主线程跑**（V11-02 硬要求）。

        v1.1：翻译事件现在是 7 元组（多了 display_order），但显示方式保持不变
        —— 规范要求「默认可保持旧显示风格，避免升级后界面突变」。
        """
        try:
            while True:
                item = self.uiq.get_nowait()
                if item[0] == "translation":
                    if len(item) >= 7:
                        _, sender, ch, original, tr, cached, _order = item
                    else:                       # 兼容旧 6 元组
                        _, sender, ch, original, tr, cached = item
                    self.overlay.add(sender, ch, original, tr, cached)
                elif item[0] == "error":
                    self.overlay.error(item[1])
                elif item[0] == "info":
                    self.overlay.add("system", "状态", "", item[1])
        except queue.Empty:
            pass

        now = time.time()
        if now - self._last_status >= 1.0:
            self._last_status = now
            self.overlay.set_status(self.status_text())
            # 设置面板开着时又发现了新频道 → 重建一次列表，否则新频道没处勾。
            if self.overlay.settings_stale(self.seen_channels):
                self.overlay._refresh_settings()
        self.root.after(100, self.pump_ui)

    def status_text(self):
        """One line that answers "is the pipeline alive?" at a glance.

        v1.1：改成用通道健康状态机（而不是「文件 idle 秒数」），并带上队列深度
        与丢弃计数。这样「装了没开游戏」「开了没人说话」「通道真断了」
        「解析一直失败」这几种完全不同的情况终于能分开。
        """
        st = self.stat
        a = self.health["A"]
        b = self.health["B"]
        a.tick()
        b.tick()
        qs = self.jobs.stats

        # 两条通道都还没配置 -> 明确提示怎么让它动起来。
        # v1.1.1：区分「目录根本没选对」和「目录对了但游戏还没跑过」——
        # 后者是可自愈的，不该把用户推去重选目录。
        if a.state == _transport.NOT_CONFIGURED and b.state == _transport.NOT_CONFIGURED:
            if not (self.cfg.get("wow_path") or "").strip():
                return "未设置客户端目录 —— 点 ⚙ 或重启程序选择"
            if not getattr(self, "logs_dir_found", False):
                return ("未找到 Logs 目录 —— 请选到含 Interface 的那一层"
                        "（启动器套壳的选 resources\\Wotlk）")
            return "等待游戏写入日志 —— 进游戏执行 /atai diag"

        # 副通道（A）天生不是实时的，标签要写清楚，不能让它拖累整体观感。
        parts = [
            f"B:{b.label()} {st['miss_marker']}",
            f"A:{a.label()} {st['marker']}",
            f"已译 {st['translated']}",
        ]
        if st["errors"]:
            parts.append(f"错 {st['errors']}")
        if st.get("muted"):
            parts.append(f"屏蔽 {st['muted']}")
        if st.get("parse_rejected"):
            parts.append(f"拒 {st['parse_rejected']}")
        depth = self.jobs.depth
        if depth or qs.dropped:
            parts.append(f"队列 {depth}/{self.queue_capacity}")
        if qs.dropped:
            parts.append(f"丢弃 {qs.dropped}")
        return " · ".join(parts)

    # ---- 诊断导出（V11-06 / V12-06 骨架） ----
    def diagnostics_report(self):
        """生成脱敏诊断报告（不含 Key / 聊天原文 / 玩家名）。"""
        a = self.health["A"].snapshot()
        b = self.health["B"].snapshot()
        return _diag.build_report(
            bridge_version=VERSION,
            addon_version=self._deployed_addon_version() or _diag.ADDON_VERSION,
            protocol_supported=PROTOCOL_SUPPORTED,
            counters={
                "stat": {k: v for k, v in self.stat.items()
                         if isinstance(v, (int, float))},
                "parse_rejected": self.stat.get("parse_rejected", 0),
                "dropped": self.stat.get("dropped", 0),
                "jobs": self.jobs.stats.snapshot(),
                "dedupe": self.deduper.stats(),
                "cache": {"hits": self.cache.hits, "misses": self.cache.misses,
                          "writes": self.cache.writes, "errors": self.cache.errors},
                "inflight_pending": self.flight.pending,
                "inflight_merged": self.flight.merged,
                "peak_queue_depth": self.jobs.peak_depth,
                "workers": self.jobs.max_workers,
                "queue_capacity": self.queue_capacity,
            },
            channel_health={"A": a, "B": b},
            latencies={
                "translate_seconds": self.lat["translate"].stats(),
                "bridge_total_seconds": self.lat["total"].stats(),
            },
            extra={
                "log_dir": _diag._basename_safe(self.log_path.parent
                                                if self.log_path else ""),
                "log_exists": bool(self.log_path and self.log_path.exists()),
                "miss_exists": bool(self.miss_path and self.miss_path.exists()),
            },
        )

    def _deployed_addon_version(self):
        try:
            if self.cfg.get("wow_path"):
                dest = addons_dir(self.cfg["wow_path"]) / ADDON_NAME
                toc = dest / "ArdenTranslatorAI.toc"
                if toc.exists():
                    return _read_toc_version(toc)
        except Exception:
            pass
        return ""

def cli_check():
    """Headless pre-flight check: run with --check before launching the overlay.

    Verifies the config resolves, the API key is visible to this process, and a
    real translation round-trip succeeds.
    """
    cfg = load_config()
    provider, model, key, base_url, extra_headers, extra_body = resolve_provider(cfg)
    env = api_key_env_name(cfg)
    # v1.0.16：Key 只从环境变量读。配置文件里不该有明文，有就顺手抹掉。
    if (cfg.get("compat") or {}).get("api_key"):
        cfg["compat"].pop("api_key", None)
        try:
            save_config(cfg)
        except Exception:
            pass
        print("[提示] 已从 config.json 抹掉遗留的明文 Key（现在只存在环境变量里）。")

    print(f"配置文件   : {CONFIG_PATH}")
    print(f"桥接版本   : {APP_NAME} {VERSION}")
    print(f"协议兼容   : ATAI2(6段) / @ATAI1@(6段, 兼容历史5段) "
          f"-> 支持范围 {list(PROTOCOL_SUPPORTED)}")
    print(f"插件版本   : 内置 {_diag.ADDON_VERSION}")
    tr = cfg["translation"]
    print(f"并发       : {tr.get('max_workers', 2)} worker · "
          f"队列上限 {tr.get('queue_capacity', 100)} · "
          f"TTL {tr.get('queue_ttl_seconds', 30)}s(公共)/"
          f"{tr.get('queue_ttl_seconds_other', 120)}s(其他)")
    print(f"model      : {model}")
    print(f"base_url   : {base_url or '(内置)'}")
    print(f"Key 来源   : 环境变量 {env} -> "
          f"{'已设置（' + str(len(key)) + ' 字符）' if key else '未设置'}")
    print(f"Key 存储   : 仅 Windows 用户环境变量（**不是**加密存储；"
          f"同机同用户的其他程序可读）")
    if extra_body:
        print(f"extra_body : {extra_body}")

    # 插件来源：EXE 内置那份才是「点开始时会写进游戏」的版本，值得单独看一眼。
    src = plugin_source_dir()
    print(f"插件来源   : {src if src else '(找不到！EXE 里没有内置插件)'}")

    # 两条通道的健康状况。通道 B（MissingFiles）是实时的那条，值得单独看一眼：
    # 文件不存在就说明客户端还没运行过、或者插件版本太旧没走 SetTexture。
    #
    # v1.1.1：不再硬写 Logs\ 与 MissingFiles.txt —— 用和 App 相同的解析器，
    # 这样 --check 报的路径和实际监听的路径永远一致（免得排障时被误导）。
    logs_dir = find_logs_dir(cfg.get("wow_path", ""))
    log_path = find_chat_log(cfg.get("wow_path", ""))
    miss_path = find_missing_files(cfg.get("wow_path", ""))
    if logs_dir is None and (cfg.get("wow_path") or "").strip():
        cands = _candidate_logs_dirs(cfg.get("wow_path", ""))
        logs_dir = cands[0] if cands else None
    if logs_dir is not None:
        print(f"Logs 目录  : {logs_dir}")
        if log_path is None:
            log_path = logs_dir / "WoWChatLog.txt"
        if miss_path is None:
            # 报「将来会出现」的路径时，两个候选名都提一下，
            # 免得用户看到 .txt 却发现在硬盘上是 .log 而困惑。
            miss_path = logs_dir / MISSING_FILES_NAMES[0]
        print(f"通道 A     : {log_path}")
        print(f"             -> {'存在' if log_path.exists() else '尚未生成'} "
              f"（**非实时**：要等客户端缓冲区满或退出才刷盘）")
        print(f"通道 B     : {miss_path}")
        print(f"             -> {'存在' if miss_path.exists() else '尚未生成（进游戏跑一次 /atai misstest）'}")
        print(f"             （实时性依赖客户端行为，本客户端实测秒级；换服请重新实测）")
        # 两个文件名都报出来，避免「明明写了 .txt 怎么找不到」的困惑。
        others = [n for n in MISSING_FILES_NAMES if (logs_dir / n).exists()]
        if others and others != [miss_path.name]:
            print(f"             （该目录下实际存在：{', '.join(others)}）")
    else:
        print(f"通道 A/B   : 未找到 Logs 目录 —— 检查 config.json 里的 wow_path")
        print(f"             已试过的候选（v1.1.1 起兼容启动器套壳布局）：")
        for c in _candidate_logs_dirs(cfg.get("wow_path", "")):
            print(f"               - {c}")

    # 已部署插件是否和内置那份一致（版本错配是最难查的一类问题）。
    ok, why = check_wow_path(cfg.get("wow_path", ""))
    if ok:
        dest = addons_dir(cfg["wow_path"]) / ADDON_NAME
        toc = dest / "ArdenTranslatorAI.toc"
        if not dest.exists():
            print(f"已装插件   : 未部署（点管理界面的「开始」会自动装）")
        else:
            ver = ""
            try:
                for ln in toc.read_text(encoding="utf-8", errors="replace").splitlines():
                    if ln.strip().startswith("## Version:"):
                        ver = ln.split(":", 1)[1].strip()
                        break
            except Exception:
                pass
            same = "一致" if _same_tree(src, dest) else "**与内置不一致，点「开始」可更新**"
            print(f"已装插件   : {dest}")
            print(f"             -> 版本 {ver or '?'} · 与内置 {same}")
    else:
        print(f"已装插件   : 无法检查 —— {why}")

    if not base_url:
        print("\n[失败] base_url 未配置。")
        return 1
    if not model:
        print("\n[失败] 模型名未配置。")
        return 1
    if not key and not is_local_base_url(base_url):
        print(f"\n[失败] 没有读到 API Key。")
        print(f"       设置方式（二选一）：")
        print(f"       1) 运行 {APP_NAME}，在管理界面里填好 Key 再点「开始翻译」")
        print(f"          （会把 Key 写进用户环境变量，不存配置文件）；")
        print(f'       2) [Environment]::SetEnvironmentVariable("{env}","你的key","User")')
        print(f"          （程序会直接读注册表，设完不必重启终端）")
        return 1

    sample = "LFG need a healer for Zul'Farrak, whisper me"
    print(f"\n发送测试请求: {sample!r}")
    try:
        timeout = int(cfg["translation"].get("request_timeout_seconds", 12))
        out = compat_translate(key, model, sample, timeout, base_url, extra_headers, extra_body)
        print(f"翻译结果   : {out}")
        print("\n[成功] 配置可用，现在可以启动悬浮窗了。")
        return 0
    except Exception as e:
        print(f"\n[失败] {type(e).__name__}: {e}")
        print("\n常见原因：")
        print("  HTTP 401 -> Key 无效或已过期")
        print("  HTTP 400 -> 模型名不对，检查 config.json 里的 compat.model")
        print("  HTTP 429 -> 触发限流，稍后再试")
        print("  超时      -> 网络不通，检查 base_url 或代理")
        return 1


def _same_tree(a: Path | None, b: Path) -> bool:
    """a、b 两个目录里的同名文件内容是否完全一致（用于「插件是不是最新的」）。"""
    if a is None or not a.exists() or not b.exists():
        return False
    try:
        names_a = {f.name for f in a.iterdir() if f.is_file()}
        names_b = {f.name for f in b.iterdir() if f.is_file()}
        if names_a != names_b:
            return False
        for n in names_a:
            if (a / n).read_bytes() != (b / n).read_bytes():
                return False
        return True
    except Exception:
        return False


def test_translation(model, key, timeout=15):
    """管理界面「测试」按钮用的单次真实调用。返回 (ok, message)。"""
    model = (model or "").strip()
    if not model:
        return False, "请先填写模型名。"
    key = (key or "").strip()
    if not key:
        return False, (f"还没有 API Key。填进上面那一栏再点开始，或者手动设置环境变量 "
                       f"{DEFAULT_API_KEY_ENV}。")
    sample = "LFG need a healer for Zul'Farrak, whisper me"
    try:
        out = compat_translate(key, model, sample, timeout, ZHIPU_BASE_URL)
    except Exception as e:
        msg = str(e)
        hint = ""
        if "401" in msg:
            hint = "\nKey 无效或已过期。"
        elif "400" in msg:
            hint = "\n模型名可能不对。"
        elif "429" in msg:
            hint = "\n触发限流，稍后再试。"
        elif "timed out" in msg.lower() or "timeout" in msg.lower():
            hint = "\n网络超时：检查网络或代理。"
        return False, f"{msg}{hint}"
    return True, out


# ---------------------------------------------------------------------------
# 管理界面（v1.0.15）
#
# 双击 EXE 看到的第一屏。填好四项 —— 客户端目录 / 模型 / API Key / 悬浮窗外观 ——
# 点「开始」就一条龙：部署插件 → 存配置 → 关本窗口 → 弹出翻译悬浮窗。
# ---------------------------------------------------------------------------

class Launcher:
    """管理界面窗口。用一个 Toplevel，主窗口（root）保持隐藏，等「开始」之后
    交给 Overlay 复用 —— 这样悬浮窗的代码一行都不用改。"""

    BG = "#1b1b1b"
    FG = "#e8e8e8"
    ACCENT = "#7fd7ff"
    DIM = "#9b9b9b"
    ERR = "#ff8b8b"
    OK = "#8fdf9f"

    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.on_start = None        # 由 main() 注入：开始 -> 弹悬浮窗
        self._testing = False

        win = tk.Toplevel(root)
        self.win = win
        win.title(f"{APP_NAME} {VERSION} — 设置")
        win.configure(bg=self.BG)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", root.destroy)
        self._build()
        # 顺序要紧，别调换：Tk 在 Windows 上打开 `-topmost` 时会**重新放置**窗口，
        # 位置被重置回 WM 的级联位置（实测 +156+156），_center() 白算 ——
        # 表现就是管理界面贴在屏幕左上角而不是居中。所以先 topmost、后 geometry。
        win.attributes("-topmost", True)
        self._center()
        win.after(300, lambda: win.attributes("-topmost", False))
        win.focus_force()

    # ---- 布局 ------------------------------------------------------------
    def _build(self):
        head = ("Microsoft YaHei UI", 10, "bold")
        body = ("Microsoft YaHei UI", 9)
        pad = dict(padx=16)

        tk.Label(self.win, text=f"{APP_NAME}  v{VERSION}", fg=self.ACCENT, bg=self.BG,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(14, 0), **pad)
        tk.Label(self.win, text="把游戏内聊天实时翻成简体中文 · 只支持智谱 GLM",
                 fg=self.DIM, bg=self.BG, font=("Microsoft YaHei UI", 8)
                 ).pack(anchor="w", pady=(0, 10), **pad)

        compat = self.cfg.get("compat") or {}
        ov = self.cfg.get("overlay") or {}

        # ---- ① 客户端目录 ----
        box1 = self._section("1  魔兽世界客户端根目录")
        row = tk.Frame(box1, bg=self.BG); row.pack(fill="x")
        self.var_wow = tk.StringVar(value=(self.cfg.get("wow_path") or ""))
        e = tk.Entry(row, textvariable=self.var_wow, bg="#2a2a2a", fg=self.FG,
                     insertbackground=self.FG, relief="flat", font=body, width=44)
        e.pack(side="left", ipady=4)
        tk.Button(row, text="浏览…", command=self._browse, bg="#2a2a2a", fg=self.FG,
                  activebackground="#3a3a3a", activeforeground="#ffffff",
                  relief="flat", font=body, padx=10).pack(side="left", padx=(6, 0))
        tk.Label(box1, text="选到含 Interface 和 Logs 的那一层，例如 C:\\WOW\\ArdenWoW\\Wotlk。"
                            "填过一次就会记住。",
                 fg=self.DIM, bg=self.BG, font=("Microsoft YaHei UI", 8),
                 wraplength=430, justify="left").pack(anchor="w", pady=(4, 0))

        # ---- ② 模型 + ③ API Key ----
        box2 = self._section("2  模型与 API Key")
        r1 = tk.Frame(box2, bg=self.BG); r1.pack(fill="x")
        # width=13 是为了完整放下 "ZHIPU_API_KEY"（Label 的 width 会裁字，
        # 之前给 10 结果显示成 "ZHIPU_API K"）。两行用同一个宽度才对得齐。
        tk.Label(r1, text="模型", fg=self.FG, bg=self.BG, font=body, width=13,
                 anchor="w").pack(side="left")
        self.var_model = tk.StringVar(value=(compat.get("model") or DEFAULT_MODEL))
        tk.Entry(r1, textvariable=self.var_model, bg="#2a2a2a", fg=self.FG,
                 insertbackground=self.FG, relief="flat", font=body, width=34
                 ).pack(side="left", ipady=4)

        r2 = tk.Frame(box2, bg=self.BG); r2.pack(fill="x", pady=(6, 0))
        tk.Label(r2, text="ZHIPU_API_KEY", fg=self.FG, bg=self.BG, font=body,
                 width=13, anchor="w").pack(side="left")
        # 值直接来自环境变量：**有就显示圆点、没有就是空白**。
        # 界面上填的新值在点「开始翻译」时写回同一个环境变量，
        # **永远不会落进 config.json**。
        self.var_key = tk.StringVar(value=effective_api_key(self.cfg))
        self.entry_key = tk.Entry(r2, textvariable=self.var_key, show="\u2022",
                                  bg="#2a2a2a", fg=self.FG, insertbackground=self.FG,
                                  relief="flat", font=body, width=34)
        self.entry_key.pack(side="left", ipady=4)

        r3 = tk.Frame(box2, bg=self.BG); r3.pack(fill="x", pady=(8, 0))
        self.btn_test = tk.Button(r3, text="测试模型 + Key", command=self._test,
                                  bg="#2f5d7a", fg="#ffffff", activebackground="#3a7396",
                                  activeforeground="#ffffff", relief="flat", font=body,
                                  padx=14)
        self.btn_test.pack(side="left")
        tk.Label(r3, text=f"只存在环境变量 {api_key_env_name(self.cfg)}，不写入配置文件",
                 fg=self.DIM, bg=self.BG, font=("Microsoft YaHei UI", 8)
                 ).pack(side="left", padx=(10, 0))

        self.lbl_test = tk.Label(box2, text="", fg=self.DIM, bg=self.BG,
                                 font=("Microsoft YaHei UI", 8), wraplength=430,
                                 justify="left", anchor="w")
        self.lbl_test.pack(anchor="w", pady=(6, 0), fill="x")
        self._set_test_text(
            f"已从环境变量读到 Key（{len(self.var_key.get())} 字符）" if self.var_key.get()
            else "尚未设置 Key。填进上面那一栏再点开始，或先点「测试」验证。", self.DIM)

        # ---- ④ 悬浮窗外观 ----
        box3 = self._section("3  翻译悬浮窗（长宽 / 位置 / 字号）")
        g = tk.Frame(box3, bg=self.BG); g.pack(fill="x")
        self.var_ov = {}
        fields = [("width", "宽", 4), ("height", "高", 4), ("x", "X", 4),
                  ("y", "Y", 4), ("alpha", "透明度", 5), ("font_size", "字号", 4),
                  ("max_lines", "最多行", 4)]
        for i, (k, lab, w) in enumerate(fields):
            cell = tk.Frame(g, bg=self.BG)
            cell.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 14), pady=3)
            tk.Label(cell, text=lab, fg=self.DIM, bg=self.BG,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w")
            v = tk.StringVar(value=str(ov.get(k, DEFAULT_CONFIG["overlay"][k])))
            self.var_ov[k] = v
            tk.Entry(cell, textvariable=v, bg="#2a2a2a", fg=self.FG,
                     insertbackground=self.FG, relief="flat", font=body,
                     width=w).pack(ipady=3)
        tk.Button(box3, text="预览位置", command=self._preview, bg="#2a2a2a",
                  fg=self.FG, activebackground="#3a3a3a", activeforeground="#ffffff",
                  relief="flat", font=("Microsoft YaHei UI", 8), padx=10
                  ).pack(anchor="w", pady=(8, 0))

        # ---- ⑤ 部署提示 + 开始 ----
        self.lbl_deploy = tk.Label(self.win, text="点「开始」会自动把插件部署到 "
                                                 "Interface\\AddOns（覆盖旧版本），然后弹出悬浮窗。",
                                   fg=self.DIM, bg=self.BG, font=("Microsoft YaHei UI", 8),
                                   wraplength=430, justify="left", anchor="w")
        self.lbl_deploy.pack(anchor="w", pady=(12, 0), **pad)

        foot = tk.Frame(self.win, bg=self.BG); foot.pack(fill="x", pady=(10, 16), **pad)
        tk.Button(foot, text="开始翻译", command=self._start, bg="#2f7a4a", fg="#ffffff",
                  activebackground="#3a9660", activeforeground="#ffffff",
                  relief="flat", font=("Microsoft YaHei UI", 11, "bold"), padx=24
                  ).pack(side="left")
        tk.Button(foot, text="退出", command=self.root.destroy, bg="#2a2a2a", fg=self.FG,
                  activebackground="#3a3a3a", activeforeground="#ffffff", relief="flat",
                  font=body, padx=16).pack(side="left", padx=(8, 0))
        tk.Label(foot, text="（悬浮窗里：Ctrl+H 隐藏/显示 · Ctrl+Q 退出）", fg="#6f6f6f",
                 bg=self.BG, font=("Microsoft YaHei UI", 8)).pack(side="right")

    def _section(self, title):
        tk.Label(self.win, text=title, fg=self.ACCENT, bg=self.BG,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(10, 4),
                                                               padx=16)
        f = tk.Frame(self.win, bg=self.BG)
        f.pack(anchor="w", fill="x", padx=16)
        return f

    def _center(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.win.geometry(f"+{x}+{y}")

    # ---- 交互 ------------------------------------------------------------
    def _browse(self):
        chosen = filedialog.askdirectory(
            title="选择魔兽世界客户端目录（含 Interface 与 Logs；启动器套壳的选 resources\\Wotlk）",
            initialdir=self.var_wow.get() or None)
        if not chosen:
            return
        chosen = str(Path(chosen))
        self.var_wow.set(chosen)
        # 需求①：选一次就记住 —— 立刻落盘，即使随后直接关窗口也不丢。
        self.cfg["wow_path"] = chosen
        try:
            save_config(self.cfg)
        except Exception:
            pass
        ok, why = check_wow_path(chosen)
        self._set_test_text(("目录 OK。" if ok else why), self.OK if ok else self.ERR)

    def _set_test_text(self, text, color):
        self.lbl_test.configure(text=text, fg=color)

    def _test(self):
        if self._testing:
            return
        model = self.var_model.get().strip()
        # 就用界面上那一栏的值（它本来就是从环境变量读出来的），不碰配置文件。
        key = self.var_key.get().strip()
        self._testing = True
        self.btn_test.configure(state="disabled", text="测试中…")
        self._set_test_text("正在向智谱发一条真实翻译请求…", self.DIM)

        def work():
            ok, msg = test_translation(model, key)
            self.root.after(0, lambda: self._test_done(ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, ok, msg):
        self._testing = False
        self.btn_test.configure(state="normal", text="测试模型 + Key")
        if ok:
            self._set_test_text(f"测试成功 · 示例译文：{msg}", self.OK)
        else:
            self._set_test_text(f"测试失败：{msg}", self.ERR)

    def _read_overlay(self):
        """把界面上的数字读成 overlay 配置。非法值一律退回默认，绝不抛错。"""
        out = {}
        for k, default in DEFAULT_CONFIG["overlay"].items():
            raw = (self.var_ov[k].get() or "").strip()
            try:
                val = float(raw) if isinstance(default, float) else int(float(raw))
            except Exception:
                val = default
            out[k] = val
        # 兜底：长宽为 0 或负数会让悬浮窗直接消失，很难自查。
        out["width"] = max(120, out["width"])
        out["height"] = max(60, out["height"])
        out["font_size"] = min(40, max(6, out["font_size"]))
        out["max_lines"] = min(200, max(1, out["max_lines"]))
        out["alpha"] = min(1.0, max(0.2, out["alpha"]))
        return out

    def _preview(self):
        """按当前长宽/位置闪一个框，方便在进游戏前把位置调好。"""
        ov = self._read_overlay()
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", min(1.0, ov["alpha"] + 0.1))
        except Exception:
            pass
        w.geometry(f'{ov["width"]}x{ov["height"]}+{ov["x"]}+{ov["y"]}')
        w.configure(bg="#111111")
        tk.Label(w, text=f'{ov["width"]}×{ov["height"]}  @{ov["x"]},{ov["y"]}'
                         f'  字号 {ov["font_size"]}',
                 fg="#7fd7ff", bg="#111111",
                 font=("Microsoft YaHei UI", max(8, int(ov["font_size"])))).pack(expand=True)
        w.after(2200, w.destroy)

    def _start(self):
        wow = self.var_wow.get().strip()
        ok, why = check_wow_path(wow)
        if not ok:
            messagebox.showerror(APP_NAME, why, parent=self.win)
            return
        model = self.var_model.get().strip() or DEFAULT_MODEL

        # ⑤ 部署插件：先删旧文件夹再重建，保证游戏里跑的一定是最新那份。
        ok, msg, dest = deploy_addon(wow)
        if not ok:
            messagebox.showerror(APP_NAME, msg, parent=self.win)
            return
        self.lbl_deploy.configure(text=msg.replace("\n", "  "), fg=self.OK)
        self.win.update_idletasks()

        # API Key：写进**用户环境变量**，不进 config.json。
        # 留空 = 保持现有的环境变量不变（不清空）；填了才覆盖。
        typed = self.var_key.get().strip()
        env = api_key_env_name(self.cfg)

        # 全新机器（比如第一次拿到 EXE 的朋友）上环境变量是空的，此时留空点开始
        # 会一路放行到翻译时才报错 —— 不如当场说清楚，但允许用户坚持继续。
        if not typed and not get_env(env):
            if not messagebox.askyesno(
                    APP_NAME,
                    f"环境变量 {env} 还没有设置，不填 Key 的话翻译会全部失败。\n\n"
                    f"现在就去智谱开放平台（open.bigmodel.cn）免费申请一个，\n"
                    f"粘进上面那一栏再点开始即可，程序会替你写进环境变量。\n\n"
                    f"仍要继续（不翻译，只部署插件）吗？",
                    parent=self.win):
                return

        if typed and typed != get_env(env):
            if not set_user_env(env, typed):
                messagebox.showerror(APP_NAME,
                                     f"无法把 Key 写入环境变量 {env}。\n"
                                     f"请手动设置后重试：\n"
                                     f'[Environment]::SetEnvironmentVariable'
                                     f'("{env}","你的key","User")', parent=self.win)
                return

        # 存配置（客户端目录 / 模型 / 悬浮窗外观 —— 就是没有 Key）
        self.cfg["wow_path"] = wow
        compat = self.cfg.setdefault("compat", {})
        compat["model"] = model
        compat["base_url"] = compat.get("base_url") or ZHIPU_BASE_URL
        compat["api_key_env"] = env
        self.cfg["overlay"] = self._read_overlay()
        self.cfg["provider"] = "zhipu"
        for legacy in ("openai_model", "gemini_model"):
            self.cfg.pop(legacy, None)
        try:
            save_config(self.cfg)          # save_config 会再剥一次 api_key
        except Exception as e:
            messagebox.showerror(APP_NAME, f"保存配置失败：{e}", parent=self.win)
            return

        # ⑥ 关掉管理窗，直接弹出翻译悬浮窗 —— 一步到位。
        self.win.destroy()
        if self.on_start:
            self.on_start()


def main():
    if "--check" in sys.argv:
        # 离线自检：不建窗口、不读配置之外的东西。CI / 排障用。
        raise SystemExit(cli_check())
    if "--diag" in sys.argv:
        # V12-06 骨架：导出**脱敏**诊断报告（不含 API Key、聊天原文、玩家名）。
        # 设计成可离线运行：没有 App 实例时只报告「未运行」，不伪造一份假的有序数据。
        raise SystemExit(cli_diag())
    if "--version" in sys.argv:
        print(f"ArdenTranslatorAI {VERSION} (bridge) / addon {_diag.ADDON_VERSION}")
        print(f"protocol supported: {_diag.PROTOCOL_SUPPORTED}")
        raise SystemExit(0)
    # v1.0.16：老配置里可能躺着明文的 compat.api_key，趁着启动搬进环境变量并从
    # 磁盘抹掉。放在这里是因为只有真正的 GUI 启动才该改注册表（测试脚本不当碰）。
    try:
        migrate_plaintext_key()
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()          # 先藏起来：管理界面还没画出来之前别闪一个空白窗

    app_holder = {}

    def start_overlay():
        app = App(root)
        app_holder["app"] = app
        root.deiconify()
        root.protocol("WM_DELETE_WINDOW", on_close)
        return app

    def on_close():
        # V11-02：关窗必须**有序停掉 worker 队列**再销毁窗口，否则 worker 还在
        # 调 API、UI 队列已经没人消费，退出时会留下「翻译结果无处可去」的告警。
        # jobs.stop() 自带 timeout，卡死也不会挂住退出流程。
        app = app_holder.get("app")
        if app is not None:
            try:
                app.shutdown()
            except Exception:
                pass
        try:
            root.destroy()
        except Exception:
            pass

    if "--overlay" in sys.argv:
        # 跳过管理界面直接上悬浮窗（配置已存好时用，也方便自动化测试）。
        start_overlay()
    else:
        launcher = Launcher(root)
        launcher.on_start = start_overlay
        root._launcher = launcher      # 保住引用，别被 GC 掉
    try:
        root.mainloop()
    finally:
        # mainloop 异常退出（或被 Alt+F4 之外的路径关掉）时兜底停一次。
        app = app_holder.get("app")
        if app is not None:
            try:
                app.shutdown()
            except Exception:
                pass


def cli_diag():
    """--diag：打印脱敏诊断报告。返回退出码（0 正常）。"""
    import json
    rep = {
        "bridge_version": VERSION,
        "addon_version": _diag.ADDON_VERSION,
        "protocol_supported": list(_diag.PROTOCOL_SUPPORTED),
        "app_running": False,
        "note": (
            "本进程是**独立**启动的 --diag，没有正在运行的桥接实例可查，"
            "所以只报告静态信息。要看运行中的通道/队列指标，请在 GUI 里点"
            "『导出诊断』，或让 --diag 从正在运行的实例读取。"
        ),
    }
    try:
        cfg = load_config()
        rep["config_path"] = _diag._basename_safe(str(CONFIG_PATH))
        rep["wow_path_set"] = bool(cfg.get("wow_path"))
        rep["model"] = cfg.get("translation", {}).get("model")
        rep["base_url"] = normalize_base_url(cfg.get("translation", {}).get("base_url", ""))
        rep["key_env"] = api_key_env_name(cfg)
        rep["key_present"] = bool(effective_api_key(cfg))
        # 这句必须明写：用户会以为「没存盘=加密了」，实际是环境变量里的明文。
        rep["key_storage"] = "Windows 用户环境变量（明文，非加密存储）"
    except Exception as exc:  # 配置本身坏了也要能出报告
        rep["config_error"] = type(exc).__name__
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    main()
