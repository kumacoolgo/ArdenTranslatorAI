# ArdenTranslator AI Windows Bridge -- wire protocol (v1.1)
#
# 一次解析、一次解码。这是 V11-01 的全部要点。
#
# 背景（v1.0.17 及以前的 bug）
# ---------------------------------------------------------------------------
# 插件把整条 payload 的**竖线分隔符也编码成 `%7C`**，因为 WoW 聊天里裸 `|`
# 是转义起始符（`|c` 颜色 / `|T` 贴图 / `|H` 链接），会被客户端吃掉。所以线
# 上形态长这样：
#
#     ATAI2%7C<session>%7C<seq>%7C<label>%7C<sender>%7C<text>
#     @ATAI1@%7C<session>%7C<seq>%7C<label>%7C<sender>%7C<text>
#
# 旧实现在 `dispatch_payload()` 里**先对整个 payload 调 unesc()**，再 split，
# 然后对 event/sender/msg **又调一次 unesc()**。两个后果：
#
#   1. 二次解码。插件把原文里的字面 `%2E` 编码成 `%252E`（`%` -> `%25`），
#      解一次刚好还原成 `%2E`；但解两次就变成 `.` —— 用户的原文被改写。
#      实测（见 tests/test_protocol.py）：
#         wire "The code is %252E" --1x--> "The code is %2E"  正确
#                                  --2x--> "The code is ."     错误
#
#   2. 正文里的字面 `|`（插件编码成 `%7C`）在「先整体解码」之后变回裸 `|`，
#      于是参与 split，把一条消息切错位。`foo%7Cbar` 这种最普通的输入就会中招。
#
# 正确做法：**在未解码的串上定位前 N 个 `%7C`**，切开之后每个字段只 `unesc()`
# 一次，正文段原样保留（它内部可能还有 `%7C`，那是用户自己打的竖线）。
#
# 为什么 `%257C` 不会被误当分隔符
# ---------------------------------------------------------------------------
# 我们在**未解码**的串上找分隔符，找的是恰好 3 个字符的 `%7C`。而 `%257C` 是
# 4 个字符（`%` `2` `5` `7` `C` 里的 `257C` 前缀不同）—— 精确地说，`%257C`
# 的字符序列是 `%`,`2`,`5`,`7`,`C`，其中**不包含** `%7C` 这个三字符子串
# （位置上是 `%25` 和 `7C`，没有 `%7C` 连续出现）。所以它天然安全，不需要特判。
# 这就是「不做通用 URL 解码」的价值：一旦用 urllib.parse.unquote 语义，
# `%257C` 就会被一层层剥开，分隔符和正文就分不清了。

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# 线上常量
# ---------------------------------------------------------------------------

# 通道 A（聊天日志，自密语）。历史格式是 5 段（没有 session），v1.0.9 起是 6 段。
MARKER_CHATLOG = "@ATAI1@"
# 通道 B（MissingFiles，实时主通道）。只有 6 段格式。
MARKER_MISS = "ATAI2"

# 分隔符的**编码形态**。插件端连分隔符都是 %7C，所以我们也只在编码形态上找。
_SEP = "%7C"
_SEP_LOWER = "%7c"

# 字段数量。
FIELDS_V6 = 6          # marker, session, seq, label, sender, text
FIELDS_V5 = 5          # marker, seq, label, sender, text  (历史格式)

# 上限。都是「防御性」的，正常一条聊天远小于这些值。
MAX_WIRE_LEN = 8192          # 整条线上 payload 的字符数上限
MAX_FIELD_LEN = 4096         # 单个字段上限
MAX_SESSION_LEN = 24         # session 是纯数字，给足余量
MAX_SEQ_LEN = 12             # seq 是纯数字自增
# 正文长度上限。WoW 聊天框单条消息本身有 255 字节限制，但插件编码后（%XX 变长）
# 会膨胀；给 2048 个字符足够覆盖任何真实聊天，同时挡住 garbage。
MAX_TEXT_LEN = 2048

# session / seq 必须是纯数字 —— 桥接端靠这个区分 6 段与 5 段（见 parse_payload）。
_DIGITS = re.compile(r"^[0-9]+$")

# 客户端给 SetTexture 的贴图名自动补的扩展名。通道 B 每行结尾都有。
_MISS_SUFFIX = re.compile(r"\.(?:blp|tga)$", re.IGNORECASE)


def unesc(s: str) -> str:
    """还原插件 esc() 做的转义。

    顺序要紧：**%25 必须最后还原**。否则用户原文里字面的 "%2E" 会被
    （插件转义成 "%252E"）先被 %2E 规则吃掉一半，再被 %25 还原成 "%2E" ——
    结果错成 "."。反过来先 %2E 后 %25 就不会误伤（"%252E" 里根本没有
    "%2E" 这个子串）。

    注意 `%257C` 还原成 `%7C`：那是用户原文里**真的**打了一个字面 "%7C"
    这四个字符，插件把 `%` 转成 `%25` 得到 `%257C`。解一次刚好还原。
    """
    return (s.replace("%7C", "|").replace("%7c", "|")
             .replace("%2E", ".").replace("%2e", ".")
             .replace("%25", "%"))


@dataclass(frozen=True)
class ParsedMessage:
    """一条通过校验的聊天消息。字段全部是**已解码**的原文。"""

    protocol: str          # "ATAI2" 或 "@ATAI1@"
    source: str            # "miss" 或 "chatlog"
    session: str           # 会话 ID（纯数字字符串）；历史 5 段为 ""
    seq: str               # 会话内单调自增序号（纯数字字符串）
    label: str             # 第 4 段：公共频道名（如 "2. Newcomers"）或 CHAT_MSG_*
    sender: str
    text: str
    version: int           # 6 或 5
    raw_len: int           # 线上 payload 长度（用于长消息统计）

    @property
    def dedupe_key(self):
        """桥接端去重主键。

        6 段格式用 (protocol, session, seq) —— 插件保证同一会话内 seq 单调递增，
        所以这个三元组全局唯一，且**不会**把「不同玩家说了同一句话」误判成重复。

        历史 5 段没有 session，只能退回到内容哈希。那种格式只在老版本插件的
        聊天日志里出现，冲突概率低得多，可以接受。
        """
        if self.version == 6 and self.session and self.seq:
            return (self.protocol, self.session, self.seq)
        return ("legacy", self.label, self.sender, self.text)

    @property
    def is_channel(self) -> bool:
        """第 4 段是公共频道名，而不是 CHAT_MSG_* 事件名。"""
        return bool(self.label) and not self.label.startswith("CHAT_MSG_")


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _next_sep(raw: str, start: int) -> int:
    """下一个**编码分隔符**的位置（大小写都认），没有就返回 -1。

    必须是精确三字符串 `%7C` / `%7c`，不做任何通用 URL 解码。
    """
    i = raw.find(_SEP, start)
    j = raw.find(_SEP_LOWER, start)
    if i == -1:
        return j
    if j == -1:
        return i
    return min(i, j)


def _split_encoded(body: str, n_body_fields: int):
    """把**已去掉 marker** 的 body 切成 n_body_fields 段。

    线上形态是 `MARKER%7C<f1>%7C<f2>...%7C<fn>` —— body 以分隔符开头，后面
    跟着 n_body_fields 个字段，字段之间共 n_body_fields-1 个分隔符。所以总共
    有 n_body_fields 个分隔符（含开头那个），切完丢掉开头的空串。

    用「找前 n_body_fields-1 个内部分隔符，剩下的整体当最后一段」，而不是
    str.split：最后一段（正文）里可能还有编码过的竖线，那些是用户原文，
    不是分隔符。

    返回 list[str]（长度恰为 n_body_fields），或者 None（分隔符不够）。
    """
    if not body.startswith(_SEP) and not body.startswith(_SEP_LOWER):
        return None
    parts = []
    start = len(_SEP)          # 跳过紧跟 marker 的那个分隔符
    for _ in range(n_body_fields - 1):
        k = _next_sep(body, start)
        if k == -1:
            return None
        parts.append(body[start:k])
        start = k + len(_SEP)
    parts.append(body[start:])
    return parts


def parse_payload(raw: str, *, source: str) -> Optional[ParsedMessage]:
    """解析一条线上 payload。失败返回 None（调用方负责计数，不要抛错）。

    `source` 取值 `"miss"`（通道 B，MissingFiles）或 `"chatlog"`（通道 A）。
    这里只做校验与解码，绝不抛异常 —— 日志 tail 线程靠这个保证「坏行不杀线程」。
    """
    if not isinstance(raw, str):
        return None
    raw = raw.strip("\r\n").strip()
    if not raw or len(raw) > MAX_WIRE_LEN:
        return None
    if source not in ("miss", "chatlog"):
        return None

    # ---- 1. 前缀判定（在**未解码**的串上做，前缀本身不含 %7C）----
    #
    # 前缀与 source 必须匹配：通道 B 只会出现 ATAI2，通道 A 只会出现 @ATAI1@。
    # 一条 @ATAI1@ 出现在 MissingFiles 里说明解析链路搞错了（或者有人手工往
    # 那个文件里粘了东西），宁可拒收也不要按 A 的宽松规则（兼容 5 段）去解析，
    # 那样会把格式不对的脏数据放进管道。
    if raw.startswith(MARKER_MISS):
        protocol = MARKER_MISS
        body = raw[len(MARKER_MISS):]
        # 通道 B 只认 6 段。格式对不上直接丢，绝不退回 5 段错位解析 ——
        # 错位解析不报错，但会把 seq 当事件名、事件名当发送者，整条消息全错，
        # 比丢掉更难排查。
        require_v6 = True
    elif raw.startswith(MARKER_CHATLOG):
        protocol = MARKER_CHATLOG
        body = raw[len(MARKER_CHATLOG):]
        # 通道 A 兼容历史 5 段（老插件版本的聊天日志）。
        require_v6 = False
        if source == "miss":
            return None
    else:
        return None

    # 前缀后面必须紧跟分隔符。marker 出现在正文中间时（比如有人把 @ATAI1@
    # 打进了聊天内容），这里就切不出正确段数，会被下面的校验挡掉。
    if not (body.startswith(_SEP) or body.startswith(_SEP_LOWER)):
        return None

    # ---- 2. 先按 6 段切（marker 之外还有 5 个字段）----
    parts6 = _split_encoded(body, FIELDS_V6 - 1)
    if parts6 is not None:
        session, seq, label, sender, text = parts6
        # 6 段的判据：session 与 seq 都是纯数字。
        # 历史 5 段格式的第 2 段是 seq（数字）、第 3 段是事件名（CHAT_MSG_*，
        # 永远不是数字）—— 所以旧格式误切时 parts6[2] 必然不是数字，会自动
        # 落回 5 段分支。
        if (_DIGITS.match(session) and _DIGITS.match(seq)
                and len(session) <= MAX_SESSION_LEN and len(seq) <= MAX_SEQ_LEN):
            return _finish(protocol, source, session, seq, label, sender, text,
                           raw, version=6)

    if require_v6:
        return None

    # ---- 3. 通道 A 的历史 5 段（marker 之外还有 4 个字段）----
    parts5 = _split_encoded(body, FIELDS_V5 - 1)
    if parts5 is None:
        return None
    seq, label, sender, text = parts5
    if not _DIGITS.match(seq) or len(seq) > MAX_SEQ_LEN:
        return None
    return _finish(protocol, source, "", seq, label, sender, text, raw, version=5)


def _finish(protocol, source, session, seq, label, sender, text, raw, *, version):
    """字段解码 + 范围校验。任何一项不过就返回 None。"""
    if (len(label) > MAX_FIELD_LEN or len(sender) > MAX_FIELD_LEN
            or len(text) > MAX_FIELD_LEN):
        return None

    # 每个字段**只解一次**。顺序无关（各字段互不影响），但必须一次。
    label = unesc(label)
    sender = unesc(sender)
    text = unesc(text)

    if len(text) > MAX_TEXT_LEN or len(sender) > MAX_FIELD_LEN:
        return None
    # label 为空是致命的（连频道都判断不了），sender 为空允许 ——
    # 某些系统消息没有作者，悬浮窗会显示成空。
    if not label:
        return None

    return ParsedMessage(
        protocol=protocol,
        source=source,
        session=session,
        seq=seq,
        label=label,
        sender=sender,
        text=text,
        version=version,
        raw_len=len(raw),
    )


def strip_miss_suffix(line: str) -> str:
    """通道 B 的一行去掉客户端自动补的 .blp / .tga。

    客户端把贴图基名补成两个文件名各记一行，所以同一条 payload 会来两次。
    """
    return _MISS_SUFFIX.sub("", line.strip())
