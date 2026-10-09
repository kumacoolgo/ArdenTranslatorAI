# 更新记录

本文件记录每次版本升级改了什么、**为什么**改、以及怎么回滚。
逐项对应《ArdenTranslatorAI_v1.1_v1.2_修改方案_WorkBuddy.md》里的要求编号。

---

## v1.1.0

版本号：桥接 `1.1.0` / 插件 `1.1.0` / 协议兼容 `ATAI1(5 段, 历史) + ATAI2(6 段)`

### 一句话概括

把「一条消息一次翻译」的老管道，换成**有界队列 + 固定 worker + 版本化缓存 +
有界去重 + 通道健康状态机**；修掉一个会让用户原文被改写的一次性解析 bug；把构建
和安装从「写死开发机路径」改成「可复现、失败可回滚」。

### 逐项

#### V11-01 [P0] 消息协议一次性解析

**改了什么**

* 新增 `WindowsBridge/protocol.py`：`parse_payload(raw, *, source)` 纯函数。
* 解析流程从「先整体 `unesc()` → `split('|')` → 再对每个字段 `unesc()`」，改成
  **在未解码的串上定位前 N 个 `%7C`，切开后每个字段只解一次**。

**为什么**

线上格式里**分隔符本身也编码**：

```
ATAI2%7C<session>%7C<seq>%7C<label>%7C<sender>%7C<text>
```

老实现先整体解码，于是正文里的字面 `|`（插件编码成 `%7C`）会变回裸 `|` 参与
切分，把一条消息切错位；同时后续字段又被解了第二次，`%252E` 这类内容被多剥一层。

实测：

```
wire "The code is %252E"  --解 1 次-->  "The code is %2E"   正确
                          --解 2 次-->  "The code is ."     改写用户原文
```

`%257C` 天然安全：它的字符序列是 `%`,`2`,`5`,`7`,`C`，**不包含** `%7C` 三字符
子串。这正是「不做通用 URL 解码」的价值 —— 一旦按 `urllib.parse.unquote` 语义，
`%257C` 会被一层层剥开，分隔符和正文就分不清了。

**兼容性**：`@ATAI1@` 的历史 5 段格式继续支持（老聊天日志还能读）；`ATAI2`
只认 6 段，格式不符宁可丢掉也不按 5 段错位解析 —— 错位不报错但整条全错，更难查。
前缀与来源必须匹配：`@ATAI1@` 出现在 MissingFiles 里会被拒收。

#### V11-02 [P0] 有界队列 + 固定 worker

**改了什么**

* 新增 `WindowsBridge/jobs.py`：`BoundedJobQueue(maxsize=100, max_workers=2)`。
* 删掉 `App.translate_one()`（原来是「每条消息起一个线程 + 信号量」）。
* `App` 起固定 worker 线程（数量夹在 1~4），`pump_ui()` 只在 Tkinter 主线程跑。

**为什么**

老实现的消息洪峰会瞬间起几百个线程，把 CPU 和 API 配额一起打爆。
`put()` 现在**绝不阻塞**（入队方是日志 tail 线程，阻塞它等于停止读日志）。

队列用 `heapq` + `Condition` 而不是 `queue.Queue`：需要「满时按优先级挑牺牲品」，
`queue.Queue.put()` 做不到。

优先级：密语(0) < 小队(1) < 公会(2) < 公共频道(3)。**同优先级不允许插队** ——
否则世界频道洪峰会让队列变 LIFO，消息顺序全乱。

TTL：公共频道 30s，其他 120s。过期任务在 worker 取出前再查一次，省一次 API 调用。

#### V11-03 [P1] 缓存版本化 + inflight 合并

**改了什么**

* 新增 `WindowsBridge/cache.py`：`cache_key()`（含 `v2` 前缀）、`TranslationCache`、
  `SingleFlight`。
* 缓存 key 现在包含 provider / base_url / model / target / 正文 / 术语表指纹 /
  `PROMPT_VERSION` / `CACHE_KEY_VERSION`。

**为什么**

老 key 只有 `文本 + target`，改了模型或术语表之后仍会命中旧译文（静默返回过期
结果，最难发现的一类 bug）。现在任何一项变化都会换 key。

* **不做 `.lower()`**：只折叠空白。大小写不同的文本可能是不同意思，
  强行归一化会让 `LFG` 和 `lfg` 共用一条译文。
* 新 key 写入**同一张表**，老行自然不再命中但**不删除**（规范要求不破坏老库），
  新增 `meta` 表记录 `cache_key_version`。
* `SingleFlight`：同一 key 并发请求只发一次，其余等待者共享结果。owner 失败时
  异常共享给所有等待者，并在 `finally` 里必定释放 inflight（防永久挂起）。

#### V11-04 [P1] 会话 ID + 有界去重

**改了什么**

* 新增 `WindowsBridge/dedupe.py`：`BoundedLruSet` / `SessionWatermark` / `Deduper`。
* 插件 `SESSION` 重写：`秒级时间 + SavedVariables 自增计数 + 2 位随机尾数`，
  **全字符串拼接**（Lua 数字是 double，2^53 以上会丢精度，绝不能做算术）。

**为什么**

* `BoundedLruSet` 用 `OrderedDict` 做真 LRU，**容量满时绝不 `clear()`** ——
  老实现一旦清空，刚去重过的消息会全部重新翻译一遍（重复刷屏）。
  容量有下限 16，且 `add()` 永不抛错。
* `SessionWatermark` 只在对齐 `(session, seq)` 时接受 **seq 严格大于水位** 的消息。
  这挡掉「MissingFiles 被整体重放」——旧行在进入去重表**之前**就被挡掉，
  所以不需要把去重表撑大到能装下整个文件。
* `.blp` / `.tga` 同一 payload 只翻一次；跨通道（A/B 都收到同一条）也只翻一次；
  **同文本不同玩家**（不同 seq）都要保留。

#### V11-05 [P1] 关闭无效自动刷盘副作用

**改了什么**

* 插件 `/atai test`：删掉 `beginFlush()` / `needFlush = true`。
* 插件 `runDiag()`：删掉 `forceFlush()` + `flushTimer = 0.6`。
* 插件 `/atai flushtest`：**必须显式 `/atai flushtest yes`** 才执行；
  结束时恢复 `autoFlush` 原值（不再写死 `true`）。
* 相关注释块加 `【v1.1 更正】` 标注：旧推理是基于**错误假设**写的。

**为什么**

这些「顺手刷一下盘」的副作用会污染诊断结果 —— 用户跑 `/atai diag` 时看到有数据，
就以为通道 A 是实时的，实际平常几十条消息都不刷一次。现在 `runDiag()` 明确
**告知**「通道 A 不是实时的，要等缓冲区满或退出」。

#### V11-06 [P1] 安全安装 / 构建 / 配置迁移

**改了什么**

1. **`deploy_addon()` 重写**：`校验 → 同卷临时目录写入 → 校验（含读 .toc 版本）
   → 现有目录改名 .bak-<stamp> → 原子 rename → 失败自动回滚`。
2. **`.spec` 彻底去掉写死的 `C:/Users/cheny/...`**：改用 `SPECPATH` 计算
   `../ArdenTranslatorAI`，缺失时 `raise SystemExit` 并给出人话。
3. **`build_exe.cmd` 改为驱动 `.spec`**：不再删 spec、不再在命令行传 `--add-data`，
   并加了一步「构建完自动跑 `--check`」。
4. **`load_config()`**：坏 JSON 备份成 `.json.bad` + 用默认值 + 置
   `_recovered_from_bad_config`，**不静默重置**。
5. **`--diag` / `--version` CLI**：导出脱敏诊断报告；报告明写「Key 是明文环境
   变量，不是加密存储」。

**为什么**

* 老实现「先 `rmtree` 再重建」——中途断电/复制失败会留下**空目录**，游戏加载到
  半个插件，比没装更糟。现在目标目录只**改名**不删，失败可原样回来。
* 写死的绝对路径在别人机器上必然不存在，PyInstaller 会构建失败；更糟的情况是
  它「成功」了但包里没有插件。
* `hiddenimports` 白名单同步修掉：删掉了并不存在的 `translator` 模块。

### 新增模块

| 文件 | 职责 |
|---|---|
| `WindowsBridge/protocol.py` | V11-01 一次性解析 |
| `WindowsBridge/jobs.py` | V11-02 有界优先级队列 + 固定 worker |
| `WindowsBridge/cache.py` | V11-03 版本化缓存 + singleflight |
| `WindowsBridge/dedupe.py` | V11-04 会话水位 + 有界 LRU 去重 |
| `WindowsBridge/transport.py` | V12-03 通道健康状态机（骨架 + 已接状态行） |
| `WindowsBridge/diagnostics.py` | 脱敏诊断 / 版本 / 延迟统计（V12-06 骨架） |

### 测试

新增 5 个测试文件（全部离线可跑，不联网、不花钱）：

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_protocol.py` | 27 | 点名文本、Unicode、点号频道名、5/6 段、坏包、二次解码 |
| `tests/test_queue.py` | 16 | 容量上限、优先级、同优先级不插队、TTL、500 条突发 |
| `tests/test_cache.py` | 26 | key 组成、不做大小写折叠、老库保留、20 并发合并 |
| `tests/test_dedupe.py` | 21 | `.blp/.tga`、跨通道、不同玩家、会话重放、容量有界 |
| `tests/test_transport.py` | 22 | 状态机、文件重写/截断、同大小换内容、UTF-8 断字节 |
| `tests/test_retry.py` | 22 | 401/429/500/超时/空 choices、错误分层 |
| `tests/test_deploy.py` | 18 | 首次安装、覆盖升级、失败回滚、路径安全闸、幂等 |
| `tests/test_diagnostics.py` | 24 | 脱敏保证、版本一致性、分位统计 |

外加 `_selftest.py`（171 项）与 `_gui_probe.py`（真实 Tk 窗口）。

**合计 176 个单元测试 + 171 项自检 + GUI 冒烟，全部通过。**

### 本次测试抓到的真实 bug

1. **通道 A 的 marker 被计两次**：`handle_log_line()` 和 `_ingest()` 都加了
   `stat["marker"] += 1`，状态行数字直接翻倍。修法：`_ingest()` 作为唯一计数出口。
2. `_split_encoded()` 的字段数语义混乱（`FIELDS_V6` vs `FIELDS_V6 - 1`）。
3. `@ATAI1@` 出现在 MissingFiles 时被误接受。
4. `dedupe.seen(None)` 抛 `AttributeError`。

### 回滚方案

**方式一：换回旧文件（推荐，最省事）**

```bash
# 备份目录已含完整 v1.0.17 源码
robocopy "ArdenTranslatorAI_v1 - 副本" "ArdenTranslatorAI_v1" /E /PURGE
```

或手工：

1. 用 `ArdenTranslatorAI_v1 - 副本` 覆盖 `ArdenTranslatorAI_v1`。
2. 重建 EXE：`WindowsBridge\build_exe.cmd`。
3. 游戏内 `/reload`。

**方式二：只回滚插件（EXE 不回滚）**

v1.1 桥接**兼容** v1.0.x 插件的 5 段协议，所以只回滚插件不会瞎。
把 `ArdenTranslatorAI_v1 - 副本\ArdenTranslatorAI\` 拷回即可。

**方式三：只回滚 EXE（插件不回滚）**

v1.0.17 桥接**不认** `ATAI2` 6 段格式（它只在 `@ATAI1@` 上工作）。
所以**不能**只回滚 EXE —— 插件会把消息发到 `ATAI2`，老 EXE 一条都读不到。
若必须只回滚 EXE，要同时把插件降回 v1.0.x。

**配置回滚**

`config.json` 向下兼容：v1.0.17 会忽略 `queue_capacity` / `queue_ttl_seconds` /
`queue_ttl_seconds_other` 三个新键，不会崩。若配置已被写坏，程序会自动备份成
`config.json.bad` 并提示。

**数据库回滚**

缓存库**原地升级、不删老行**。回滚到 v1.0.17 后老 key 会重新命中（老行还在），
新 key 对应的行留着不用，不影响正确性。

---

## v1.1.1（布局兼容修复）

版本号：桥接 `1.1.1` / 插件 `1.1.0`（未变）/ 协议 `ATAI1 + ATAI2`

### 一句话概括

v1.1.0 假设「客户端根目录下就有 `Logs\`」；但**很多客户端是 Electron 启动器套壳**，
真正的游戏根目录在 `<root>\resources\Wotlk\`，`Logs\` 和 `Interface\AddOns\` 都
躺在里面。结果是插件装对了、日志也写了，桥接却报「找不到 Logs」。
本版把路径解析改成**多布局自适应**，并兼容 `MissingFiles.log` 这个另一种文件名。

### 触发场景（用户实报）

用户在 `C:\WOW2\Grimfall` 里装了新版插件，桥接却显示：

```
[wow_path] C:\WOW2\Grimfall
状态行   : 未找到 Logs\ —— 进游戏执行 /atai diag 让插件生成日志
```

而实际上：

```
C:\WOW2\Grimfall\resources\Wotlk\Logs\MissingFiles.log      ← 已有 118 行 ATAI2 数据
C:\WOW2\Grimfall\resources\Wotlk\Interface\AddOns\ArdenTranslatorAI\  ← 插件已装
```

**通道 B 一直在正常工作**，纯粹是桥接找错了目录和文件名。

### 逐项

#### 布局自适应

新增：

* `_candidate_logs_dirs(wow_path)` —— 列出「这个客户端可能把 `Logs` 放哪」的全部
  候选，按可信度排序：
  1. `<root>\Logs`（布局 A：ArdenWoW 等）
  2. `<root>\resources\Wotlk\Logs`（布局 B：Grimfall 等 Electron 套壳）
  3. `<root>\Resources\Wotlk\Logs` / `<root>\Wotlk\Logs`（大小写/层级别名）
  4. 若 `wow_path` 本身名字是 `Wotlk`/`WoW`/`Client`，再往上找一层到两层
     的 `Logs`（用户可能把 `wow_path` 指到了子目录）
* `find_logs_dir(wow_path)` —— 从候选里挑**第一个真实存在**的。
* `find_chat_log(wow_path)` —— 在候选目录里找 `WoWChatLog.txt`。
* `find_missing_files(wow_path)` —— 在候选目录里找 `MissingFiles.txt`
  **或** `MissingFiles.log`。

**为什么用「候选列表」而不是「探测式递归」**：递归 `rglob("Logs")` 在 30GB 的
客户端目录上会卡死；候选列表是有限、可预期、O(1) 次 `stat`。

#### 文件名兼容

`MISSING_FILES_NAMES = ("MissingFiles.txt", "MissingFiles.log")`

实测：ArdenWoW 系客户端写 `MissingFiles.txt`，Grimfall 系写 `MissingFiles.log`。
两者内容格式完全一致（都是 `ATAI2%7C...`）。v1.1.0 只认 `.txt`，所以在
Grimfall 上会一直报「通道 B 未就绪」。

`start_tailer()` 也同步改了：找不到现成文件时，用**实际 Logs 目录** +
`MissingFiles.txt` 作为待创建路径（而不是拿 `wow_path` 硬拼）。

#### `addons_dir()` / `check_wow_path()` 重写

老逻辑要求「`Interface` 和 `Logs` 同层」，这在布局 B 下必然失败。
新逻辑：

* `addons_dir()`：优先返回**已装插件的位置**（`ArdenTranslatorAI.lua` 所在目录的
  父目录）→ 含 `Blizzard_*` 的 `AddOns` → 任意存在的 `AddOns` → 兜底首候选。
* `check_wow_path()`：在 `[root] + [root/sub...]` 里找 `Interface/AddOns`，
  不再要求与 `Logs` 同层。

#### 状态行三态

`NOT_CONFIGURED` 之前只有一句「未设置」；现在按真实情况分三种：

| 情况 | 文案 |
|---|---|
| `wow_path` 为空 | `未设置客户端目录 —— 点「设置」选到游戏根目录` |
| 有路径但找不到 Logs | `未找到 Logs 目录 —— 套壳启动器请选到 …\resources\Wotlk` |
| 找到 Logs 但文件还没出现 | `等待游戏写入日志 —— 进游戏后自动开始` |

这样用户能一眼看出是「路径选错」还是「还没进游戏」。

#### 界面提示

三处输入框提示语补上套壳布局说明：

```
ArdenWoW / 传统客户端      → 选到含 Logs 的根目录
Grimfall / 启动器套壳客户端 → 选到 …\resources\Wotlk
```

`cli_check()`（`--check`）新增 `Logs 目录 :` 一行，并列出所有候选路径及其
存在与否，方便用户自己判断选错了哪一级。

### 新增测试

`tests/test_layout.py` —— 25 项，覆盖：

* 布局 A / 布局 B / 大小写变体 / 多层级
* `wow_path` 指到子目录
* `MissingFiles.txt` 与 `.log` 双文件名
* 空路径 / 不存在的路径 / 带引号与尾空格的路径
* 候选去重与顺序稳定性
* `addons_dir()` 在两种布局下的取值
* `check_wow_path()` 不再要求 Interface 与 Logs 同层

### 实测验证

在真实 `C:\WOW2\Grimfall` 客户端目录上跑 `--check`：

```
桥接版本   : ArdenTranslator AI 1.1.1
Logs 目录  : C:\WOW2\Grimfall\resources\Wotlk\Logs
通道 A     : C:\WOW2\Grimfall\resources\Wotlk\Logs\WoWChatLog.txt
             -> 存在 （**非实时**：要等客户端缓冲区满或退出才刷盘）
通道 B     : C:\WOW2\Grimfall\resources\Wotlk\Logs\MissingFiles.log
             -> 存在
已装插件   : C:\WOW2\Grimfall\resources\Wotlk\Interface\AddOns\ArdenTranslatorAI
             -> 版本 1.1.0 · 与内置 一致
翻译结果   : 寻找治疗者组队去祖尔法拉，私聊我
[成功] 配置可用，现在可以启动悬浮窗了。
```

用该客户端的 `MissingFiles.log` 做**离线管道验证**：

* 118 行 `ATAI2` 全部解析成功，**0 失败**
* 去重后 **59 条唯一消息**（发送方重复发送被正确折叠）
* 频道分布 `{'6. World': 116, 'WorldDefense': 2}`
* 含引号/特殊字符的文本完整还原

顺手挖出两个**格式佐证**：

* `LFM Tank M0 Tour` 后面客户端多加了 `.blp` —— 证明客户端确实在**最后一个点号**
  处腰斩再拼扩展名，`strip_miss_suffix()` 的语义是对的。
* `6%2E World` —— 证明 v1.0.14 的 `%2E` 转义正确，且逆解码后得到 `6. World`。

### 没改什么

* **协议格式未变** —— 仍是 `ATAI1(5 段) + ATAI2(6 段)`。
* **插件未变** —— 仍是 `1.1.0`，用 v1.1.0 的插件配 v1.1.1 的桥接完全正常。
* **config.json 结构未变** —— 用户原来的 `wow_path` 无需手改；即使用户已经手动
  指到 `resources\Wotlk`（正确值），新代码也照样认。

### 回滚

v1.1.1 只改了**路径解析**，不涉及数据格式。回滚到 v1.1.0 只需换回 `.py`/EXE，
`config.json` 与缓存库都无需处理。

---

## v1.2（计划中，尚未实施）

v1.1 验收通过后再做。骨架已在 v1.1 落地：`transport.py`（通道健康状态机）、
`diagnostics.py`（脱敏导出）、`TranslationJob.settings_snapshot`（配置快照）、
`display_order`（UI 占位排序）。

| 编号 | 内容 |
|---|---|
| V12-01 | 优先级 + aging（防低优先级饿死） |
| V12-02 | 顺序 / 占位 / UI 批刷新 |
| V12-03 | 通道健康状态机完善（自动降级 / 退避） |
| V12-04 | 启动回补与断线恢复 |
| V12-05 | 超时 / 重试 / 429 `Retry-After` / 错误分层 |
| V12-06 | 状态仪表盘 + 脱敏诊断导出 |
| V12-07 | 长消息测量与 ATAI3（可留作后续实验功能） |
