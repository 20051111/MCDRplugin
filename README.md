# MCDRplugin

TPW 的 MCDReforged 插件 / 资源仓库。

| 目录 | 内容 |
|---|---|
| [`plugins/`](plugins/) | MCDReforged 插件（当前：[简单放置破坏榜](#简单放置破坏榜-simpleplacedminedscoreboard)、[仓库审计日志](#仓库审计日志-warehouseauditlog)） |
| [`材质包/`](材质包/) | 服务器材质包相关文件 |

---

# 简单放置破坏榜 (SimplePlacedMinedScoreboard)

MCDReforged 单文件插件：在 Minecraft 服务端侧边栏（sidebar）实时显示**八个统计榜单**，
数值由数据包逐 tick 增量累加，**无需 RCON、不依赖客户端模组**。

- 插件 id：`sidebar_boards`（内部 id 与数据包命名空间保持不变，升级不会重置世界里的计分项）
- 命令：`!!spms`（旧命令 `!!dsb` 已不再注册）
- 依赖：MCDReforged ≥ 2.1.0，Minecraft 1.20.x（数据包 `pack_format` 按 `minecraft_version` 配置自动选择）
- 当前版本：**v1.5.0**

## 功能特性

### 八个榜单，自由切换

| 命令 | 榜单 | 统计口径（stats JSON 分类） |
|---|---|---|
| `!!spms break` | 破坏总和榜 | `minecraft:mined` 所有项求和 |
| `!!spms place` | 放置总和榜 | `minecraft:used`（与 mined 取交集）求和 |
| `!!spms both` | 放置/破坏同榜 | 两个独立排行榜上下堆叠在同一侧边栏，**不相加** |
| `!!spms craft` | 合成榜 | `minecraft:crafted` 求和 |
| `!!spms drop` | 丢弃榜 | `minecraft:dropped` 求和 |
| `!!spms pick` | 拾荒榜 | `minecraft:picked_up` 求和 |
| `!!spms kill` | 击杀榜 | `minecraft:killed` 按实体求和 |
| `!!spms custom` | 综合榜 | `minecraft:custom` 全部杂项统计求和 |

单榜格式统一为「全服总和行 + 排行榜」：名字列左对齐、数值走原版分数列右对齐。

### 实时更新

- 首次进入 datapack 模式时从 `world/stats/<uuid>.json` 导入每个玩家的历史总和；
- 之后完全由数据包监听原版统计准则（`minecraft.mined:*` / `minecraft.used:*` /
  `minecraft.crafted:*` / `minecraft.dropped:` / `minecraft.picked_up:` /
  `minecraft.killed:<实体>` / `minecraft.custom:<统计项>`）逐 tick 累加，**不读 JSON、不走 RCON**；
- 破坏/放置等常用分类逐 tick 消费；craft/drop/pick/kill/custom 准则项有几千项，
  按 `extra_scan_interval_ticks`（默认 5 tick = 250ms）批量消费，侧边栏无感知。

### 纯假人榜单

`!!spms botboard on|off`：打开后**所有榜单只上榜 `bot_` 前缀的 carpet 假人**，
方便给假人单独开榜/刷数据；关闭后恢复只上榜真人。

> 注意：carpet 假人的统计大多集中在 `custom`（在线时长、行走距离等），mined/used 通常为 0，
> 所以纯假人模式下看综合榜（`!!spms custom`）才有数据。

### 其他命令

| 命令 | 说明 |
|---|---|
| `!!spms` | 精简 help：榜单标题是**可点击按钮**（点击即切换，悬停显示统计项英文） |
| `!!spms show` / `hide` | 显示 / 隐藏侧边栏（分数保留，随时再开） |
| `!!spms status` | 榜单与运行状态（含假人开关状态） |
| `!!spms debug` | 诊断路径 / 文件数 / 每人口径 |
| `!!spms dp` | 数据包模式诊断（模式、pack_format、RCON、生成的文件） |
| `!!spms dp rebuild` | 重建实时数据包（保留已累计分数） |
| `!!spms refresh` | 立即重画 |

切换榜单、`botboard`、`show/hide`、`dp rebuild` 需要权限等级 2。

## 安装

1. 把 `plugins/simpleplacedminedscoreboard-1.5.0.py` 放进 MCDR 的 `plugins/` 目录；
2. 重启（或等 AutoPluginReloader 自动重载）；
3. 进游戏执行 `!!spms` 查看 help，点按钮切换榜单。

插件会在世界目录下生成自己的数据包（`world/datapacks/sidebar_boards/`），
世界里的 `dsb_*` 计分项由插件和数据包共同维护，请勿手动删除。

## 配置

首次运行后生成 `config/sidebar_boards/config.json`，常用项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `realtime_mode` | `datapack` | `datapack`（推荐，逐 tick 实时）或 `poll`（插件轮询 JSON） |
| `active_board` | `break` | 启动时显示的榜单 |
| `board_top_n` | `10` | 单榜排行榜条数（≤14） |
| `both_top_n` | `6` | 同榜每边条数（2 个总和行 + 2×N ≤ 15，最多 6） |
| `show_blank_row` | `false` | 总和行与排行榜之间是否留空行 |
| `thousands_separator` | `""` | 千分位分隔符（`,` / `.` / 空格 / 空） |
| `sidebar_visible` | `true` | 启动是否直接显示侧边栏 |
| `bot_only_mode` | `false` | 纯假人榜单（等价 `!!spms botboard on`） |
| `bot_name_prefixes` | `["bot_"]` | 假人名字前缀 |
| `extra_scan_interval_ticks` | `5` | 新分类批量消费间隔（tick） |
| `minecraft_version` | `1.20.4` | 决定数据包 `pack_format` |
| `server_path` | `""` | 服务端目录（留空 = MCDR 工作目录） |
| 标题/标签 | 见配置 | `break_title` / `place_title` / `both_title` / `craft_title` / `drop_title` / `pick_title` / `kill_title` / `custom_title` 与对应 `total_*_label` |

改配置后插件会自动重建数据包；也可以 `!!spms dp rebuild` 手动重建。

## 侧边栏怎么画的

原版一个 display slot 只能挂一个计分项，team 槽位只是「按队伍颜色区分可见性」，
并不能把多个榜单叠起来。所以本插件：

- 每个榜各建一个 dummy 计分项（`dsb_rank_brk` / `dsb_rank_plc` / `dsb_rank_cft` …）作为展示项；
- 用假玩家名占行（`dsb_br01`…`dsb_br15`），靠 `scoreboard players display name`
  写入「名次 + 名字」；
- 数值直接当占位行的分数：原版侧边栏名字列左对齐、分数列右对齐，排序也由分数决定
  （同分按占位行名字升序，占位行按名次零填充命名，正好维持名次顺序）；
- 同榜（both）用名次分排序、数值写进显示名（`numberformat blank` 隐藏分数列）。

## 数据口径

- 破坏榜 = `sum(minecraft:mined)`（沿用 StatsHelper 的 `#all` 求和逻辑）；
- 放置榜 = 玩家各自 `used.keys() ∩ mined.keys()` 内的 used 计数总和，
  逐 tick 按 used 新增继续累计（首次从 JSON 导入初始值）；
- 其余分类榜 = 对应分类所有项求和；
- `bot_` 前缀玩家默认排除，`bot_only_mode` 打开后反过来只保留假人；
- 玩家名映射来自 `usercache.json` + 插件自己的 `uuid.json`（逻辑同 StatsHelper）。

## 版本历史

- **v1.5.0**：新增合成/丢弃/拾荒/击杀/综合五榜（原版统计分类全覆盖）；去掉总榜；
  新增纯假人榜单（`!!spms botboard`）；help 改为可点击按钮；
  新分类按 `extra_scan_interval_ticks` 批量消费；修复纯假人模式下假人被过滤、
  长假人名导致 tick 崩溃等问题。
- **v1.4.0**：破坏/放置/总榜/同榜四榜自由切换；同榜堆叠复刻；名字左对齐数值右对齐。
- **v1.3.x**：更早的 stacked 同榜版本。

## 致谢

玩家名/UUID 映射与统计读取逻辑参考 StatsHelper 的实现。

---

# 仓库审计日志 (WarehouseAuditLog)

MCDReforged 单文件插件：监控指定坐标范围内**仓库容器（箱子/陷阱箱/木桶/17 种潜影盒）的取出行为**，
记录到 SQLite 并以 CSV 导出，全程只读 RCON 轮询 + 异步队列写入，**不卡服**。

- 插件 id：`warehouse_audit`
- 命令：`!!wh`（`!!wh help` 查看全部）
- 依赖：MCDReforged ≥ 2.1.0，Minecraft 1.13~1.20.4 实测（1.20.5+ 自适应），需启用 RCON
- 当前版本：**v1.0.0**
- 源码与详细文档：[20051111/mcdr-warehouse-audit](https://github.com/20051111/mcdr-warehouse-audit)

## 功能特性

- **只记取出**：默认存入不记录、不记玩家 ID、不记维度、不记槽位（均可配置）
- **自动合并**：1 秒内同容器同物品的多次取出合并为一条、数量相加
- **内容物读取**：取出潜影盒/箱子物品时读取其内嵌 NBT，如 `packed_ice x640; chest x64`
- **容器自动发现**：区域 ≤2 万方块用 RCON 扫描，超出回退区域文件解析
- **数据安全**：服务端崩溃/关闭时合并缓冲立即写出、恢复后重新基线（回档不产生幽灵记录）；
  优雅退出排空队列，强杀最多丢约 0.5 秒未写记录

## 常用命令

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `!!wh recent [N]` | 所有人 | 查看最近 N 条记录（默认 10，最新在上） |
| `!!wh status` / `list` | 所有人 | 运行状态 / 已知容器列表 |
| `!!wh export [选项]` | helper+ | 导出 CSV（带统计时间跨度、服务端版本备注行） |
| `!!wh region set <名称> <维度> <x1> <y1> <z1> <x2> <y2> <z2>` | helper+ | 游戏内配置监控范围，立即生效 |
| `!!wh rescan` / `on` / `off` | helper+ | 重新枚举 / 恢复 / 暂停轮询 |

CSV 默认列：`时间, X, Y, Z, 容器类型, 物品ID, 变化量, 变化前, 变化后, 方向, 内容物`

## 安装

1. 把 `plugins/warehouse_audit-1.0.0.py` 放进 MCDR 的 `plugins/` 目录；
2. 确保服务端 RCON 已启用（`config.yml` 中 `rcon.enable: true`）；
3. 重启 MCDR 或等 AutoPluginReloader 自动重载；
4. 用 `!!wh region set ...` 配置区域（或编辑 `config/warehouse_audit/warehouse_audit.json`）。

## 版本历史

- **v1.0.0**：首个版本。只记取出/不记玩家ID/不记维度/不记槽位；1 秒合并；
  内容物读取；区域指令；崩溃/回档数据安全；92 项单元测试。
