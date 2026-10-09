"""把真实插件加载进一个最小化的假 WoW 环境，触发事件并检查 payload。

目的：在**真实源码**上验证两件事（不是验证复制出来的片段）：
  1. v1.0.13 的「频道名填进第 4 段」仍然成立；
  2. v1.0.14 的「点号转义成 %2E」确实生效 —— payload 里**不能出现任何裸点号**，
     否则客户端会把路径在最后一个点号处截断（把后半段当扩展名换成 .blp/.tga），
     公共频道整条消息会被吃掉。

跑法（需要 lupa）：
    C:/Users/cheny/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe _lua_probe.py
"""
import lupa, pathlib, sys

# 包内插件副本：<包根>/ArdenTranslatorAI/ArdenTranslatorAI.lua
ADDON = (pathlib.Path(__file__).resolve().parent.parent
         / "ArdenTranslatorAI" / "ArdenTranslatorAI.lua")
if not ADDON.exists():
    print("找不到插件文件:", ADDON)
    sys.exit(1)
src = ADDON.read_text(encoding="utf-8")

L = lupa.LuaRuntime(unpack_returned_tuples=True)

PRELUDE = r"""
-- ---- 捕获用 ----
CAPTURED_TEXTURE = nil
CAPTURED_WHISPERS = {}
FRAMES = {}
SLASH = {}

-- ---- 最小 WoW 环境 ----
UIParent = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(self, m) end }
GameFontHighlightSmall = "GameFontHighlightSmall"

local function mkframe()
  local f = {}
  f._scripts = {}
  f.SetScript = function(self, ev, fn) self._scripts[ev] = fn end
  f.RegisterEvent = function() end
  f.SetSize = function() end
  f.SetPoint = function() end
  f.SetMaxLines = function() end
  f.SetTimeVisible = function() end
  f.SetAlpha = function() end
  f.Hide = function() end
  f.Show = function() end
  f.CreateTexture = function(self)
    return { SetTexture = function(_, p) CAPTURED_TEXTURE = p end }
  end
  FRAMES[#FRAMES + 1] = f
  return f
end

function CreateFrame(t, name, parent) return mkframe() end

-- 插件用 SendChatMessage 发私聊（通道 A）；记下来
function SendChatMessage(msg, kind, lang, target)
  CAPTURED_WHISPERS[#CAPTURED_WHISPERS + 1] = msg
end

function UnitName(unit) return "Me" end
function GetTime() return 1760000000 end
function time() return 1760000000 end
function LoggingChat(v) if v ~= nil then _LOGGING = v end return true end
function LoggingCombat(v) return true end
function GetCVar(k) return "0" end
function SetCVar(k, v) return true end
function ChatFrame_AddMessageEventFilter() end
function ChatFrame_RemoveMessageEventFilter() end
function GetChannelName(i) return i, "General" end
function strlower(s) return string.lower(s) end

SlashCmdList = setmetatable({}, { __index = function(t, k) return function() end end })
"""

L.execute(PRELUDE)
L.execute(src)

# 触发 ADDON_LOADED 让插件完成初始化（copyDefaults / applyFilters 等）
lua_frames = L.globals().FRAMES
n_frames = len(lua_frames)
print("插件创建的 frame 数量 :", n_frames)

# 找到注册了 OnEvent 的那个 frame（Lua 表用 1 起始下标访问）
on_event = None
for i in range(1, n_frames + 1):
    scripts = lua_frames[i]._scripts
    if scripts["OnEvent"] is not None:
        on_event = scripts["OnEvent"]
        break
assert on_event is not None, "没找到 OnEvent 处理器"
print("已找到 OnEvent 处理器")

# 初始化
on_event(None, "ADDON_LOADED", "ArdenTranslatorAI")
on_event(None, "PLAYER_LOGIN")
print("ADDON_LOADED / PLAYER_LOGIN 已触发")

VERSION = None
for line in src.splitlines():
    if line.startswith("local VERSION = "):
        VERSION = line.split('"')[1]
print("插件版本 :", VERSION)


def unesc(s):
    """与桥接端 ArdenTranslatorAI.unesc() 等价（那边要 import tkinter，这里跑不了）。
    真正的 unesc() 由 _selftest.py 覆盖。顺序：%25 最后。"""
    return (s.replace("%7C", "|").replace("%7c", "|")
             .replace("%2E", ".").replace("%2e", ".")
             .replace("%25", "%"))


def fire(event, *args):
    L.globals().CAPTURED_TEXTURE = None
    on_event(None, event, *args)
    return L.globals().CAPTURED_TEXTURE


def parts_of(payload):
    """ATAI2 | nonce | seq | label | author | msg"""
    return payload.split("%7C")


def label_of(payload):
    """原始（仍转义着的）标签段。"""
    return parts_of(payload)[3]


def msg_of(payload):
    """原始（仍转义着的）正文。正文里若含竖线会被转义成 %7C，按 %7C 切分会
    多出字段 —— 所以从第 6 段起再拼回来（正文是最后一段）。"""
    return "%7C".join(parts_of(payload)[5:])


def check_nodot(payload, what):
    """整个 payload 里不能有裸点号 —— 有的话客户端一定截断。"""
    assert "." not in payload, f"{what}: payload 里有裸点号，会被客户端截断\n  {payload}"
    print(f"  -> OK：无裸点号（{what}）")


print()
print("=== 1) 公共频道：频道名 '2. Newcomers' 里的点号必须被转义 ===")
# CHAT_MSG_CHANNEL: text, playerName, languageName, channelName, ...
p = fire("CHAT_MSG_CHANNEL", "anyone selling bags cheap",
         "Kywen", "Common", "2. Newcomers")
print("payload      :", p)
print("第 4 段(原始) :", label_of(p))
check_nodot(p, "频道名含点号")
assert label_of(p) == "2%2E Newcomers", label_of(p)
assert unesc(label_of(p)) == "2. Newcomers", unesc(label_of(p))
print("  -> OK：'2. Newcomers' 被转义成 '2%2E Newcomers'，unesc 能还原")

print()
print("=== 2) 正文里的点号也必须被转义 ===")
p = fire("CHAT_MSG_CHANNEL", "Might. It adds defence stuff",
         "Jacey", "Common", "2. Newcomers")
print("payload      :", p)
check_nodot(p, "正文含句号")
assert msg_of(p) == "Might%2E It adds defence stuff", msg_of(p)
assert unesc(msg_of(p)) == "Might. It adds defence stuff"
print("  -> OK：以前会被砍成 'Might'，现在完整保留")

print()
print("=== 3) 中文频道名 ===")
p = fire("CHAT_MSG_CHANNEL", "有人卖包吗", "Ann", "通用语", "5. 世界")
print("第 4 段(原始) :", label_of(p))
check_nodot(p, "中文频道名")
assert unesc(label_of(p)) == "5. 世界", unesc(label_of(p))
print("  -> OK：中文频道名也能带出")

print()
print("=== 4) 其它事件不受影响（仍填事件名，无点号）===")
p = fire("CHAT_MSG_GUILD", "need dps for raid", "Bob")
print("第 4 段(原始) :", label_of(p))
assert label_of(p) == "CHAT_MSG_GUILD", label_of(p)
p = fire("CHAT_MSG_PARTY", "hi there friend", "Ken")
assert label_of(p) == "CHAT_MSG_PARTY", label_of(p)
check_nodot(p, "小队消息")
print("  -> OK：公会/小队仍是事件名")

print()
print("=== 5) 频道名取不到时退回事件名 ===")
p = fire("CHAT_MSG_CHANNEL", "hello world guys", "Zoe", "Common")   # 没有 arg4
print("第 4 段(原始) :", label_of(p))
assert label_of(p) == "CHAT_MSG_CHANNEL", label_of(p)
print("  -> OK：安全退回，不会崩")

print()
print("=== 6) 段数必须是 6（协议没变）===")
p = fire("CHAT_MSG_CHANNEL", "six fields please ok", "Ann", "Common", "2. Newcomers")
all_parts = parts_of(p)
print("段数 :", len(all_parts), all_parts)
assert len(all_parts) == 6, len(all_parts)
assert all_parts[0] == "ATAI2"
print("  -> OK：仍是 6 段，旧版桥接不会错位")

print()
print("=== 7) 百分号 / 竖线 / 点号 混合（转义顺序不能互相破坏）===")
p = fire("CHAT_MSG_CHANNEL", "100% off | nice. ok", "Ann", "Common", "2. Newcomers")
print("payload      :", p)
check_nodot(p, "混合字符")
assert unesc(msg_of(p)) == "100% off | nice. ok", unesc(msg_of(p))
print("  -> OK：unesc 精确还原 '100% off | nice. ok'")

print()
print("=== 8) 自己的消息不转发 ===")
p = fire("CHAT_MSG_CHANNEL", "my own words here", "Me", "Common", "2. Newcomers")
print("payload :", p)
assert p is None, p
print("  -> OK：作者是自己时被 shouldForward 挡下")

print()
print("ALL OK：真实插件源码在频道名传递 + 点号转义上行为完全正确")
