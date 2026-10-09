-- ArdenTranslator AI v1.0.8
-- Designed for WoW 3.3.5-style clients / ArdenWoW.
--
-- 架构说明（重要，先读这段）
-- ======================================================================
-- WoW 的 Lua 插件没有网络 / 文件读写能力，只能借客户端自带的聊天日志
-- WoWChatLog.txt 当单向管道：
--
--   插件 -> 把消息发到聊天里 -> 客户端写进 Logs\WoWChatLog.txt
--        -> 桥接程序(Arden.exe) 实时 tail 该文件 -> 调大模型翻译 -> 悬浮窗
--
-- 三个必须同时满足的条件：
--
-- 1) 消息要进日志，就不能被过滤器拦掉。
--    3.3.5 的 ChatFrame_MessageEventHandler 里过滤器先跑，返回 true 就直接
--    return，连 self:AddMessage 都不会调用 —— 而日志是 C 层在 AddMessage
--    时写的。v1.0.0 把 CHAT_MSG_WHISPER 和 CHAT_MSG_WHISPER_INFORM 两个
--    都过滤了，等于自己把管道堵死。
--
-- 2) 本客户端没有 chatLog 这个 CVar（GetCVar 返回 nil），但 LoggingChat()
--    是真生效的 —— 实测 WoWChatLog.txt 确实被 Arden.exe 占着句柄。
--
-- 3) 但是！客户端把日志缓存在内存里，只在缓冲区满（约 96KB）或客户端
--    退出时才写盘。所以必须主动逼它 flush，见 beginFlush()。
--
--    这里有个大坑，别重走：「关掉再打开」必须跨两帧。
--    同一次调用里连着 LoggingChat(false) + LoggingChat(true) 是**无效的** ——
--    客户端的关闭是延迟的，同一帧内重开会把这次关闭直接取消，缓冲区不落盘。
--    铁证：11:12 截图时日志还停在 99 行 / 10:31，而 /atai diag 的 T1~T4
--    是 11:10:16 发的 —— 当时它们并不在日志里。后来 11:21 日志一次性暴涨
--    98KB，那是客户端缓冲区自己满了，跟 toggle 无关。
--    => 正确做法：beginFlush() 关掉并起计时器，OnUpdate 里到点再 finishFlush()。
--
--    还有一点：关闭期间**必须暂停发送**。日志没开的时候发出去的消息不会
--    被记录，等于白发。所以队列里挂着，等重开之后再发，一条都不会丢。
--
-- 另外两条实测结论（很反直觉，但日志里白纸黑字）：
--
-- * 过滤器只挡「显示」，不挡「日志」。日志是 C 层在事件派发时就写的，
--   早于 Lua 的 ChatFrame_MessageEventHandler。证据：filter=whisper 时
--   CHAT_MSG_WHISPER 已被过滤，日志里却照样有「猎魔人悄悄地说：@ATAI1@...」。
--   => 所以 filter all 可以做到「聊天框里完全看不见 + 日志照常写」。
--
-- * 一条自密语会产生两行日志（收 + 发），桥接靠 seq/内容哈希去重。
--
-- 4) v1.0.6 / v1.0.7 追加调查 —— 已经实测出结果了：
--
--    a) 客户端确实把 Blizzard Internal 那批 API 放开了。G 行实测：
--       AppendToFile / ReadFile / DeleteFile / debugdump / debugprint /
--       debuginfo / debugload / debugtimestamp / ConsoleExec / Screenshot
--       **全部是 function**。
--
--    b) 但调用一律返回  ERR: Access Denied 。
--       连 ReadFile 读一个**不存在**的文件也报 Access Denied（本该是
--       「文件不存在」）—— 说明这是一刀切的权限门，不是路径写错。
--       => 文件通道是死路，别再试路径了。
--
--    c) 落盘时机也彻底搞清楚了，只有两个：
--         1. 缓冲区满（11:21 那次一次涨了 98KB）
--         2. 客户端退出（12:56:32 那次，涨到 179020 字节）
--       LoggingChat(false)/(true) 跨帧也不刷盘，句柄一直攥在客户端手里
--       （独占打开返回 err=32）。所以聊天日志的延迟天然是「几十分钟」级，
--       12:57 之后聊的内容要等退出游戏才会出现。
--
--    d) v1.0.7 实测：debugprint / debuginfo / debugdump 调用**全部返回 OK**，
--       但 Logs\LUA.txt 里**没有出现任何 ATAI-PROBE** —— 正式版客户端里
--       这几个 debug* 函数是空转的接收器，写不到任何文件。这条路也断了。
--       （LUA.txt 只在 /reload 时多一行 *FrameXML.toc Loaded*，那是客户端
--        自己内部写的，插件碰不到。）
--
--    e) v1.0.8 两个新方向：
--       1. Logs\MissingFiles.txt 是**游戏过程中实时追加**的（12:55:54 那次
--          没有任何 /reload，它却从 9024 涨到 9646 字节）。里面记的是
--          「尝试加载但找不到的资源」，连 GameFontHighlightSmall、(listfile)、
--          tos.html 这种裸字符串都原样记下来 —— 如果插件能让客户端去加载
--          一个不存在的贴图、路径里带上我们的内容，那就是一条实时通道。
--          => /atai misstest 实测
--       2. LoggingChat(false) 到底有没有生效，之前从来没验证过（只验证过
--          「设 true 后读回 true」，而状态本来就是 true，等于没验证）。
--          现在监听 ADDON_ACTION_BLOCKED/FORBIDDEN，并且设 false 后下一帧读回。
--          => /atai flushtest 实测
--
--    f) v1.0.9 —— **通道找到了，就是 MissingFiles.txt**。
--       /atai misstest 实测（13:44:01）：SetTexture("Interface\ATAI\P134401.blp")
--       这类字符串会在**同一秒**落进 Logs\MissingFiles.txt，而且原样保留
--       （%7C 不解码、裸字符串也照收、自动补 .blp/.tga 各一条）。
--       当时客户端还在运行（独占打开 WoWChatLog.txt 仍返回 err=32），
--       所以确认是**实时写盘**，不是退出刷盘。
--       => 插件现在把每条消息同时用 SetTexture 发一遍（通道 B），
--          桥接端读 MissingFiles.txt 即可，延迟从几十分钟降到秒级。
--          /atai chan chatlog|miss|both 切换，默认 both 互为备份。
--
--    另外 /atai flushtest 的结论：J0 读回 1、J2 读回 nil、被拦次数=0。
--    这个客户端用 **1 / nil** 表示开关状态（不是 true/false），所以
--    LoggingChat(false) **确实生效了**（状态从开变关），只是客户端关闭日志时
--    **根本不刷缓冲区**。也就是说聊天日志的延迟无法靠 toggle 解决，已经判死。
--
--    g) v1.0.10 —— 三个稳健性修正，都是「不报错但结果是错的」那类隐患：
--       1. SESSION 现在用 string.format("%d", math.floor(t)) 强制**纯数字**。
--          桥接端靠「第 2、3 段都是数字」区分 6 段新格式与 5 段旧格式；如果
--          time() 返回带小数的值，tostring 会给出 "1776000001.5"，桥接端
--          判定失败退回 5 段分支，于是 session 被当成 seq、seq 被当成事件名
--          —— 整条消息错位且不报错。
--       2. /atai test 的直发探测原来把 "direct" 放在 seq 槽，同样会触发上面的
--          错位。改成 9001，现在这条诊断输出是可信的。
--       3. 桥接端通道 B 改为**只认 6 段格式**（require_six）：格式对不上直接
--          丢掉，不再退回 5 段错位解析。通道 A 仍兼容历史 5 段日志。
--
--    h) v1.0.11 —— 由 /atai test 的「假重复」反查出来的三处问题：
--       1. **自动刷盘默认关闭**。实测：刷盘次数=19，但 WoWChatLog.txt 的 mtime
--          停在 15:00:04，22 分钟没动 —— 关日志并不会让客户端写盘（早就判死了），
--          而这个动作有副作用：关闭的那 1 秒客户端不记录聊天，反而在通道 A 上
--          戳出空洞。既然通道 B 已实测实时可用，就没必要再破坏通道 A 的完整性。
--          想恢复旧行为：/atai autoflush on
--       2. **/atai test 的探针文本全部改成互不相同、且纯英文**。旧版两条探针
--          文本完全一样，桥接端会翻出两条一模一样的中文 —— 看起来像重复 bug。
--          更隐蔽的是第三条探针 "ArdenTranslator 探测 <时间>"：它本意是「中文」，
--          但拉丁字母 15 个、汉字只有 2 个，桥接端的语言判定会算成英文并送去翻译，
--          于是又撞出一条重复。现在三条探针自带路径标签（probe1/2/3）。
--       3. status 里 `ChatLog:ON CVar=nil LoggingChat()=nil` 自相矛盾：前者读的是
--          我们自己的 SavedVariables「意图」，后者是客户端实时状态。现在拆成
--          「插件想要=」和「客户端实际=」，不一致时直接标红。
--       另外「当前状态: 日志关闭中（等待重开）」改成了不吓人的说法，并说明
--       「日志 0」是正常的（通道 A 要等客户端自己刷盘）。
--
--    i) v1.0.12 —— 只为「换到别的私服也能用」而升，**插件行为零变化**：
--       1. 删掉诊断探针里写死的绝对路径 `C:/WOW/ArdenWoW/Wotlk/Logs/ATAI.txt`。
--          在别人的服上那条路径不存在，/atai diag 会多打一行无意义的 ERR。
--          其余探针路径本来就是相对的，通用。
--       2. 把「频道过滤只看事件类型」这一点写进注释固化下来：
--          shouldForward() 走 eventToSetting[event]，CHAT_MSG_CHANNEL -> "channel"，
--          **不读频道名、也不读频道号**。所以别的私服频道叫 `2. Newcomers`
--          还是 `World`，行为完全一致，不需要任何配置。
--
--    j) v1.0.13 —— 悬浮窗显示真实频道名 + 左上角「屏蔽」设置面板：
--       1. **公共频道消息现在带上频道名**。CHAT_MSG_CHANNEL 的 arg4 是
--          「带序号的频道名」（如 "2. Newcomers"、"5. 世界"），悬浮窗原来只显示
--          笼统的「[频道]」，现在直接显示 "[2. Newcomers]"。
--          **payload 仍是 6 段**：只是第 4 段（标签段）在有频道名时填频道名、
--          否则填事件名。这样旧版 EXE 靠 EVENT_LABELS 的兜底也能正确显示，
--          且不给切分引入任何歧义 —— 比加第 7 段安全得多。
--       2. 配合桥接端的「屏蔽」功能：面板里屏蔽某个频道后，桥接端会在**翻译前**
--          就丢掉该频道的消息，既不看也不花钱。插件端无需改动（仍在转发，
--          只是本地 I/O，不产生 API 调用）。
--
--    k) v1.0.14 —— 修 v1.0.13 引入的**公共频道完全翻译不了**的回归。
--       现象：悬浮窗里密语正常，公共频道一条都没有（实时计数也不涨）。
--       根因：**点号**。客户端把 SetTexture 的路径当文件名，在**最后一个点号**
--       处切分，把后半段当扩展名替换成 .blp / .tga。v1.0.13 把频道名
--       "2. Newcomers" 放进了标签段，点号后面还有作者和正文 —— 整条被截成
--       "2.blp"，只剩 4 段，桥接端按 require_six 直接丢弃，静默无提示。
--       证据：同一会话的两条通道逐条配对（聊天日志存的是原文，不受路径规则
--       影响），116 条样本里 14 条被改写，**全部**由「最后一个点号」解释，
--       空格 / 逗号 / 问号 / 冒号 / 星号 / 等号 / % 全部无损（% 序列 116/116
--       原样保留，所以 %2E 是安全载体）。
--       修法：esc() 里把点号转义成 %2E，桥接端 unesc 时还原。
--       顺带修好一个一直存在的静默截断：以前**任何带句号的消息**都会被砍掉
--       最后一个点号之后的全部内容（如 "Might. It adds defence stuff" 只剩
--       "Might"）。现在完整保留。
--       另外 /atai test 的三条探针都改成带点号，probe2 还特意用带点号的频道名
--       当标签 —— 一跑就能确认修复在真实客户端里生效。
--
-- 三种过滤模式（/atai filter off|whisper|all）：
--   all      默认。收发两个事件都藏，聊天框完全干净
--   whisper  只藏服务器回显
--   off      什么都不藏（排错用）

local ADDON = "ArdenTranslatorAI"
-- v1.1：桥接端与插件端**各自独立**演进，版本号不必统一。桥接端同版本号见
-- WindowsBridge/ArdenTranslatorAI.py 的 VERSION。协议兼容范围：
--   支持 ATAI2（6 段，通道 B）与 @ATAI1@（6 段 / 兼容历史 5 段，通道 A）。
local VERSION = "1.1.0"
-- 协议版本标记。桥接端据此判断「这个插件会不会说我现在听得懂的话」。
-- 只增不改：1 = ATAI1 5 段，2 = ATAI1/ATAI2 6 段，3 = 预留（分片）。
local PROTOCOL = 2
local PREFIX = "@ATAI1@"
local frame = CreateFrame("Frame")
local seq = 0
local sendQueue = {}
local elapsedSinceSend = 0

-- 运行期开关：不写进 SavedVariables，/reload 后回默认
local debugForwarding = false
local filterMode = "all"      -- 实测：过滤只挡显示、不挡日志，所以两个都藏最舒服
local sinkName = nil          -- nil = 密语给自己
local hiddenSink = nil
-- 自动刷盘总开关。**v1.0.11 起默认关闭**，因为实测证明它没有任何作用，反而有害：
--   实测证据：/atai status 显示「刷盘次数=19」，但 WoWChatLog.txt 的 mtime
--   停在 15:00:04，整整 22 分钟没动过 —— 关掉日志并不会让客户端把缓冲区写盘。
--   而这个动作本身**是有副作用的**：日志关闭的那 1 秒里客户端不记录任何聊天，
--   于是通道 A 反而被戳出一堆空洞。既然通道 B（MissingFiles）已经实测实时可用，
--   就没必要再为了一个无效动作去破坏通道 A 的完整性。
--   想恢复旧行为：/atai autoflush on
local autoFlush = false
local FLUSH_INTERVAL = 6.0    -- 定期刷盘间隔（空闲时靠它把日志撑「活」）
local FLUSH_MIN_GAP = 2.0     -- 发完消息后至少隔这么久才刷一次
local flushTimer = 6.0
local needFlush = false       -- 刚发完消息，想立刻刷一次
local sinceFlush = 0          -- 距上次触发刷盘的秒数

local defaults = {
    enabled = true,
    guild = true,
    party = true,
    raid = true,
    whisper = true,
    say = false,
    yell = false,
    battleground = true,
    channel = true,
    officer = true,
    logging = true,
}

local eventToSetting = {
    CHAT_MSG_GUILD = "guild",
    CHAT_MSG_OFFICER = "officer",
    CHAT_MSG_PARTY = "party",
    CHAT_MSG_PARTY_LEADER = "party",
    CHAT_MSG_RAID = "raid",
    CHAT_MSG_RAID_LEADER = "raid",
    CHAT_MSG_RAID_WARNING = "raid",
    CHAT_MSG_WHISPER = "whisper",
    CHAT_MSG_SAY = "say",
    CHAT_MSG_YELL = "yell",
    CHAT_MSG_BATTLEGROUND = "battleground",
    CHAT_MSG_BATTLEGROUND_LEADER = "battleground",
    CHAT_MSG_CHANNEL = "channel",
}

local function ATPrint(msg)
    DEFAULT_CHAT_FRAME:AddMessage("|cff58c7ffArdenTranslator AI|r " .. tostring(msg))
end

local function copyDefaults()
    if not ArdenTranslatorAIDB then ArdenTranslatorAIDB = {} end
    for k, v in pairs(defaults) do
        if ArdenTranslatorAIDB[k] == nil then
            ArdenTranslatorAIDB[k] = v
        end
    end
end

local function esc(s)
    s = tostring(s or "")
    s = string.gsub(s, "%%", "%%25")
    s = string.gsub(s, "|", "%%7C")
    -- v1.0.14：**点号也必须转义**，这是踩出来的。
    -- 客户端把 SetTexture 的路径当文件名处理：在**最后一个点号**处切分，把后半段
    -- 当扩展名，直接替换成 .blp / .tga 各记一行。所以 payload 里任何点号后面的
    -- 内容都会被吃掉。实测证据（同一会话，聊天日志 vs MissingFiles 逐条配对）：
    --     "Might. It adds defence stuff"   -> 记成 "Might.blp"
    --     "I like the model though...its just to grindy"
    --                                      -> 记成 "I like the model though...tga"
    --    频道名 "2. Newcomers" 在标签段里，点号后面还有作者+正文，整条被截成
    --     "2.blp" —— 只剩 4 段，桥接端按 require_six 直接丢弃，**公共频道因此
    --    完全翻译不了**（v1.0.13 引入的回归）。
    -- 顺带这还修好了一直存在的静默截断：以前任何带句号的消息都会被砍掉尾巴。
    -- %2E 是安全的：客户端对 % 转义序列原样保留（实测 116/116 条含 % 的 payload
    -- 完好，%7C、%25 都不受影响），桥接端 unesc 时还原成 "."。
    -- 注意只转义点号就够 —— 空格、逗号、问号、冒号、星号、等号都实测无损。
    s = string.gsub(s, "%.", "%%2E")
    s = string.gsub(s, "\r", " ")
    s = string.gsub(s, "\n", " ")
    return s
end

local function isOwnBridgeMarker(msg)
    return type(msg) == "string" and string.sub(msg, 1, string.len(PREFIX)) == PREFIX
end

-- ------------------------------------------------------------- 过滤器
local function filterWhisper(self, event, msg, author, ...)
    if isOwnBridgeMarker(msg) then return true end
    return false
end

local function filterWhisperInform(self, event, msg, author, ...)
    if isOwnBridgeMarker(msg) then return true end
    return false
end

local function applyFilters()
    if ChatFrame_RemoveMessageEventFilter then
        ChatFrame_RemoveMessageEventFilter("CHAT_MSG_WHISPER", filterWhisper)
        ChatFrame_RemoveMessageEventFilter("CHAT_MSG_WHISPER_INFORM", filterWhisperInform)
    end
    if not ChatFrame_AddMessageEventFilter then return false end

    if filterMode == "whisper" or filterMode == "all" then
        ChatFrame_AddMessageEventFilter("CHAT_MSG_WHISPER", filterWhisper)
    end
    if filterMode == "all" then
        ChatFrame_AddMessageEventFilter("CHAT_MSG_WHISPER_INFORM", filterWhisperInform)
    end
    return true
end

local function setFilterMode(mode)
    if mode ~= "off" and mode ~= "whisper" and mode ~= "all" then
        return false
    end
    filterMode = mode
    applyFilters()
    return true
end

-- --------------------------------------------------------- 聊天日志开关
local function loggingState()
    local cvar = "?"
    if GetCVar then
        local ok, v = pcall(GetCVar, "chatLog")
        cvar = ok and tostring(v) or ("ERR:" .. tostring(v))
    end
    local flag = "?"
    if LoggingChat then
        local ok, v = pcall(LoggingChat)
        flag = ok and tostring(v) or ("ERR:" .. tostring(v))
    end
    return cvar, flag
end

local function ensureChatLogging()
    if not LoggingChat then
        return "LoggingChat=nil（本客户端没有这个 API）"
    end
    local want = ArdenTranslatorAIDB.logging and true or false
    local ok, ret = pcall(LoggingChat, want)
    if not ok then ret = "ERR:" .. tostring(ret) end

    -- 三重保险：LoggingChat / SetCVar / ConsoleExec 各试一遍。
    -- 本客户端 GetCVar("chatLog") 返回 nil，说明没有这个 CVar，
    -- 后两条多半无效，但成本为零。
    if want then
        if SetCVar then pcall(SetCVar, "chatLog", "1") end
        if ConsoleExec then pcall(ConsoleExec, "chatLog 1") end
    end

    local cvar = loggingState()
    return "LoggingChat(" .. tostring(want) .. ")=" .. tostring(ret) .. "  CVar chatLog=" .. cvar
end

-- ------------------------------------------------------------ 强制刷盘
-- 本客户端把聊天日志缓存在内存里，只在缓冲区满（约 96KB）或客户端退出时才写盘。
--
-- 【v1.1 更正】下面这段「关掉再打开」的推理是**当年基于错误假设写的**，保留
-- 作为调查记录，但结论已被后续实测推翻，不要照它去做事：
--   错误假设：跨帧 LoggingChat(false) -> LoggingChat(true) 能让客户端把缓冲刷盘。
--   实测结果（见文件头 c)/h) 条）：**不会**。缓冲区只在满或退出时落盘，
--   toggle 多少次都没用；而且关闭的那 1 秒客户端不记录聊天，通道 A 反而被
--   戳出空洞。所以 v1.0.11 起 autoFlush 默认关闭，v1.1 继续默认关闭，
--   并且 /atai test、/atai diag 不再偷偷调它。
--   => 想验证链路请用通道 B（秒级）；通道 A 天然是「几十分钟/退出时」级，
--      不要把它当实时通道。
--
-- 下面的两帧状态机代码仍然保留，只服务于 /atai flush（手动、显式）和
-- /atai flushtest（调试、需确认）。正常路径不碰它。
--
-- 关键教训：「关掉再打开」必须在**两个不同的帧**里做。
-- 同一次调用里连着 LoggingChat(false) + LoggingChat(true) 是无效的 ——
-- 客户端的关闭是延迟的，同一帧内重开会把这次关闭直接取消，缓冲区根本不落盘。
--
-- 铁证：第 8 轮 11:12 截图时日志还停在 99 行 / 10:31，而 /atai diag 的
-- T1~T4 是 11:10:16 发的 —— 当时它们并不在日志里。后来 11:21 日志一次性
-- 暴涨 98KB，那是客户端缓冲区自己满了，跟这个 toggle 没关系。
--
-- 所以现在改成两步：
--   beginFlush()  -> LoggingChat(false)，并起一个 CLOSE_HOLD 秒的计时器
--   OnUpdate      -> 计时到点再 LoggingChat(true)
-- 中间这段「关着」的窗口就是客户端真正把缓冲写下去的时间。
local CLOSE_HOLD = 1.0       -- 日志保持关闭的时长（秒）
local reopenTimer = 0        -- >0 表示正处在「已关闭、等待重开」状态
local flushCount = 0         -- 完成的刷盘次数
local flushFail = 0          -- 重开失败次数

-- v1.0.8：被保护函数拦截的计数。如果 LoggingChat 之类的调用被拦，
-- 客户端会派发 ADDON_ACTION_BLOCKED/FORBIDDEN —— 这是判断「设了没生效」
-- 的直接证据，之前几轮一直没监听，等于瞎猜。
local blockedCount = 0
local blockedInfo = ""

-- /atai flushtest 的状态机：设 LoggingChat(false)，下一帧读回来看有没有生效。
local probeStage = 0
local probeTimer = 0
-- v1.1（V11-05）：flushtest 执行前的 autoFlush 原值，结束时恢复，不再写死 true。
local probeAutoFlushBackup = nil

local function beginFlush()
    if not LoggingChat or not ArdenTranslatorAIDB.logging then return false end
    if reopenTimer > 0 then return false end   -- 已经关着了，别重复关
    local ok = pcall(LoggingChat, false)
    if not ok then return false end
    reopenTimer = CLOSE_HOLD
    return true
end

-- 兼容旧名字：调用方只需要「触发一次刷盘」
local function forceFlush()
    return beginFlush()
end

local function finishFlush()
    reopenTimer = 0
    local ok = pcall(LoggingChat, true)
    if not ok then
        flushFail = flushFail + 1
    end
    -- 保险：万一没开回来，日志会永久冻结，立刻补开
    local ok2, v = pcall(LoggingChat)
    if ok2 and v == false then
        pcall(LoggingChat, true)
        flushFail = flushFail + 1
    end
    flushCount = flushCount + 1
    flushTimer = FLUSH_INTERVAL
    sinceFlush = 0
end

-- ============ 传输通道 B：MissingFiles（v1.0.9，实测可用，实时） ============
-- 实测证据（13:44:01 那次 /atai misstest；当时客户端还在运行，
-- 独占打开 WoWChatLog.txt 仍返回 err=32，所以**不是**退出刷盘）：
--   SetTexture("Interface\ATAI\P134401.blp") -> 落盘 Interface\ATAI\P134401.blp / .tga
--   SetTexture("ATAI-RAW-134401")            -> 落盘 ATAI-RAW-134401.blp / .tga
--   SetTexture("ATAI%7Cesc%7C134401")        -> 落盘 ATAI%7Cesc%7C134401.blp / .tga
--   PlaySoundFile("Sound\ATAI\P134401.wav")  -> 落盘 Sound\ATAI\P134401.wav
-- 七条全部在**同一秒**写进 Logs\MissingFiles.txt。
--
-- 【重要，v1.1 更正】上面的「实时」是**针对这台 ArdenWoW 客户端实测**的结论，
-- 不是所有 WoW 私服都提供的通用能力。它依赖客户端把缺失资源日志写到
-- Logs\MissingFiles.txt，且在该文件上不缓冲。换服/换客户端版本后必须重新实测
-- （`/atai misstest`），不能假设一定可用。桥接端的状态栏会如实显示通道 B 是
-- 否真的有数据进来，而不是只看 SetTexture 是否返回成功。
--
-- 客户端的行为（都是实测出来的）：
--   * 把字符串当贴图基名，自动补 .blp 和 .tga 各记一条（一条 payload 两行）
--   * %7C 原样保留，不转义也不解码
--   * 裸字符串也原样保留（文件里本来就有 GameFontHighlightSmall、(listfile)）
--   * 文件是「集合」语义，重复字符串只记一次 —— 所以 payload 里必须带自增 seq
--   * 整个文件会被重写（大小会涨也会跌），所以桥接端要按「整文件 + 已见集合」
--     去重，不能只靠文件偏移量 tail
--
-- 于是：把 payload 当贴图路径丢给 SetTexture，就得到一条**实时、不占聊天框、
-- 不需要自密语**的单向通道。桥接端的解析规则：行首是 ATAI2 -> 去掉结尾的
-- .blp/.tga -> unesc -> 按 | 切分。
local MISS_PREFIX = "ATAI2"     -- 和聊天日志用的 @ATAI1@ 区分开，两条可并存
local chanMode = "both"         -- chatlog | miss | both
local missFrame = nil
local missTex = nil
local missSent = 0
local missFail = 0

-- 会话唯一串。MissingFiles 是「集合」语义：完全相同的字符串只会被记一次。
-- 没有它的话，新会话的第 1 条消息会和上一次会话的第 1 条撞车，客户端直接
-- 不记 —— 那就是一条消息凭空消失，而且极难排查。每次 /reload 都会重新生成。
--
-- v1.1（V11-04）：秒级 time() 在「快速 /reload」时有真实碰撞概率（同一秒内
-- 两次加载 -> 同一个 SESSION -> 桥接端把新会话当旧会话，或者反过来说
-- 新会话的第 1 条被当成上次会话的旧记录挡掉）。现在改成三段拼接：
--
--     <秒级时间><本机 reload 计数><两位随机尾数>
--
-- **必须是纯数字**（桥接端靠「第 2、3 段都是数字」区分 6 段与 5 段），
-- 且**必须是字符串拼接**、不能当浮点运算：Lua 传统实现里数字是 double，
-- 安全整数上限 2^53。拼接后的十进制串可以很长，只要不去 tonumber() 就不会
-- 丢精度。桥接端把 session 当**字符串**比较，不转数字，所以长度无所谓。
--
-- reload 计数持久化在 SavedVariables 里（每个角色一份），所以同一秒内连续
-- reload 一定拿到不同的计数；即使跨角色、跨机器，还有随机尾数兜底。
local SESSION = (function()
    local t = 0
    if type(time) == "function" then
        local ok, v = pcall(time)
        if ok and type(v) == "number" and v > 0 then t = v end
    end
    if t == 0 and type(GetTime) == "function" then
        local ok, v = pcall(GetTime)
        if ok and type(v) == "number" then t = v * 1000 end
    end
    t = math.floor(t)

    -- reload 计数：SavedVariables 里自增。不能用 ArdenTranslatorAIDB.logging
    -- 之类业务字段，单独占一个键，避免被 copyDefaults 清掉。
    local counter = 0
    if type(_G.ArdenTranslatorAIDB) == "table" then
        local c = tonumber(ArdenTranslatorAIDB.sessionCounter)
        if c and c >= 0 and c < 1e9 then counter = math.floor(c) end
        ArdenTranslatorAIDB.sessionCounter = counter + 1
        counter = ArdenTranslatorAIDB.sessionCounter
    end

    -- 两位随机尾数。random 只当**额外**保险，不作为唯一性依据。
    local rnd = 0
    if type(math.random) == "function" then
        local ok, v = pcall(math.random, 0, 99)
        if ok and type(v) == "number" then rnd = math.floor(v) end
    end

    -- 全部走字符串拼接，**不**做算术：%d 只用于把 Counter/随机数格式化成
    -- 定宽十进制，避免 "1" 和 "01" 这种歧义（其实无所谓，但定宽更整齐）。
    return string.format("%d", t)
        .. string.format("%03d", counter % 1000)
        .. string.format("%02d", rnd)
end)()


local function ensureMissTexture()
    if missTex then return missTex end
    local ok = pcall(function()
        missFrame = CreateFrame("Frame", nil, UIParent)
        missFrame:SetSize(1, 1)
        missFrame:SetPoint("TOPLEFT", UIParent, "TOPLEFT", -4000, 4000)
        missTex = missFrame:CreateTexture(nil, "BACKGROUND")
    end)
    if not ok then
        missTex = nil
        missFail = missFail + 1
    end
    return missTex
end

-- 往 MissingFiles 通道发一条 payload
local function emitMissing(payload)
    local tex = ensureMissTexture()
    if not tex then return false end
    local ok = pcall(function() tex:SetTexture(payload) end)
    if ok then
        missSent = missSent + 1
    else
        missFail = missFail + 1
    end
    return ok
end

-- ------------------------------------------------------------- 队列发送
-- channelName：公共频道专用。CHAT_MSG_CHANNEL 的 arg4 是带序号的频道名
-- （如 "2. Newcomers"），悬浮窗要显示它而不是笼统的「频道」。
-- **格式没有变**：仍然 6 段，只是第 4 段（标签段）在有频道名时填频道名、
-- 否则填 WoW 事件名。这样桥接端 `EVENT_LABELS.get(event, event)` 天然兜底，
-- 连旧版 EXE 都能正确显示频道名，也不会给切分引入任何歧义。
local function queueForBridge(event, msg, author, channelName)
    if #sendQueue >= 60 then
        table.remove(sendQueue, 1)
    end
    seq = seq + 1
    -- 注意：连分隔符也用 %7C（转义后的竖线），整条 payload 里不出现任何裸 |。
    -- 因为 WoW 聊天里 | 是转义起始符（|c 颜色、|T 贴图、|H 链接），
    -- 像「|Telomeres」这种刚好以 |T 开头的内容会被客户端当成贴图转义吃掉。
    -- 桥接端会先 unesc 还原，再按 | 切分。
    local label = (channelName and channelName ~= "") and channelName or event
    local body = SESSION .. "%7C" .. tostring(seq) .. "%7C" .. esc(label) .. "%7C" .. esc(author) .. "%7C" .. esc(msg)

    -- 通道 B：MissingFiles —— 实时，走贴图路径
    if chanMode == "miss" or chanMode == "both" then
        emitMissing(MISS_PREFIX .. "%7C" .. body)
    end
    -- 通道 A：聊天日志 —— 有缓冲，要等客户端退出才刷盘
    if chanMode == "chatlog" or chanMode == "both" then
        table.insert(sendQueue, PREFIX .. "%7C" .. body)
    end
end

local function shouldForward(event, msg, author)
    if not ArdenTranslatorAIDB.enabled then return false end
    local key = eventToSetting[event]
    if not key or not ArdenTranslatorAIDB[key] then return false end
    if isOwnBridgeMarker(msg) then return false end

    -- 自己的消息不翻译
    local me = UnitName("player")
    if author and me and string.lower(author) == string.lower(me) then
        return false
    end
    return true
end

-- 隐藏接收框：如果客户端连插件 AddMessage 也会写日志，这是最干净的通道
local function getHiddenSink()
    if hiddenSink then return hiddenSink end
    local ok, f = pcall(CreateFrame, "ScrollingMessageFrame", "ArdenTranslatorAIHiddenSink", UIParent)
    if not ok or not f then return nil end
    f:SetPoint("TOPLEFT", UIParent, "TOPLEFT", -3000, 3000)
    f:SetSize(1, 1)
    f:SetMaxLines(20)
    f:SetTimeVisible(2)
    f:Hide()
    hiddenSink = f
    return f
end

frame:SetScript("OnEvent", function(self, event, ...)
    -- 被保护函数拦下来时会走这里。之前几轮没监听这两个事件，
    -- 所以「LoggingChat(false) 到底有没有生效」一直只能靠猜。
    if event == "ADDON_ACTION_BLOCKED" or event == "ADDON_ACTION_FORBIDDEN" then
        local addonName, fnName = ...
        blockedCount = blockedCount + 1
        blockedInfo = tostring(addonName) .. " / " .. tostring(fnName)
        ATPrint("|cffff5555" .. event .. "|r 被拦：" .. blockedInfo
            .. "（累计 " .. blockedCount .. " 次）")
        return
    end

    if event == "ADDON_LOADED" then
        local name = ...
        if name == ADDON then
            copyDefaults()
            applyFilters()
            local state = ensureChatLogging()
            ATPrint("v" .. VERSION .. " 已加载。" .. state)
            ATPrint("没翻译就输入 |cffffff00/atai diag|r 自检。")
        end
        return
    end

    if event == "PLAYER_LOGIN" then
        copyDefaults()
        applyFilters()
        ensureChatLogging()
        return
    end

    local msg, author = ...
    -- 公共频道：arg4 是「带序号的频道名」，如 "2. Newcomers" / "5. 世界"。
    -- 其他事件没有这个参数（arg4 是别的东西，比如语言/旗标），所以只对
    -- CHAT_MSG_CHANNEL 取。取不到就留空，桥接端会退回显示「频道」。
    local channelName = ""
    if event == "CHAT_MSG_CHANNEL" then
        local chan = select(4, ...)
        if type(chan) == "string" and chan ~= "" then
            channelName = chan
        end
    end
    if shouldForward(event, msg, author) then
        if debugForwarding then
            ATPrint("转发 " .. tostring(event) .. " <" .. tostring(author) .. "> " .. tostring(msg)
                .. (channelName ~= "" and ("  |cff88ff88频道=" .. channelName .. "|r") or ""))
        end
        queueForBridge(event, msg, author, channelName)
    end
end)

frame:SetScript("OnUpdate", function(self, elapsed)
    -- ---- /atai flushtest 第 2 步：0.5 秒后把 LoggingChat() 读回来 ----
    -- 关键：之前只验证过「设 true 后读回 true」，那说明不了问题（状态本来就是
    -- true）。这次反过来设 false 再读：读回 false 才叫生效。
    if probeStage == 1 then
        probeTimer = probeTimer + elapsed
        if probeTimer >= 0.5 then
            probeStage = 0
            local after = tostring(select(2, loggingState()))
            ATPrint("J2. 0.5 秒后 LoggingChat()=" .. after)
            if after == "false" then
                ATPrint("    |cff00ff00setter 生效|r —— 那问题就在客户端「关了也不刷盘」。")
            else
                ATPrint("    |cffff5555setter 被忽略|r —— 关都关不掉，自然刷不了盘。")
            end
            -- v1.1（V11-05）：恢复**原来的**状态，而不是写死 autoFlush=true。
            -- 这个命令是调试工具，不该在用户机器上悄悄改长期行为。
            pcall(LoggingChat, true)
            if probeAutoFlushBackup ~= nil then
                autoFlush = probeAutoFlushBackup
                probeAutoFlushBackup = nil
            end
            ATPrint("    已恢复 LoggingChat(true)，自动刷盘=" ..
                (autoFlush and "ON" or "OFF") .. "（与执行前一致）。")
        end
    end

    -- ---- 刷盘第 2 步：关闭时间到，重新打开（关闭期间客户端才会真正落盘）----
    if reopenTimer > 0 then
        reopenTimer = reopenTimer - elapsed
        if reopenTimer <= 0 then
            finishFlush()
        end
    end

    -- ---- 刷盘第 1 步：定期心跳 + 发完立刻刷（有最小间隔）----
    if flushTimer > 0 then flushTimer = flushTimer - elapsed end
    sinceFlush = sinceFlush + elapsed

    if autoFlush and reopenTimer <= 0 then
        local due = (flushTimer <= 0)                            -- 定期心跳
        local urgent = needFlush and sinceFlush >= FLUSH_MIN_GAP  -- 刚发完，想立刻刷
        if due or urgent then
            needFlush = false
            flushTimer = FLUSH_INTERVAL
            sinceFlush = 0
            beginFlush()
        end
    end

    -- ---- 发送队列 ----
    -- 日志关闭期间不发：关着的时候发出去的消息不会被记录，等于白发。
    -- 留在队列里，等重开之后再发，这样一条都不会丢（代价只是最多 1 秒延迟）。
    if reopenTimer > 0 then return end

    elapsedSinceSend = elapsedSinceSend + elapsed
    if elapsedSinceSend < 0.18 then return end
    elapsedSinceSend = 0

    if #sendQueue == 0 then return end
    local me = UnitName("player")
    if not me or me == "" then return end

    local payload = table.remove(sendQueue, 1)
    -- 自密语是刻意为之：Lua 插件开不了 socket、也读不了文件，
    -- 只有聊天流量会被客户端写进 Logs\WoWChatLog.txt。
    SendChatMessage(payload, "WHISPER", nil, sinkName or me)
    if autoFlush then needFlush = true end
end)

frame:RegisterEvent("ADDON_LOADED")
frame:RegisterEvent("PLAYER_LOGIN")
frame:RegisterEvent("ADDON_ACTION_BLOCKED")
frame:RegisterEvent("ADDON_ACTION_FORBIDDEN")
for event, _ in pairs(eventToSetting) do
    frame:RegisterEvent(event)
end

-- ---------------------------------------------------------------- 自检
local function stamp()
    if date then
        local ok, v = pcall(date, "%H:%M:%S")
        if ok and v then return v end
    end
    return tostring(GetTime and math.floor(GetTime() * 1000) or 0)
end

-- ---------------------------------------------- 文件 API 探测（v1.0.6）
-- 目标：判断客户端有没有把 Blizzard Internal 那三个文件函数开放给插件。
-- 一旦开放，AppendToFile 就是理想通道 —— 它按行追加，不需要缓冲区满。
local FILE_API_NAMES = {
    "AppendToFile", "ReadFile", "DeleteFile",
    "debugdump", "debugprint", "debuginfo", "debugload",
    "debugbreak", "debugtimestamp",
    "Screenshot", "ConsoleExec", "SetConsoleKey",
    "LoggingChat", "LoggingCombat", "SetCVar", "GetCVar",
}

local function fileApiReport()
    local parts = {}
    for _, k in ipairs(FILE_API_NAMES) do
        parts[#parts + 1] = k .. "=" .. type(_G[k])
    end
    return table.concat(parts, "  ")
end

-- 真的去调一次，而不是只看类型。错误信息本身就很有信息量。
-- 最后一条 WTF/ 是为了排除「相对路径解析错了」这种可能：换一个常见可写目录再试。
-- 注意：**不要**写死某个私服的绝对路径 —— 插件在别的服上跑会多打一行无意义报错。
local FILE_PROBE_PATHS = {
    "Logs/ATAI.txt",
    "Logs\\ATAI.txt",
    "ATAI.txt",
    "WTF/ATAI.txt",
}

local function testFileTransport()
    ATPrint("|cffffff00=== 文件通道探测 v" .. VERSION .. " ===|r")
    ATPrint("G. " .. fileApiReport())

    if type(AppendToFile) ~= "function" then
        ATPrint("|cffff5555AppendToFile = " .. type(AppendToFile) .. "|r")
        ATPrint("插件拿不到写文件能力，只能继续走聊天日志那条链路。")
        return
    end

    local t = stamp()
    for _, path in ipairs(FILE_PROBE_PATHS) do
        local line = "ATAI-FILEPROBE " .. t .. " path=" .. path .. "\n"
        local ok, err = pcall(AppendToFile, path, line)
        ATPrint("AppendToFile(" .. path .. ") -> "
            .. (ok and "|cff00ff00OK|r" or ("|cffff5555ERR: " .. tostring(err) .. "|r")))
        if type(ReadFile) == "function" then
            local ok2, data = pcall(ReadFile, path)
            if ok2 then
                ATPrint("  ReadFile -> " .. tostring(data))
            else
                ATPrint("  ReadFile -> |cffff5555ERR: " .. tostring(data) .. "|r")
            end
        end
    end
    ATPrint("请把 G 行和每条 AppendToFile 的结果发我。")
    ATPrint("同时看一下 |cffffff00Logs|r 目录里有没有冒出 ATAI.txt。")
end

-- ------------------------------------------------ 实时日志通道（v1.0.7）
-- v1.0.6 实测结论：AppendToFile / ReadFile / DeleteFile 在 G 行里**都是 function**
-- （这客户端把 Blizzard Internal 那批放开了），但调用一律返回
--   ERR: Access Denied
-- 而且 ReadFile 读一个**不存在**的文件也报 Access Denied（本该是「文件不存在」）
-- => 这是一刀切的权限门，不是路径写错，再试路径没有意义。
--
-- 换一条路。Logs\LUA.txt 是**逐行实时**写盘的（实测 12:08:00 那条明显不是
-- /reload 触发的），格式是：
--   13:11:10 [Interface\FrameXML\FrameXMLPost.lua:4] *FrameXML.toc Loaded*
-- 那个 [文件:行] 正是 debugprint 一类的函数用 debugstack() 取调用点留下的痕迹。
-- 而 debugprint 在 G 行里是 function。
--
-- 如果这条成立，它就是理想的单向通道：实时、不需要自密语、不污染聊天框。
-- 用 /atai logtest 实测。
local function testLogChannel()
    ATPrint("|cffffff00=== 实时日志通道探测 v" .. VERSION .. " ===|r")

    local t = stamp()
    ATPrint("H. debugprint=" .. type(_G.debugprint)
        .. "  debuginfo=" .. type(_G.debuginfo)
        .. "  debugdump=" .. type(_G.debugdump)
        .. "  debugtimestamp=" .. type(_G.debugtimestamp)
        .. "  ConsoleExec=" .. type(_G.ConsoleExec))

    local function attempt(label, fn)
        local ok, err = pcall(fn)
        ATPrint(label .. " -> "
            .. (ok and "|cff00ff00OK|r" or ("|cffff5555ERR: " .. tostring(err) .. "|r")))
    end

    -- 主力假设：debugprint 写 Logs\LUA.txt
    if type(_G.debugprint) == "function" then
        attempt("debugprint(ATAI-PROBE-PRINT-" .. t .. ")",
            function() debugprint("ATAI-PROBE-PRINT-" .. t) end)
    end
    if type(_G.debuginfo) == "function" then
        attempt("debuginfo(ATAI-PROBE-INFO-" .. t .. ")",
            function() debuginfo("ATAI-PROBE-INFO-" .. t) end)
    end
    if type(_G.debugdump) == "function" then
        attempt("debugdump(ATAI-PROBE-DUMP-" .. t .. ")",
            function() debugdump("ATAI-PROBE-DUMP-" .. t) end)
    end

    -- 备选：控制台那条老路内部可能是另一套开关，也许真的会关文件、把缓冲刷下去
    if type(_G.ConsoleExec) == "function" then
        attempt("ConsoleExec(chatlog) 第 1 次", function() ConsoleExec("chatlog") end)
        attempt("ConsoleExec(chatlog) 第 2 次", function() ConsoleExec("chatlog") end)
        if LoggingChat then pcall(LoggingChat, true) end
        ATPrint("  已补一次 LoggingChat(true)，保证日志开关还是开的。")
    end

    ATPrint("时间戳 " .. t .. " —— 请把 H 行和每条结果截图给我。")
    ATPrint("我会去看 |cffffff00Logs\\LUA.txt|r 里有没有出现 ATAI-PROBE。")

    -- 如果 debug* 都不是那条通道，就把所有名字像「日志 / 写文件」的全局列出来，
    -- 下一轮直接挑对的那个调。**只列不调** —— 免得误触 Logout 之类的东西。
    --
    -- 上一版这里没过滤类型，结果 "log" 把 DIALOG / COMBATLOG / QUESTLOG 全匹配上，
    -- 一口气列出 1326 条本地化字符串，全是噪音。现在只留 function，
    -- 并且排除带下划线的常量名（BINDING_NAME_*、BLIZZARD_COMBAT_LOG_* 那些）。
    local KEYWORDS = { "log", "print", "dump", "trace", "file", "write", "console", "script" }
    local hits = {}
    pcall(function()
        for k, v in pairs(_G) do
            if type(v) == "function" then
                local ks = tostring(k)
                if not string.find(ks, "_", 1, true) then
                    local lower = string.lower(ks)
                    for _, kw in ipairs(KEYWORDS) do
                        if string.find(lower, kw, 1, true) then
                            hits[#hits + 1] = ks
                            break
                        end
                    end
                end
            end
        end
    end)
    table.sort(hits)
    ATPrint("I. 名字像日志/文件的**函数**共 " .. tostring(#hits) .. " 条（只列不调）：")
    for _, h in ipairs(hits) do
        ATPrint("  " .. h)
    end
end

-- ------------------------------------------- MissingFiles 通道（v1.0.8）
-- 实测发现 Logs\MissingFiles.txt 是**在游戏过程中**实时追加的：
-- 12:55:54 那次没有任何 /reload，它却从 9024 涨到了 9646 字节。
--
-- 里面记的是「客户端尝试加载但找不到的资源」，而且像
--   GameFontHighlightSmall / (listfile) / tos.html
-- 这种根本不是路径的裸字符串也会被原样记下来。
--
-- 所以：只要能让客户端去加载一个不存在的贴图，路径里带上我们的内容，
-- 内容就会出现在 MissingFiles.txt 里 —— 一条不需要自密语、不占聊天框的通道。
-- 用 /atai misstest 实测三种写法，看哪一种能原样落地。
local function testMissingFileChannel()
    ATPrint("|cffffff00=== MissingFiles 通道探测 v" .. VERSION .. " ===|r")

    local t = string.gsub(stamp(), ":", "")   -- 13:17:24 -> 131724
    local host = CreateFrame("Frame", nil, UIParent)
    host:SetSize(1, 1)
    host:SetPoint("TOPLEFT", UIParent, "TOPLEFT", -4000, 4000)

    local tex
    local okTex, errTex = pcall(function()
        tex = host:CreateTexture(nil, "BACKGROUND")
    end)
    if not okTex or not tex then
        ATPrint("|cffff5555CreateTexture 不可用：|r" .. tostring(errTex)
            .. " —— 这条探测做不了。")
        return
    end

    -- 三种写法，验证客户端到底会不会「原样」记录：
    --   K1 标准贴图路径（最接近客户端自己的行为）
    --   K2 完全不像路径的裸字符串（MissingFiles 里确实出现过 GameFontHighlightSmall）
    --   K3 带 %7C 转义（万一裸 | 会被客户端当转义起始符吃掉）
    local variants = {
        "Interface\\ATAI\\P" .. t .. ".blp",
        "ATAI-RAW-" .. t,
        "ATAI%7Cesc%7C" .. t,
    }
    for i, p in ipairs(variants) do
        local ok, err = pcall(function() tex:SetTexture(p) end)
        ATPrint("K" .. i .. ". SetTexture(" .. p .. ") -> "
            .. (ok and "|cff00ff00OK|r" or ("|cffff5555ERR: " .. tostring(err) .. "|r")))
    end

    if PlaySoundFile then
        local sp = "Sound\\ATAI\\P" .. t .. ".wav"
        local ok = pcall(PlaySoundFile, sp)
        ATPrint("K4. PlaySoundFile(" .. sp .. ") -> "
            .. (ok and "|cff00ff00OK|r" or "|cffff5555ERR|r"))
    end

    -- K5~K9：用**真实形态**的 payload 验证编码链路能不能扛住。
    -- 前面 K1~K4 只证明了「字符串能落地」，还要确认空格、中文、百分号、
    -- 以及接近 255 字节的长消息都不会被客户端截断或改写。
    -- v1.0.14：这组探针原来都是 **5 段旧格式**（没有 nonce），而桥接端通道 B
    -- 从 v1.0.10 起 require_six —— 也就是它们其实**从来没走到过悬浮窗**，只能靠
    -- 肉眼去翻 MissingFiles.txt。现在统一补上 SESSION，K5~K9 都会真的出现在
    -- 悬浮窗里，一次 /atai diag 就能把编码链路看完。
    -- 长消息里的点号也写成 %2E：否则客户端会在最后一个点号处把它截掉，
    -- 看着像「长消息被截断」，其实是点号的问题。
    local long = string.rep("The quick brown fox jumps over the lazy dog%2E ", 5)
    local realistic = {
        { "K5 空格+标点", MISS_PREFIX .. "%7C" .. SESSION .. "%7C901%7CCHAT_MSG_CHANNEL%7CTestguy"
            .. "%7Cneed healer for the daily heroic, anyone up? 100%25 off" },
        { "K6 中文", MISS_PREFIX .. "%7C" .. SESSION .. "%7C902%7CCHAT_MSG_SAY%7C玩家甲"
            .. "%7C这个Boss门是不是出bug了没开？" },
        { "K7 长消息", MISS_PREFIX .. "%7C" .. SESSION .. "%7C903%7CCHAT_MSG_GUILD%7CLongname%7C" .. long },
        { "K8 混合", MISS_PREFIX .. "%7C" .. SESSION .. "%7C904%7CCHAT_MSG_WHISPER%7C张三"
            .. "%7C|Hitem:12345|h[测试物品]|h 100%25" },
        -- K9：**点号转义的关键验证**。标签段用带点号的频道名，正文里也有句号。
        -- 悬浮窗能显示出 "[2. Newcomers] Testguy: ..." 且正文带句号，就说明
        -- %2E 在真实客户端里活下来了，公共频道不会再被截断。
        { "K9 点号转义", MISS_PREFIX .. "%7C" .. SESSION .. "%7C905%7C2%2E Newcomers%7CTestguy"
            .. "%7CMight%2E It adds defence stuff" },
    }
    for _, r in ipairs(realistic) do
        local ok = emitMissing(r[2])
        ATPrint(r[1] .. " 长度=" .. tostring(#r[2])
            .. " -> " .. (ok and "|cff00ff00OK|r" or "|cffff5555ERR|r"))
    end

    ATPrint("时间戳 " .. t .. " —— 等 5 秒后我去看 Logs\\MissingFiles.txt。")
    ATPrint("里面出现 ATAI2 就说明这条路通了；还要看 K5~K9 有没有被截断。")
    ATPrint("K9 专门看点号：悬浮窗里应显示 |cffffff00[2. Newcomers]|r 且正文带句号。")
end

local function sendWhisper(text, target)
    local me = target or sinkName or UnitName("player")
    if not me or me == "" then
        ATPrint("|cffff5555找不到收信人名字，跳过。|r")
        return
    end
    SendChatMessage(text, "WHISPER", nil, me)
end

local function runDiag()
    copyDefaults()
    local t = stamp()
    local had = filterMode

    ATPrint("|cffffff00========== 自检 v" .. VERSION .. " ==========|r")
    ATPrint("A. " .. ensureChatLogging())

    local cvar, flag = loggingState()
    ATPrint("B. GetCVar(chatLog)=" .. tostring(cvar) .. "  LoggingChat()=" .. tostring(flag))
    ATPrint("C. LoggingChat=" .. type(LoggingChat) .. "  SetCVar=" .. type(SetCVar)
        .. "  过滤API=" .. type(ChatFrame_AddMessageEventFilter))
    ATPrint("D. 队列=" .. tostring(#sendQueue) .. "  收信人=" .. tostring(sinkName or UnitName("player"))
        .. "  过滤模式=" .. filterMode)

    -- E 行用来判断 OnUpdate 到底有没有在跑：它停了，队列和刷盘就全都不会发生。
    ATPrint("E. 已转发=" .. tostring(seq) .. "  刷盘次数=" .. tostring(flushCount)
        .. "  重开失败=" .. tostring(flushFail)
        .. "  被拦=" .. tostring(blockedCount)
        .. (blockedInfo ~= "" and ("（" .. blockedInfo .. "）") or "")
        .. "  上次刷盘=" .. string.format("%.1f", sinceFlush) .. "秒前")

    -- F 行：客户端如果开放了文件读写，就可以彻底扔掉聊天日志这条链路。
    local function g(name)
        local head, tail = string.match(name, "^([^.]+)%.(.+)$")
        if not head then return _G[name] end
        local t2 = _G[head]
        if type(t2) ~= "table" then return nil end
        return t2[tail]
    end
    ATPrint("F. io=" .. type(g("io")) .. "  io.open=" .. type(g("io.open"))
        .. "  io.write=" .. type(g("io.write"))
        .. "  os.execute=" .. type(g("os.execute"))
        .. "  dofile=" .. type(g("dofile")))

    -- G 行：客户端有没有把 Blizzard Internal 的文件 API 开放给插件。
    -- 这是 v1.0.6 的重点 —— 一旦非 nil，聊天日志链路就可以退休了。
    ATPrint("G. AppendToFile=" .. type(_G.AppendToFile)
        .. "  ReadFile=" .. type(_G.ReadFile)
        .. "  DeleteFile=" .. type(_G.DeleteFile)
        .. "  debugdump=" .. type(_G.debugdump)
        .. "  debugprint=" .. type(_G.debugprint)
        .. "  ConsoleExec=" .. type(_G.ConsoleExec)
        .. "  Screenshot=" .. type(_G.Screenshot))
    ATPrint("   （想看细节就输入 |cffffff00/atai filetest|r，它会真的去写一次文件）")

    -- T1：不带标记的普通自密语（任何模式都不拦）
    setFilterMode("off")
    sendWhisper("ATAI-T1-PLAIN-WHISPER-" .. t)
    -- T2：带标记的自密语，完全不过滤
    sendWhisper(PREFIX .. "%7Cdiag%7CT2-NOFILTER%7C" .. t)

    -- T3：只过滤服务器回显（本插件默认模式）
    setFilterMode("whisper")
    sendWhisper(PREFIX .. "%7Cdiag%7CT3-WHISPER-ONLY%7C" .. t)

    -- T4：两个都过滤（v1.0.0 的坏模式）
    setFilterMode("all")
    sendWhisper(PREFIX .. "%7Cdiag%7CT4-FILTER-ALL%7C" .. t)

    -- T5：插件直接往聊天框写（可见）
    DEFAULT_CHAT_FRAME:AddMessage("ATAI-T5-ADDONMSG-" .. t)
    -- T6：插件往隐藏框写（不可见）
    local sink = getHiddenSink()
    if sink then
        sink:AddMessage("ATAI-T6-HIDDENFRAME-" .. t)
    end

    setFilterMode(had)

    -- v1.1（V11-05）：**不再**在这里强制刷盘。实测早就证明「关掉再打开」
    -- 不会让客户端把缓冲区写下来（见文件头 h) 条），而这个动作有真实副作用：
    -- 日志关闭的那 1 秒客户端不记录任何聊天，通道 A 被戳出空洞。诊断命令
    -- 绝不该为了一个无效动作去破坏用户的数据完整性。
    -- 想手动试一次刷盘：/atai flush（或跑 /atai flushtest，它会显式进入调试
    -- 模式并在结束时恢复原状态）。

    ATPrint("|cff00ff00已发出 6 条测试消息|r，时间戳 " .. t)
    ATPrint("T1~T4 已发往通道 A（聊天日志）。|cffffff00通道 A 不是实时的|r —— "
        .. "客户端只在缓冲区满（约 96KB）或退出游戏时才写盘，")
    ATPrint("所以这些探针可能要等很久才出现在 WoWChatLog.txt 里，这是正常的。")
    ATPrint("想立刻验证链路请用 |cffffff00/atai test|r（走通道 B，秒级）。")
    ATPrint("  T1 / T2 / T3 / T4 出现在日志里 = 过滤器确实不挡日志")
    ATPrint("  T5-ADDONMSG 会显示在聊天框里，T6-HIDDENFRAME 不会显示")
end

-- ------------------------------------------------------------ 环境探测
-- 客户端到底给插件开放了哪些能力？直接问它。
local function resolveGlobal(name)
    local dot = string.find(name, ".", 1, true)
    if not dot then return _G[name] end
    local head = string.sub(name, 1, dot - 1)
    local tail = string.sub(name, dot + 1)
    local t = _G[head]
    if type(t) ~= "table" then return nil end
    return t[tail]
end

local PROBES = {
    "io", "io.open", "io.write", "io.lines",
    "os", "os.execute", "os.remove", "os.rename", "os.time", "os.date",
    "dofile", "loadfile", "loadstring", "load", "require", "debug",
    "setfenv", "getfenv", "newproxy",
    "socket", "http", "curl", "ConsoleExec", "Screenshot",
    "LoggingChat", "LoggingCombat", "SetCVar", "GetCVar",
    "ChatFrame_AddMessageEventFilter", "SendAddonMessage",
    "AppendToFile", "ReadFile", "DeleteFile",
    "debugdump", "debugprint", "debuginfo", "debugload",
}

local function runEnv()
    ATPrint("|cffffff00=== 环境探测 v" .. VERSION .. " ===|r")
    for _, n in ipairs(PROBES) do
        local ok, v = pcall(resolveGlobal, n)
        ATPrint("  " .. n .. " = " .. (ok and type(v) or "ERR"))
    end
    ATPrint("  表=table 函数=function 不存在=nil")
end

local function runGlobals(filter)
    filter = string.lower(filter or "")
    local hits = {}
    pcall(function()
        for k, v in pairs(_G) do
            local ks = tostring(k)
            if filter == "" or string.find(string.lower(ks), filter, 1, true) then
                hits[#hits+1] = ks .. " (" .. type(v) .. ")"
            end
        end
    end)
    table.sort(hits)
    ATPrint("全局名含 \"" .. filter .. "\" 共 " .. tostring(#hits) .. " 条：")
    local shown = 0
    for _, h in ipairs(hits) do
        if shown >= 40 then
            ATPrint("  ...（还有更多，换个关键字）")
            break
        end
        ATPrint("  " .. h)
        shown = shown + 1
    end
end

-- -------------------------------------------------------------- 斜杠命令
SLASH_ARDENTRANSLATORAI1 = "/atai"
SlashCmdList["ARDENTRANSLATORAI"] = function(input)
    copyDefaults()
    local cmd, arg = string.match(string.lower(input or ""), "^%s*(%S*)%s*(%S*)")
    cmd = cmd or ""
    arg = arg or ""

    if cmd == "" or cmd == "help" then
        ATPrint("命令: /atai on|off|status|diag|test|flush")
        ATPrint("/atai test                   发一条测试消息，验证整条链路")
        ATPrint("/atai filetest               实测 AppendToFile/ReadFile 能不能用")
        ATPrint("/atai logtest                实测 debugprint 是不是实时写 Logs\\LUA.txt")
        ATPrint("/atai misstest               实测能否借 MissingFiles.txt 当实时通道（重点）")
        ATPrint("/atai chan chatlog|miss|both 选择传输通道（默认 both）")
        ATPrint("/atai flushtest yes          实测 LoggingChat(false) 是否生效（调试命令，会改日志开关）")
        ATPrint("/atai env                    探测客户端开放了哪些能力")
        ATPrint("/atai globals <关键字>       列出名字含关键字的全局")
        ATPrint("/atai autoflush on|off       发完消息后是否强制刷盘（默认 OFF，实测无效）")
        ATPrint("/atai hold <秒>              刷盘时日志关闭的时长（默认 1.0）")
        ATPrint("/atai guild|party|raid|whisper|say|yell|bg|channel|officer on|off")
        ATPrint("/atai logging on|off")
        ATPrint("/atai debug on|off            实时打印转发过程")
        ATPrint("/atai filter off|whisper|all  插件自己那条标记密语的隐藏方式")
        ATPrint("/atai sink <名字>             把标记密语发给别人（默认发给自己）")
        return
    end

    if cmd == "on" then
        ArdenTranslatorAIDB.enabled = true
        ensureChatLogging()
        ATPrint("总开关: |cff00ff00ON|r")
        return
    elseif cmd == "off" then
        ArdenTranslatorAIDB.enabled = false
        ATPrint("总开关: |cffff5555OFF|r")
        return
    elseif cmd == "status" then
        local function s(k) return ArdenTranslatorAIDB[k] and "|cff00ff00ON|r" or "|cffff5555OFF|r" end
        local cvar, flag = loggingState()
        ATPrint("总开关:" .. s("enabled") ..
            " 公会:" .. s("guild") ..
            " 小队:" .. s("party") ..
            " 团队:" .. s("raid") ..
            " 密语:" .. s("whisper"))
        ATPrint("说话:" .. s("say") ..
            " 喊话:" .. s("yell") ..
            " 战场:" .. s("battleground") ..
            " 公共频道:" .. s("channel") ..
            " 官员:" .. s("officer"))
        -- 「插件打算开日志」和「客户端实际开着日志」是两回事：前者读的是我们自己的
        -- SavedVariables 标记，后者是 LoggingChat() 的实时读值。两者不一致时必须
        -- 明说，否则用户看到「ChatLog:ON」会以为一切正常，实际上日志是关的。
        -- 这个客户端用 1 / nil 表示开关（不是 true/false），nil 就是「关」。
        local wantOn = ArdenTranslatorAIDB.logging and true or false
        local liveOn = (flag ~= nil and flag ~= false)
        ATPrint("ChatLog: 插件想要=" .. (wantOn and "|cff00ff00ON|r" or "|cffff5555OFF|r")
            .. "  客户端实际=" .. (liveOn and "|cff00ff00ON|r" or "|cffff5555OFF|r")
            .. " (LoggingChat()=" .. tostring(flag) .. ")"
            .. (wantOn ~= liveOn and "  |cffff5555← 不一致|r" or "")
            .. "  CVar=" .. tostring(cvar)
            .. "  队列:" .. tostring(#sendQueue))
        ATPrint("v" .. VERSION ..
            " 协议=ATAI" .. tostring(PROTOCOL + 0) ..
            " 过滤=" .. filterMode ..
            " 通道=" .. chanMode ..
            " 自动刷盘=" .. (autoFlush and "ON" or "OFF") ..
            " 收信人=" .. tostring(sinkName or UnitName("player")))
        ATPrint("会话=" .. SESSION .. "（纯数字，含 reload 计数防碰撞）")
        ATPrint("已转发=" .. tostring(seq) ..
            " MissingFiles=" .. tostring(missSent) ..
            (missFail > 0 and ("（失败 " .. missFail .. "）") or "") ..
            " 刷盘次数=" .. tostring(flushCount) ..
            " 重开失败=" .. tostring(flushFail) ..
            " 被拦=" .. tostring(blockedCount) ..
            " 上次刷盘=" .. string.format("%.1f", sinceFlush) .. "秒前")
        ATPrint("当前状态: " .. (reopenTimer > 0
                and ("|cffffff00刷盘窗口内（日志临时关闭 " .. string.format("%.1f", reopenTimer) .. " 秒）|r")
                or "空闲")
            .. "  排队=" .. tostring(#sendQueue))
        ATPrint("|cff888888桥接端底部状态栏：实时=通道B(MissingFiles) 日志=通道A(聊天日志)。|r")
        ATPrint("|cff888888通道 B 在本客户端实测是秒级**实时**，但它依赖客户端的"
            .. "缺失资源日志行为，别的私服不一定支持 —— 以桥接端实际收到的计数为准。|r")
        ATPrint("|cff888888通道 A **不是实时的**：要等客户端缓冲区满（约 96KB）或退出"
            .. "游戏才刷盘，所以「日志 0」长期为 0 是正常的，只要「实时」在涨就说明链路通。|r")
        return
    elseif cmd == "diag" then
        runDiag()
        return
    elseif cmd == "env" then
        runEnv()
        return
    elseif cmd == "filetest" then
        testFileTransport()
        return
    elseif cmd == "logtest" then
        testLogChannel()
        return
    elseif cmd == "chan" then
        if arg == "chatlog" or arg == "miss" or arg == "both" then
            chanMode = arg
            local desc = {
                chatlog = "只走聊天日志（实时性差，要等客户端退出才刷盘）",
                miss = "只走 MissingFiles（实时，推荐）",
                both = "两条都走（默认，互为备份）",
            }
            ATPrint("传输通道: |cff00ff00" .. arg .. "|r  " .. desc[arg])
            if arg ~= "chatlog" then
                if ensureMissTexture() then
                    ATPrint("MissingFiles 通道就绪（已创建隐藏贴图）。")
                else
                    ATPrint("|cffff5555CreateTexture 失败，MissingFiles 通道不可用。|r")
                end
            end
        else
            ATPrint("当前通道: " .. chanMode
                .. "  （chatlog / miss / both）")
            ATPrint("用法: /atai chan chatlog|miss|both")
        end
        return
    elseif cmd == "misstest" then
        testMissingFileChannel()
        return
    elseif cmd == "flushtest" then
        -- v1.1（V11-05）：这是**调试命令**，会真的去动 LoggingChat，所以
        --   1) 必须显式确认（不带 "yes" 就先解释清楚、不执行）；
        --   2) 结束时**恢复原来的状态**，不再顺手把 autoFlush 打开（旧版会）。
        -- 只问一个问题：LoggingChat(false) 到底有没有生效？
        -- 之前只验证过「设 true 后读回 true」—— 那说明不了任何事，因为状态
        -- 本来就是 true。这次反过来：设 false，下一帧读回。
        --   读回 false -> setter 生效，问题在客户端「关了也不刷盘」
        --   读回 true  -> setter 被忽略（函数被保护/拦截），得换路子
        if arg ~= "yes" then
            ATPrint("|cffffff00=== 刷盘生效性探测（调试命令）===|r")
            ATPrint("这个命令会临时调用 LoggingChat(false)，然后 0.5 秒后恢复。")
            ATPrint("副作用：关闭的那一小段时间里，客户端不记录聊天，通道 A 会有空洞。")
            ATPrint("确认要跑请输入： |cffffff00/atai flushtest yes|r")
            return
        end
        ATPrint("|cffffff00=== 刷盘生效性探测 v" .. VERSION .. " ===|r")
        ATPrint("J0. LoggingChat()=" .. tostring(select(2, loggingState()))
            .. "  被拦次数=" .. tostring(blockedCount)
            .. (blockedInfo ~= "" and ("  最后一次: " .. blockedInfo) or ""))
        -- 记住原状态，结束时恢复（不再写死 autoFlush=true）。
        probeAutoFlushBackup = autoFlush
        autoFlush = false
        local ok, ret = pcall(LoggingChat, false)
        ATPrint("J1. 已调用 LoggingChat(false) -> ok=" .. tostring(ok)
            .. " ret=" .. tostring(ret))
        ATPrint("    0.5 秒后读回结果，请看下一行（J2）。结束后会恢复原状。")
        probeStage = 1
        probeTimer = 0
        return
    elseif cmd == "globals" then
        runGlobals(arg)
        return
    elseif cmd == "flush" then
        if beginFlush() then
            ATPrint("已触发刷盘：关闭聊天日志 " .. tostring(CLOSE_HOLD) .. " 秒后重开。")
            ATPrint("这一步必须分两帧做，同帧内「关了立刻开」是无效的。")
        else
            ATPrint("|cffff5555刷盘失败：LoggingChat 不可用、日志开关是关的，或已经在关闭中。|r")
        end
        return
    elseif cmd == "autoflush" then
        autoFlush = (arg ~= "off")
        ATPrint("自动刷盘: " .. (autoFlush and "|cff00ff00ON|r" or "|cffff5555OFF|r"))
        return
    elseif cmd == "hold" then
        -- 调「日志保持关闭」的时长：客户端的落盘是延迟的，这个窗口太短
        -- 就会来不及写盘。不用 /reload 就能试不同值。
        local n = tonumber(arg)
        if n and n >= 0.05 and n <= 10 then
            CLOSE_HOLD = n
            ATPrint("刷盘关闭窗口改为 " .. tostring(CLOSE_HOLD) .. " 秒。")
            ATPrint("试试 /atai test，然后看桥接端「标记」有没有 +1。")
        else
            ATPrint("当前关闭窗口 " .. tostring(CLOSE_HOLD) .. " 秒。")
            ATPrint("用法: /atai hold 1.5   （0.05 ~ 10 秒，默认 1.0）")
        end
        return
    elseif cmd == "test" then
        local t = stamp()
        -- 三条探针的文本**必须各不相同、而且都是纯英文**，原因有两条：
        --   1. 文本相同的话，桥接端会翻出两条一模一样的中文，看起来像重复 bug
        --      —— 实际上只是同一条内容走了两条不同的路，但没人能分辨。
        --   2. 混中文的探针（旧版的 "ArdenTranslator 探测 <时间>"）会被桥接端的
        --      语言判定当成英文：拉丁字母 15 个、汉字只有 2 个，汉字占比不够，
        --      于是照样送去翻译 —— 又和另一条撞成「重复」。
        -- 每条探针都自带路径标签，悬浮窗里一眼就能看出是哪条路通了。
        --
        -- v1.0.14：三条探针**都带上点号**，专门验证「点号截断」修复真的生效。
        -- probe2 连标签段都用带点号的频道名 "2. Newcomers"，精确复现公共频道那条
        -- 曾经被截断的路径。只要悬浮窗能把 probe2 / probe3 完整翻出来（而不是只
        -- 剩 "[2]" 或干脆没有），就说明 %2E 在真实客户端里活下来了。
        sendWhisper(PREFIX .. "%7C" .. SESSION .. "%7C9001%7CCHAT_MSG_WHISPER%7CATAITEST%7CATAI probe1 chatlog-only 3.3.5 " .. t)
        queueForBridge("CHAT_MSG_CHANNEL", "ATAI probe2 queue-both dot.test " .. t, "ATAITEST", "2. Newcomers")
        emitMissing(MISS_PREFIX .. "%7C" .. SESSION .. "%7C9999%7CCHAT_MSG_WHISPER%7CATAITEST%7C"
            .. esc("ATAI probe3 missing.file 100% done " .. t))
        -- v1.1（V11-05）：**不再**偷偷调 beginFlush()。实测证明它刷不出盘，
        -- 却会让通道 A 出现空洞。这个命令是「验证链路」用的，不是「强制落盘」
        -- 用的；要动日志开关请走 /atai flushtest（有确认 + 会恢复）。
        ATPrint("已发出 3 条探针（v" .. VERSION .. "，都带点号）：")
        ATPrint("  |cff00ff00probe2 queue-both|r   通道 A + B 都发，标签=2. Newcomers（应立刻出现）")
        ATPrint("  |cff00ff00probe3 missingfile|r  只发通道 B（应立刻出现）")
        ATPrint("  |cffffff00probe1 chatlog-only|r 只发通道 A（要等客户端自己刷盘，"
            .. "通常不会立刻出现 —— 这是正常的）")
        ATPrint("  MissingFiles 累计=" .. tostring(missSent)
            .. "（通道=" .. chanMode .. "）"
            .. "  LoggingChat()=" .. tostring(select(2, loggingState()))
            .. "  刷盘次数=" .. tostring(flushCount))
        ATPrint("悬浮窗 2 秒内应出现 |cffffff00probe2|r 与 |cffffff00probe3|r 两条。")
        ATPrint("|cff888888只要底部「实时」在涨，链路就是通的 —— 「日志」长期是 0 属于正常。|r")
        return
    elseif cmd == "debug" then
        if arg == "on" then
            debugForwarding = true
            ATPrint("调试: |cff00ff00ON|r（每条被转发的消息都会打印出来）")
        else
            debugForwarding = false
            ATPrint("调试: |cffff5555OFF|r")
        end
        return
    elseif cmd == "filter" then
        if arg == "" then
            ATPrint("当前过滤模式: " .. filterMode
                .. "（只作用于插件自己发的 @ATAI1@ 标记密语，不影响别人说的话）")
            return
        end
        if setFilterMode(arg) then
            -- 注意措辞：这三个模式**只**管插件自己那条标记密语。
            -- 之前把这里描述成「藏英文原文」，害用户以为能藏别人的话，
            -- 所以现在每一条都写明作用对象。
            local desc = {
                off = "完全不过滤（聊天框能看到插件自己的 @ATAI1@ 标记密语，排错用）",
                whisper = "只藏标记密语的服务器回显",
                all = "收发两边的标记密语都藏（推荐）",
            }
            ATPrint("过滤模式: |cff00ff00" .. arg .. "|r  " .. (desc[arg] or ""))
        else
            ATPrint("用法: /atai filter off|whisper|all")
        end
        return
    elseif cmd == "sink" then
        local raw = string.match(input or "", "^%s*%S+%s+(.-)%s*$")
        if raw and raw ~= "" then
            sinkName = raw
            ATPrint("收信人改为: " .. sinkName)
        else
            sinkName = nil
            ATPrint("收信人改回自己")
        end
        return
    end

    local aliases = {
        guild="guild", party="party", raid="raid", whisper="whisper",
        say="say", yell="yell", bg="battleground", battleground="battleground",
        channel="channel", officer="officer", logging="logging"
    }
    local key = aliases[cmd]
    if key and (arg == "on" or arg == "off") then
        ArdenTranslatorAIDB[key] = (arg == "on")
        if key == "logging" and arg == "on" then ensureChatLogging() end
        ATPrint(cmd .. ": " .. (ArdenTranslatorAIDB[key] and "|cff00ff00ON|r" or "|cffff5555OFF|r"))
        return
    end

    ATPrint("未知命令。输入 /atai help")
end
