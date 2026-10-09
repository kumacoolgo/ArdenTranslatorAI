# ArdenTranslator AI v1

这是一个针对 WoW 3.3.5 风格客户端（ArdenWoW、Ascension 等私服都行）的 AI 聊天翻译方案：
游戏里别人说的话，自动变成中文显示在 Windows 的置顶悬浮窗里。

> 不绑定任何服务器 —— 插件靠客户端行为判定，换服只需重选一次客户端目录、
> 按需改一下 `protected_terms`（见下文「WoW 专有名词保护」）。

> **换新机器部署 → 看 [`新机器部署指南.md`](新机器部署指南.md)。**
> 那份文档是给「从零装一台新电脑」用的，含全部变量清单和排错表。

## 结构

- `ArdenTranslatorAI/`：WoW 插件，负责频道过滤、开关和把允许的聊天行送出去。
  **它会被打包进 EXE**，用户不需要手工复制。
- `WindowsBridge/ArdenTranslatorAI.py`：Windows 桥接 —— 管理界面 + 悬浮翻译窗。
- `WindowsBridge/dist/`：打包好的 EXE（见下文「打包」）。
- API Key **只**存在 Windows 用户环境变量 `ZHIPU_API_KEY` 里（管理界面填的 Key
  也是写进环境变量）。`config.json` 里不放明文。**不放进 WoW 插件。**

## 双击 EXE 之后发生什么（v1.0.15）

```
双击 ArdenTranslatorAI.exe
        ↓
管理界面：客户端目录 / 模型 / ZHIPU_API_KEY / 悬浮窗长宽字号
        ↓ 点「开始翻译」
删除 <客户端>\Interface\AddOns\ArdenTranslatorAI，用 EXE 内置的那份重建
        ↓
保存配置 → 关掉管理窗 → 直接弹出翻译悬浮窗
        ↓
启动游戏（插件已经是启用的），开聊就行
```

**插件永远是最新的**：每次点「开始」都会删旧重建，不存在「EXE 更新了但游戏里
还是旧插件」这种最难查的错配。

## 为什么需要 Windows Bridge

WoW Lua 插件没有普通网络 socket / HTTP 权限，也不能任意读写本地文件。
所以桥接程序走**两条**由客户端代写的通道 —— 插件负责「把数据塞进客户端会写的地方」，
EXE 负责「读出来 + 调 AI + 显示」。

### 通道 B：`Logs\MissingFiles.txt`（实时，主力）

插件把消息伪装成一个贴图路径丢给 `SetTexture()`。客户端加载失败时会把该路径
**原样、实时**写进 `Logs\MissingFiles.txt`（游戏运行中即写盘，延迟**秒级**）。

这是目前的主力通道。

### 通道 A：`Logs\WoWChatLog.txt`（兜底）

插件私聊自己一条带 `@ATAI1@` 标记的消息，插件再把它从聊天窗口隐藏，
客户端把它写进聊天日志。**但实测这个客户端只在缓冲区满（约 96KB）或客户端退出时
才刷盘** —— 延迟可能几十分钟，因此只作为备份存在。

> 两条通道对同一条消息使用**相同的会话串和序号**，所以同一批消息即使从两条通道
> 都到达，也只会翻译一次，不会重复。
> 用 `/atai chan chatlog|miss|both` 切换，默认 `both`（互为备份）。

注意：通道 A 会产生「发给自己」的隐藏私聊流量。插件做了 0.18 秒节流和 60 条队列
上限，避免短时间刷爆服务器。

## 安装插件

**正常情况不用管这一节** —— 点管理界面的「开始翻译」会自动装好。

手工安装（或想确认装没装）时，`Interface\AddOns\ArdenTranslatorAI\` 里应该是：

```
ArdenTranslatorAI.lua
ArdenTranslatorAI.toc
```

注意别多套一层文件夹：`AddOns\ArdenTranslatorAI\ArdenTranslatorAI.toc` 才是对的。
客户端根目录就是含 `Interface` 和 `Logs` 的那一层。

进游戏：

- `/reload`
- `/atai status`
- `/atai on`

常用命令：

- `/atai on` / `/atai off` —— 总开关
- `/atai status` —— 看状态
- `/atai test` —— 发 3 条带标签的探针，一眼看出哪条路通
- `/atai diag` —— 详细诊断（转发数、刷盘次数、通道计数）
- `/atai chan chatlog|miss|both` —— 选择传输通道
- `/atai autoflush on|off` —— 是否自动「关一下日志」逼客户端刷盘（**默认 OFF**，见下）
- `/atai filter off|whisper|all` —— 标记密语的隐藏方式
- `/atai guild on|off`、`/atai party on|off`、`/atai raid on|off`、
  `/atai whisper on|off`、`/atai channel on|off`、`/atai say on|off`、
  `/atai yell on|off`、`/atai bg on|off`、`/atai officer on|off`
- `/atai logging on|off`
- `/atai help` —— 全部命令

### 怎么读悬浮窗底部那行状态

**v1.1 起格式变了**（旧格式见本节末尾）。现在由**通道健康状态机**驱动，
目的是把几种「看起来都是没动静、其实完全不同」的故障分开：

```
B:正常 41 · A:等待数据 0 · 已译 31 · 错 0 · 队列 3/100 · 丢弃 2
```

| 字段 | 含义 |
|---|---|
| `B:` | 通道 B（MissingFiles）的健康状态 + 收到条数 —— **这个才是主力** |
| `A:` | 通道 A（聊天日志）的健康状态 + 收到条数 |
| 已译 | 成功翻译的条数 |
| 错 | 翻译失败条数 |
| 拒 | 协议解析失败的条数（**这个一直在涨 = 插件和 EXE 版本不匹配**） |
| 屏蔽 | 被「⚙ 频道屏蔽」挡下、**没送去翻译**的条数（为 0 时不显示） |
| 队列 | 待翻译队列深度 / 容量（空闲时不显示） |
| 丢弃 | 因队列满或过期被丢掉的条数（为 0 时不显示） |

通道状态取值：

| 显示 | 含义 | 该怎么办 |
|---|---|---|
| `未配置` | 还没配客户端目录 | 在管理界面选目录 |
| `等待数据` | 配好了，但没收到过**有效**消息 | 装了插件没开游戏，或没进有人的频道 |
| `正常` | 最近收到过有效消息 | 一切正常 |
| `45s 无消息` | 曾经正常，但 90 秒没动静了 | 没人说话（正常），或通道断了 |
| `读取/解析异常` | 有 marker 行但一条都解不出来 | **版本不匹配 / 编码坏了**，看末尾提示 |

**四种故障终于能分开**：

* 装了没开游戏 → 两条通道都 `未配置`
* 开了游戏没人说话 → `等待数据` / `45s 无消息`（这**是正常的**）
* 通道真断了 → 曾经 `正常`，现在是 `Ns 无消息`
* 解析一直失败 → `读取/解析异常`，且 `拒` 在涨

**「A: 等待数据 / 长期 0」不用担心**：通道 A 要等客户端自己刷盘（缓冲区满约 96KB，
或客户端退出），天然滞后几十分钟甚至一整个会话。**`B: 正常` 在涨就说明一切正常。**

> 旧格式（v1.0.x）：`实时 31 · 日志 0 · 已译 31 · 错 0 · 3s 无新行`。
> v1.1 去掉它是因为「3s 无新行」把「没人说话」和「通道断了」显示成同一句话，
> 用户没法据此行动。

### 悬浮窗左上角的 ⚙ —— 按频道屏蔽 / 解开屏蔽

悬浮窗**左上角有个齿轮按钮**，点开是频道设置面板（思路跟游戏里的聊天设置一样）：

- **按分类屏蔽**：公会 / 小队 / 团队 / 密语 / 说话 / 喊话 / 战场 / 官员
- **公共频道**：`全部公共频道` 总开关 + **实际收到过的频道名**（如 `2. Newcomers`、`5. 世界`）

**取消勾选 = 屏蔽**。被屏蔽的频道**不显示、也不送去翻译** —— 屏蔽判定在
`dispatch_payload` 里、翻译**之前**完成，所以能实打实省下 API 调用。
改动立刻写入 `config.json` 的 `mute` 段，重启后依然有效：

```jsonc
"mute": {
  "labels":   ["说话", "喊话"],      // 按分类
  "channels": ["2. Newcomers", "*"]  // 按频道名；"*" = 全部公共频道
}
```

> 频道列表是**动态发现**的：收到过该频道的消息才会出现。面板开着时来了新频道，
> 列表会自动刷新。

### 方括号里显示的是什么

| 显示 | 含义 |
|---|---|
| `[公会]` `[小队]` `[团队]` `[密语]` `[战场]` `[官员]` `[说话]` `[喊话]` | 聊天分类 |
| `[2. Newcomers]` `[5. 世界]` | **公共频道的真实频道名**（带序号） |

频道名来自 WoW `CHAT_MSG_CHANNEL` 的第 4 个参数，插件原样带给桥接端，所以
**任何私服、任何频道名都能正确显示**。

> 实现上**没有改 payload 段数**：公共频道消息只是把第 4 段（标签段）填成频道名，
> 其余事件仍填 `CHAT_MSG_*`。桥接端 `EVENT_LABELS.get(event, event)` 天然兜底，
> 因此连旧版 EXE 都能正确显示频道名。

### 为什么 payload 里的点号要转义成 `%2E`（v1.0.14）

**客户端把 `SetTexture` 的路径当文件名处理**：在**最后一个点号**处切分，把后半段
当扩展名，直接替换成 `.blp` / `.tga` 各记一行。所以 payload 里任何点号之后的内容
都会被吃掉 —— 这是通道 B 唯一的编码限制。

实测证据（同一会话，聊天日志存原文、MissingFiles 存客户端改写后的结果，逐条配对
116 条）：

| 原文 | 客户端实际记录 |
|---|---|
| `Might. It adds defence stuff` | `Might.blp` |
| `I like the model though...its just to grindy` | `I like the model though...tga` |
| 频道名 `2. Newcomers`（在标签段） | `2.blp` ← 后面还有作者和正文，全丢 |

**v1.0.13 的公共频道翻译不了就是这个原因**：频道名里的点号在标签段，整条 payload
被截成 `2.blp`（只剩 4 段），桥接端 `require_six` 直接丢弃，静默无提示。
顺带它还在**一直**悄悄砍掉带句号的消息尾巴（`Might. It adds...` 只翻出「Might」）。

修法：`esc()` 把 `.` 转义成 `%2E`，桥接端 `unesc()` 还原。`%` 转义序列是安全载体
（客户端对 `%7C`、`%25` 等 116/116 原样保留，不做任何解码）；空格、逗号、问号、
冒号、星号、等号也全部实测无损，**只有点号需要处理**。

### 为什么 `autoflush` 默认关掉了

v1.0.10 及以前，插件会在每次发消息后「把聊天日志关掉 1 秒再打开」，
想借此逼客户端把缓冲区写盘。**实测证明这招无效**：`/atai status` 显示刷盘 19 次，
而 `WoWChatLog.txt` 的修改时间整整 22 分钟没动过。

更糟的是它有副作用 —— 日志关闭的那 1 秒里客户端不记录任何聊天，
反而在通道 A 上戳出空洞。既然通道 B 已经实测实时可用，就没必要再为了一个
无效动作去破坏通道 A 的完整性。想恢复旧行为：`/atai autoflush on`。

默认：

- 公会、小队、团队、密语、战场、公共频道、官员：ON
- Say / Yell：OFF

## API Key

**只支持智谱（GLM）**，默认模型 `glm-4-flash-250414`。

### Key 只存在 Windows 用户环境变量里（v1.0.16）

**`config.json` 里永远不会出现明文 Key** —— 这是硬约束：`save_config()` 在写文件
前会主动把 `compat.api_key` 剥掉，老配置里躺着的明文也会在启动时被搬进环境变量
再抹掉。

管理界面的 `ZHIPU_API_KEY` 那一栏就是环境变量本身的镜像：

- 环境变量里**有** Key → 显示成圆点；**没有** → 空白；
- 在那一栏粘贴新 Key 再点「开始翻译」→ 程序把它**写进用户环境变量**
  （`HKCU\Environment`，等价于 `setx` 但不弹黑框），配置文件里不留任何痕迹；
- 留空 = 保持现有环境变量不变（不会清空）。

也可以自己设：

```powershell
[Environment]::SetEnvironmentVariable("ZHIPU_API_KEY","你的KEY","User")
```

程序**直接读注册表**，设完不用重启电脑，也不用重开终端。

Key 从 https://bigmodel.cn/usercenter/proj-mgmt/apikeys 申请。

> 别把 Key 放进插件目录、别发给别人、别提交到 git。

### 悬浮窗长宽 / 位置 / 字号

管理界面第 3 节，对应 `config.json` 的 `overlay`：

```jsonc
"overlay": {
  "x": 40, "y": 520,          // 左上角屏幕坐标
  "width": 500, "height": 200,
  "alpha": 0.88,               // 不透明度
  "font_size": 9,
  "max_lines": 7
}
```

点「预览位置」会按当前数值闪一个框，不用进游戏就能把位置调好。
数值非法（空、字母、负数）一律退回默认并夹到合理区间，不会把窗口弄丢。

## Windows Bridge 运行

### 方式 A：用打包好的 EXE（推荐）

`WindowsBridge\dist\` 下有两个：

- `ArdenTranslatorAI.exe` —— **平时双击这个**：先出管理界面，点「开始翻译」后
  自动装插件并弹出悬浮窗
- `ArdenTranslatorAI-check.exe` —— 带控制台的诊断版，不翻译时用它查原因

EXE 是单文件、自带 Python 运行时和插件，**目标机器不需要装 Python**。

命令行参数：

- `ArdenTranslatorAI.exe --overlay` —— 跳过管理界面，直接用已存好的配置上悬浮窗
- `ArdenTranslatorAI-check.exe --check` —— 不启动界面，打印配置 / Key / 两条通道 /
  已装插件版本，并发一条真实翻译请求

### 方式 B：直接运行 Python

Windows 安装 Python 3.11+（要勾上 `tcl/tk and IDLE`，否则没有 tkinter），双击：

`run_python.cmd`

## 打包（生成 EXE）

双击：

`build_exe.cmd`

它会自动找带 tkinter 的 Python、安装 PyInstaller，然后生成上面那两个 EXE。
构建脚本会先清空 `dist\`，避免构建失败时留下旧的 EXE 看起来像成功了。

> **改了插件就要重新打包**：插件是用 `--add-data` 嵌进 EXE 的，不重新构建的话
> 部署到游戏里的还是旧版本。构建脚本会检查 `..\ArdenTranslatorAI\` 是否存在。

## 把 EXE 发给朋友（v1.0.17）

**可以发。** `ArdenTranslatorAI.exe` 里不含你的 Key，也不含你的 `config.json`。

发的时候把 `dist\` 下三个文件一起打包就行：

```
ArdenTranslatorAI.exe          ← 双击这个
ArdenTranslatorAI-check.exe    ← 出问题时用
使用说明.txt                    ← 最简单的 3 步说明
```

EXE 里装了什么：

| 内容 | 来源 | 会不会带上你的东西 |
|---|---|---|
| Python 3.11 运行时 + tkinter | PyInstaller 自动打包 | 否 |
| 插件 `ArdenTranslatorAI.lua` / `.toc` | `--add-data ...;plugin` | 否（纯代码） |
| 程序代码（编译成 `.pyc` 并压缩） | `ArdenTranslatorAI.py` | 否 |
| **API Key** | —— | **从来没有**，v1.0.16 起只从环境变量读 |
| **config.json** | —— | **不打包**，在运行它的机器上首次运行时生成 |

想自己验证就跑：

```
python _leakcheck.py
```

它会拿你注册表里真正的 `ZHIPU_API_KEY` 去逐字节扫两个 EXE 和 `dist\` 下每一个文件，
再枚举 PyInstaller 实际打包的所有 DATA 条目，确认没有 `config.json` / `.env` /
密钥文件混进去。全绿才会打印 `NO SECRET FOUND - safe to share`。

朋友那边要做的三件事：

1. 双击 EXE（SmartScreen 会拦一次，点「更多信息」→「仍要运行」）
2. 自己申请一个智谱 Key（open.bigmodel.cn，免费的），粘进管理界面的 Key 栏
3. 填他的魔兽目录 → 点「开始翻译」

他的 Key 会写进**他那台机器**的用户环境变量 `ZHIPU_API_KEY`，跟你的互不影响。
如果他那台机器从来没设过 Key、又留空点开始，程序会先弹确认告诉他去哪儿申请，
而不是静默放行到翻译时才报错。

## 配置

配置保存在：

`%LOCALAPPDATA%\ArdenTranslatorAI\config.json`

首次运行自动生成。关键字段：

```jsonc
{
  "provider": "zhipu",
  "compat": {
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-4-flash-250414",
    "api_key_env": "ZHIPU_API_KEY"   // 只存**变量名**，不存 Key 本身
  },
  "wow_path": "C:/WOW/ArdenWoW/Wotlk",
  "overlay": { "...": "见上" }
}
```

改完重启 EXE 生效。`api_key_env` 填的是**变量名**（字符串），想换变量名就改这里。
`base_url` 理论上可以指向任何 OpenAI 兼容端点（比如本地 Ollama），但界面上不提供
入口 —— 只支持智谱是刻意的。

### `wow_path` 该选哪一级？（v1.1.1）

**客户端目录布局不止一种**，选错一级就会显示「未找到 Logs」：

| 客户端类型 | 该选的目录 | 里面的样子 |
|---|---|---|
| ArdenWoW 等传统客户端 | 含 `Logs\` 的根目录 | `<root>\Logs\`、`<root>\Interface\AddOns\` |
| Grimfall 等**启动器套壳**客户端 | `…\resources\Wotlk` | `<root>\resources\Wotlk\Logs\`、`<root>\resources\Wotlk\Interface\AddOns\` |

**怎么快速判断**：在资源管理器里翻一下，看到 `Logs` 文件夹的那一层就是该选的层。
或者直接跑 `ArdenTranslatorAI-check.exe`，它会打印候选路径：

```
Logs 目录  : C:\WOW2\Grimfall\resources\Wotlk\Logs
             -> 存在
候选路径(按可信度)：
  C:\WOW2\Grimfall\Logs                          [不存在]
  C:\WOW2\Grimfall\resources\Wotlk\Logs          [存在]  ← 用这个
```

**不用纠结选偏**：v1.1.1 会自己往下探 `resources\Wotlk`，
即使用户选到了 `<root>`（少一级）或选到 `…\resources\Wotlk\Logs`（多一级），
也能正确定位。

**日志文件名也不统一**：ArdenWoW 系写 `MissingFiles.txt`，Grimfall 系写
`MissingFiles.log`，内容格式完全一样。v1.1.1 两个都认。

## 自动语言识别

本地先做轻量判断：

- 英文 → 翻译
- 日文（平假名/片假名）→ 翻译
- 俄文（西里尔字母）→ 翻译
- 中文 → 跳过
- 纯数字/符号 → 跳过

## 缓存

SQLite 缓存在：

`%LOCALAPPDATA%\ArdenTranslatorAI\cache.sqlite3`

缓存键包含 provider + model + 原句。相同句子再次出现时不请求 API，直接显示缓存
结果（悬浮窗里会标 `[缓存]`）。

## WoW 专有名词保护

`config.json` 的 `translation.protected_terms` 可以加入你不希望被 AI 翻译的词，
例如服务器名、公会名、特定技能/装备名。

**默认值里前 4 个是 ArdenWoW 那台服特有的，换服请替换成你自己服的词：**

- `ArdenWoW`、`Ascension`、`Runemaster`、`Geo Primalist` ← 换成你的服务器名 / 自定义职业名
- `Alliance`、`Horde`、`PvP`、`PvE`、`DPS`、`AOE` ← 通用，不用动
- `LFG`、`LFM`、`BiS`、`DoT`、`HoT`、`CC`

程序会先替换成占位符，翻译完成后再恢复原词。

> **频道名/组别不一样不影响使用**：插件按聊天**事件类型**（`CHAT_MSG_CHANNEL` 等）
> 过滤，不看频道叫什么名字、是几号频道。

## 延迟

- 通道 B 的写盘是**秒级**的，EXE 轮询 `MissingFiles.txt` 约 0.4 秒一次。
- 实际总延迟主要取决于 AI API 与网络，正常情况下**几秒内**上屏。
- 通道 A 的延迟不可控（取决于客户端何时刷盘），只作兜底。

## 安全/隐私

只有插件筛选过的聊天文本会发给智谱 API。
**API Key 只存在 Windows 用户环境变量里**，不写进 `config.json`、不进游戏目录。
（`save_config()` 会强制剥掉任何 `compat.api_key`，所以明文落盘这条路是堵死的。）
