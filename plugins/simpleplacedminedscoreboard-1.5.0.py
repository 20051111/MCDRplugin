# -*- coding: utf-8 -*-
"""
简单放置破坏榜 (SimplePlacedMinedScoreboard) —— 服务端 Sidebar 八榜（破坏/放置/同榜/合成/丢弃/拾荒/击杀/综合）

插件名：简单放置破坏榜 (SimplePlacedMinedScoreboard)
插件命令：!!spms（旧命令 !!dsb 已不再注册）
内部 id / 数据包命名空间仍是 sidebar_boards，保持不变，避免世界里的计分项与索引重来。

单文件 MCDReforged 插件。

数据来源、路径、字段、UUID 映射、总和读法全部沿用参考插件 StatsHelper
（stats_helper/utils.py::get_stat_data + stats_helper/__init__.py::refresh_uuid_list）。

    world/stats/<uuid>.json  ->  stats["minecraft:mined"]  ->  sum(所有类型)

破坏榜 = sum(minecraft:mined 的所有项)                            （直接复用参考插件 #all 求和）
放置榜 = 玩家各自的 (used.keys() ∩ mined.keys()) 内 used 项计数总和
         并在逐 tick 过程中按 used 的新增数量继续累计

其余分类榜（合成/丢弃/拾荒/击杀/综合）口径一致：sum(该分类所有项)。

v1.5.0 起八个榜自由切换（!!spms help 里的榜单标题是可点击按钮，悬停显示统计项英文）：
    !!spms break  破坏总和榜（mined）      !!spms place  放置总和榜（used ∩ mined）
    !!spms both   放置/破坏同榜（堆叠，不相加）
    !!spms craft  合成榜（crafted）         !!spms drop   丢弃榜（dropped）
    !!spms pick   拾荒榜（picked_up）       !!spms kill   击杀榜（killed，按实体）
    !!spms custom 综合榜（custom，全部杂项统计相加）
    !!spms botboard on|off  纯假人榜单（打开后所有榜单只上榜 bot_ 假人）
    !!spms hide   统一隐藏
单榜格式：全服总和行 + 排行榜；名字列左对齐，数值走原版分数列右对齐。
同榜（both）复刻最老版本：破坏榜、放置榜上下堆叠，数值写进显示名、用名次分排序，
每边默认 6 条（2 个总和行 + 2*6 = 14 行，可用 both_top_n 调到 1~6）。
新分类（craft/drop/pick/kill/custom）的准则项太多，按 extra_scan_interval_ticks 批量消费
（默认 5 tick = 250ms，侧边栏无感知）；初始值同样从 stats JSON 导入。

渲染方式说明（见 README「侧边栏怎么画的」一节）：
    原版一个 display slot 只能挂一个计分项，team 槽位只是「按队伍颜色区分可见性」，
    并不是把多个榜单叠起来。所以每个榜各用一个 dummy 计分项（dsb_rank_brk / dsb_rank_plc /
    dsb_rank_cft / dsb_rank_drp / dsb_rank_pck / dsb_rank_kil / dsb_rank_cus），用假玩家名占行，
    靠 scoreboard players display name 写入「名次+名字」。
    数值直接当占位行的分数：原版侧边栏名字列左对齐、分数列右对齐，排序也由分数决定
    （同分按占位行名字升序，占位行按名次零填充命名，正好维持名次顺序）。
    在 Minecraft 1.20.4 中由数据包每 tick 汇总增量并用函数宏更新当前榜；无需 RCON。
    stats JSON 仅首次导入历史值，不作为实时数据源。
"""

PLUGIN_METADATA = {
    # 内部 id 保持不变：改 id 会让 config/数据包索引/计分项槽位全部重来，
    # 世界里的 dsb_* / dsbm* / dsbu* 计分项和历史分数也会变成孤儿。
    'id': 'sidebar_boards',
    'version': '1.5.0',
    # MCDR 的插件名只能是单个字符串（description 才支持多语言），所以中英一起写
    'name': '简单放置破坏榜 (SimplePlacedMinedScoreboard)',
    'description': {
        'en_us': 'Server-tick sidebar boards (break/place/both/craft/drop/pick/kill/custom), switchable via !!spms, pure-bot mode via !!spms botboard',
        'zh_cn': '服务端逐 tick 的破坏/放置/同榜/合成/丢弃/拾荒/击杀/综合八榜，!!spms 切换，!!spms botboard 切纯假人榜',
    },
    'author': 'TPW',
    'dependencies': {
        'mcdreforged': '>=2.1.0',
    },
}

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
import zlib
from typing import Any, Dict, List, Optional, Set, Tuple

from mcdreforged.api.all import *
from mcdreforged.api.rtext import RText, RTextList, RAction, RColor

# ===========================================================================
# 常量
# ===========================================================================

# 与参考插件 stats_helper/constants.py 的 AllTargetTag = '#all' 同名同义
ALL_TAG = '#all'

# 统计分类（参考插件里叫 cls，命令行是 minecraft.<cls>:minecraft.<target>）
MINED_CLASS = 'mined'   # 破坏榜用这个
USED_CLASS = 'used'     # 放置榜用这个
CRAFT_CLASS = 'crafted'     # 合成榜
DROP_CLASS = 'dropped'      # 丢弃榜
PICK_CLASS = 'picked_up'    # 捡起榜
KILLED_CLASS = 'killed'     # 击杀榜（按实体）
CUSTOM_CLASS = 'custom'     # 杂项统计榜（所有项相加）

# 计分项名：<=16 字符，只能 [A-Za-z0-9_.-]
# 每个玩家的实时总分（dummy，由数据包逐 tick 维护）
DP_OBJ_BREAK = 'dsb_brk'        # 破坏总和（minecraft:mined）
DP_OBJ_PLACE = 'dsb_plc'        # 放置总和（used ∩ mined 口径）
# 其余原版统计分类的每玩家总分
DP_OBJ_CRAFT = 'dsb_cft'        # 合成总和（minecraft:crafted）
DP_OBJ_DROP = 'dsb_drp'         # 丢弃总和（minecraft:dropped）
DP_OBJ_PICK = 'dsb_pck'         # 捡起总和（minecraft:picked_up）
DP_OBJ_KILL = 'dsb_kill'        # 击杀总和（minecraft:killed）
DP_OBJ_CUSTOM = 'dsb_cus'       # 杂项统计总和（minecraft:custom，全部项相加）
# 全服合计（侧边栏总和行用）
DP_OBJ_TOTAL_BREAK = 'dsb_tbrk'     # 全服破坏合计
DP_OBJ_TOTAL_PLACE = 'dsb_tplc'     # 全服放置合计
DP_OBJ_TOTAL_CRAFT = 'dsb_tcft'     # 全服合成合计
DP_OBJ_TOTAL_DROP = 'dsb_tdrp'      # 全服丢弃合计
DP_OBJ_TOTAL_PICK = 'dsb_tpck'      # 全服捡起合计
DP_OBJ_TOTAL_KILL = 'dsb_tkil'      # 全服击杀合计
DP_OBJ_TOTAL_CUSTOM = 'dsb_tcus'    # 全服杂项合计
DP_OBJ_FMT = 'dsb_fmt'              # 千分位格式化用的中间计分项（#fmt / #g1 / #g2 / #g3 / #1000 / #1000000）
DP_OBJ_BASE = 'dsb_B'  # 旧版名称，仅用于兼容诊断
DP_INDEX_FILE = 'datapack_index.json'
DP_SCHEMA = 5   # 4 -> 5：去掉总榜、新增 custom 榜，老玩家按新 schema 重新导入历史值

# 侧边栏展示计分项（dummy，假玩家占行）：每个榜各一个，用 setdisplay 切换
DP_BOARD_BREAK = 'dsb_rank_brk'
DP_BOARD_PLACE = 'dsb_rank_plc'
DP_BOARD_BOTH = 'dsb_rank_both'   # 放置+破坏同榜（堆叠式，互不相加）
DP_BOARD_CRAFT = 'dsb_rank_cft'   # 合成榜
DP_BOARD_DROP = 'dsb_rank_drp'    # 丢弃榜
DP_BOARD_PICK = 'dsb_rank_pck'    # 捡起榜
DP_BOARD_KILL = 'dsb_rank_kil'    # 击杀榜
DP_BOARD_CUSTOM = 'dsb_rank_cus'  # 杂项统计榜

# poll 模式的侧边栏展示计分项（只有一个，切换榜单时删旧建新）
OBJ_BOARD = 'dsb_brd'

# 侧边栏最多显示 15 行（原版限制）；总和行 + 换行按配置占用
SIDEBAR_RENDER_LIMIT = 15
# 占位行命名/清理的上限（dsb_*r01 .. dsb_*r15）
MAX_BOARD_ROWS = SIDEBAR_RENDER_LIMIT
# 同榜（放置+破坏）里每个分榜的行数上限：2 个总和行 + 2*N <= 15 -> N <= 6
MAX_BOTH_ROWS = (SIDEBAR_RENDER_LIMIT - 2) // 2

# 榜单的 id（#board dsb_ctl 的取值 = 这里的顺序）
BOARD_BREAK = 'break'
BOARD_PLACE = 'place'
BOARD_BOTH = 'both'
BOARD_CRAFT = 'craft'     # 合成榜
BOARD_DROP = 'drop'       # 丢弃榜
BOARD_PICK = 'pick'       # 捡起榜
BOARD_KILL = 'kill'       # 击杀榜
BOARD_CUSTOM = 'custom'   # 杂项统计榜（custom 全部项相加）

# 新版本首次需要由插件（控制台）补建的计分项（老世界里 load 的 add 会因重名中断，
# 这些新项必须由插件补建）；旧版 stacked 展示项则删除
DP_NEW_OBJECTIVES = (DP_BOARD_BREAK, DP_BOARD_PLACE, DP_BOARD_BOTH,
                     DP_OBJ_CRAFT, DP_OBJ_DROP, DP_OBJ_PICK, DP_OBJ_KILL, DP_OBJ_CUSTOM,
                     DP_OBJ_TOTAL_CRAFT, DP_OBJ_TOTAL_DROP, DP_OBJ_TOTAL_PICK,
                     DP_OBJ_TOTAL_KILL, DP_OBJ_TOTAL_CUSTOM,
                     DP_BOARD_CRAFT, DP_BOARD_DROP, DP_BOARD_PICK, DP_BOARD_KILL, DP_BOARD_CUSTOM)
DP_LEGACY_OBJECTIVES = ('dsb_rank', 'dsb_tot', 'dsb_ttot', 'dsb_rank_tot')   # 旧总榜/stacked 遗留项
DP_OBJECTIVES_MARK = 'dp_objectives_v6.done'

# Minecraft 1.20.4 原版实体类型注册表（击杀榜 criteria 用；来源同 blocks/items）
# 运行时还会并入玩家 stats 里出现过的实体 id，覆盖模组实体。
ENTITIES_1204 = (
    'allay', 'area_effect_cloud', 'armor_stand', 'arrow', 'axolotl', 'bat', 'bee', 'blaze',
    'block_display', 'boat', 'breeze', 'camel', 'cat', 'cave_spider', 'chest_boat',
    'chest_minecart', 'chicken', 'cod', 'command_block_minecart', 'cow', 'creeper', 'dolphin',
    'donkey', 'dragon_fireball', 'drowned', 'egg', 'elder_guardian', 'end_crystal',
    'ender_dragon', 'ender_pearl', 'enderman', 'endermite', 'evoker', 'evoker_fangs',
    'experience_bottle', 'experience_orb', 'eye_of_ender', 'falling_block', 'firework_rocket',
    'fox', 'frog', 'furnace_minecart', 'ghast', 'giant', 'glow_item_frame', 'glow_squid',
    'goat', 'guardian', 'hoglin', 'hopper_minecart', 'horse', 'husk', 'illusioner',
    'interaction', 'iron_golem', 'item', 'item_display', 'item_frame', 'fireball', 'leash_knot',
    'lightning_bolt', 'llama', 'llama_spit', 'magma_cube', 'marker', 'minecart', 'mooshroom',
    'mule', 'ocelot', 'painting', 'panda', 'parrot', 'phantom', 'pig', 'piglin',
    'piglin_brute', 'pillager', 'polar_bear', 'potion', 'pufferfish', 'rabbit', 'ravager',
    'salmon', 'sheep', 'shulker', 'shulker_bullet', 'silverfish', 'skeleton',
    'skeleton_horse', 'slime', 'small_fireball', 'sniffer', 'snow_golem', 'snowball',
    'spawner_minecart', 'spectral_arrow', 'spider', 'squid', 'stray', 'strider', 'tadpole',
    'text_display', 'tnt', 'tnt_minecart', 'trader_llama', 'trident', 'tropical_fish',
    'turtle', 'vex', 'villager', 'vindicator', 'wandering_trader', 'warden', 'wind_charge',
    'witch', 'wither', 'wither_skeleton', 'wither_skull', 'wolf', 'zoglin', 'zombie',
    'zombie_horse', 'zombie_villager', 'zombified_piglin', 'player', 'fishing_bobber',
)

# Minecraft 1.20.4 原版 custom 统计项（杂项榜 criteria 用；来源同 blocks/items）
# 运行时还会并入玩家 stats 里出现过的项，覆盖模组统计。
CUSTOM_STATS_1204 = (
    'leave_game', 'play_time', 'total_world_time', 'time_since_death', 'time_since_rest',
    'sneak_time', 'walk_one_cm', 'crouch_one_cm', 'sprint_one_cm', 'walk_on_water_one_cm',
    'fall_one_cm', 'climb_one_cm', 'fly_one_cm', 'walk_under_water_one_cm', 'minecart_one_cm',
    'boat_one_cm', 'pig_one_cm', 'horse_one_cm', 'aviate_one_cm', 'swim_one_cm',
    'strider_one_cm', 'jump', 'drop', 'damage_dealt', 'damage_dealt_absorbed',
    'damage_dealt_resisted', 'damage_taken', 'damage_blocked_by_shield', 'damage_absorbed',
    'damage_resisted', 'deaths', 'mob_kills', 'animals_bred', 'player_kills', 'fish_caught',
    'talked_to_villager', 'traded_with_villager', 'eat_cake_slice', 'fill_cauldron',
    'use_cauldron', 'clean_armor', 'clean_banner', 'clean_shulker_box',
    'interact_with_brewingstand', 'interact_with_beacon', 'inspect_dropper', 'inspect_hopper',
    'inspect_dispenser', 'play_noteblock', 'tune_noteblock', 'pot_flower',
    'trigger_trapped_chest', 'open_enderchest', 'enchant_item', 'play_record',
    'interact_with_furnace', 'interact_with_crafting_table', 'open_chest', 'sleep_in_bed',
    'open_shulker_box', 'open_barrel', 'interact_with_blast_furnace', 'interact_with_smoker',
    'interact_with_lectern', 'interact_with_campfire', 'interact_with_cartography_table',
    'interact_with_loom', 'interact_with_stonecutter', 'bell_ring', 'raid_trigger',
    'raid_win', 'interact_with_anvil', 'interact_with_grindstone', 'target_hit',
    'interact_with_smithing_table',
)


def _board_specs() -> List[Dict[str, Any]]:
    """榜单的完整定义。含 cfg() 的字段在调用时求值，配置改动后重新生成即生效。"""
    return [
        {'id': BOARD_BREAK, 'index': 0, 'kind': 'b', 'stat': 'minecraft:mined',
         'objective': DP_OBJ_BREAK, 'total_objective': DP_OBJ_TOTAL_BREAK, 'display': DP_BOARD_BREAK,
         'title': cfg('break_title'), 'total_label': cfg('total_break_label'), 'prefix': 'dsb_b'},
        {'id': BOARD_PLACE, 'index': 1, 'kind': 'p', 'stat': 'minecraft:used',
         'objective': DP_OBJ_PLACE, 'total_objective': DP_OBJ_TOTAL_PLACE, 'display': DP_BOARD_PLACE,
         'title': cfg('place_title'), 'total_label': cfg('total_place_label'), 'prefix': 'dsb_p'},
        # 同榜：破坏、放置两个独立排行榜堆在一个侧边栏里（不相加）
        {'id': BOARD_BOTH, 'index': 2, 'kind': 'd', 'stat': 'minecraft:mined+used',
         'display': DP_BOARD_BOTH,
         'title': cfg('both_title'), 'prefix': 'dsb_d',
         'subs': [
             {'tag': 'b', 'objective': DP_OBJ_BREAK, 'total_objective': DP_OBJ_TOTAL_BREAK,
              'total_label': cfg('total_break_label'), 'row_prefix': 'dsb_dr', 'total_holder': 'dsb_dtb'},
             {'tag': 'p', 'objective': DP_OBJ_PLACE, 'total_objective': DP_OBJ_TOTAL_PLACE,
              'total_label': cfg('total_place_label'), 'row_prefix': 'dsb_dp', 'total_holder': 'dsb_dtp'},
         ]},
        # 原版其余统计分类（口径同 mined：sum(该分类所有项)）
        {'id': BOARD_CRAFT, 'index': 3, 'kind': 'k', 'stat': 'minecraft:crafted',
         'objective': DP_OBJ_CRAFT, 'total_objective': DP_OBJ_TOTAL_CRAFT, 'display': DP_BOARD_CRAFT,
         'title': cfg('craft_title'), 'total_label': cfg('total_craft_label'), 'prefix': 'dsb_c'},
        {'id': BOARD_DROP, 'index': 4, 'kind': 'o', 'stat': 'minecraft:dropped',
         'objective': DP_OBJ_DROP, 'total_objective': DP_OBJ_TOTAL_DROP, 'display': DP_BOARD_DROP,
         'title': cfg('drop_title'), 'total_label': cfg('total_drop_label'), 'prefix': 'dsb_o'},
        {'id': BOARD_PICK, 'index': 5, 'kind': 'i', 'stat': 'minecraft:picked_up',
         'objective': DP_OBJ_PICK, 'total_objective': DP_OBJ_TOTAL_PICK, 'display': DP_BOARD_PICK,
         'title': cfg('pick_title'), 'total_label': cfg('total_pick_label'), 'prefix': 'dsb_i'},
        {'id': BOARD_KILL, 'index': 6, 'kind': 'x', 'stat': 'minecraft:killed',
         'objective': DP_OBJ_KILL, 'total_objective': DP_OBJ_TOTAL_KILL, 'display': DP_BOARD_KILL,
         'title': cfg('kill_title'), 'total_label': cfg('total_kill_label'), 'prefix': 'dsb_k'},
        # custom：全部杂项统计相加（play_time/行走距离/次数等，数值会被里程和时间主导）
        {'id': BOARD_CUSTOM, 'index': 7, 'kind': 'j', 'stat': 'minecraft:custom',
         'objective': DP_OBJ_CUSTOM, 'total_objective': DP_OBJ_TOTAL_CUSTOM, 'display': DP_BOARD_CUSTOM,
         'title': cfg('custom_title'), 'total_label': cfg('total_custom_label'), 'prefix': 'dsb_j'},
    ]


def _board_spec(board_id: str) -> Dict[str, Any]:
    for spec in _board_specs():
        if spec['id'] == board_id:
            return spec
    return _board_specs()[0]


def _active_board() -> str:
    board = str(cfg('active_board') or BOARD_BREAK).lower()
    valid = (BOARD_BREAK, BOARD_PLACE, BOARD_BOTH,
             BOARD_CRAFT, BOARD_DROP, BOARD_PICK, BOARD_KILL, BOARD_CUSTOM)
    return board if board in valid else BOARD_BREAK


def _board_holders(prefix: str, rows: int) -> List[str]:
    """一个榜的全部假玩家：总和行 / 换行 / 名次行。"""
    names = [prefix + 't', prefix + 'x']
    names += [prefix + 'r{:02d}'.format(i) for i in range(1, rows + 1)]
    return names


def _board_all_holders(spec: Dict[str, Any]) -> List[str]:
    """一个榜用到的全部假玩家（init/切换时清理用）。"""
    if 'subs' in spec:
        names = []
        for sub in spec['subs']:
            names.append(sub['total_holder'])
            names += [sub['row_prefix'] + '{:02d}'.format(i) for i in range(1, MAX_BOTH_ROWS + 1)]
        return names
    return _board_holders(spec['prefix'], MAX_BOARD_ROWS)


def _board_extra_rows() -> int:
    """总和行 + 换行占掉的行数（按当前配置）。"""
    extra = 0
    if cfg('show_total_row'):
        extra += 1
    if cfg('show_blank_row'):
        extra += 1
    return extra


def _board_row_budget() -> int:
    """当前配置下每榜最多能放多少条玩家数据（侧边栏 15 行 - 额外行）。"""
    return max(1, SIDEBAR_RENDER_LIMIT - _board_extra_rows())


def _board_row_limit(player_count: int) -> int:
    """每榜最多显示的行数：受侧边栏 15 行上限和 board_top_n 限制。"""
    return max(0, min(int(cfg('board_top_n')), _board_row_budget(), player_count))


def _both_row_limit(player_count: int) -> int:
    """同榜（放置+破坏）里每个分榜的行数：2 个总和行 + 2*N <= 15。"""
    return max(0, min(int(cfg('both_top_n')), MAX_BOTH_ROWS, player_count))


# 名次颜色，对应参考插件 constants.rankColor = ['§b','§d','§e','§f']
RANK_COLORS = ['aqua', 'light_purple', 'yellow', 'white']

CONFIG_FILE = 'config.json'
UUID_FILE = 'uuid.json'
UUID_FILE_PREV = os.path.join('config', 'StatsHelper', 'uuid.json')   # 参考插件的映射文件，做兼容读取


# ===========================================================================
# 配置
# ===========================================================================

CONFIG_DEFAULT: Dict[str, Any] = {
    # ---- 服务端 / 世界路径 ----
    # server_path 留空 = 用 MCDR 的 working_directory
    'server_path': '',
    # world_folder 留空 = 自动从 server.properties 的 level-name 读
    'world_folder': '',

    # ---- 逐 tick 刷新 ----
    'tick_interval_ms': 50,          # 一次“tick”的间隔，50ms ≈ 原版 20 TPS
    'scan_interval_ticks': 1,        # 每多少 tick 扫一次文件 mtime（1 = 每 tick）
    # 原版 stats/*.json 只在 保存时 落盘（自动保存约 5 分钟一次 / 玩家退出 / save-all）。
    # 不主动 save-all 的话，读文件拿不到实时数据。0 = 不主动保存（延迟变分钟级）。
    'save_all_interval_seconds': 20,

    # ---- 实时方式 ----
    # poll     : 读 stats/*.json 算总和（延迟 = save_all_interval_seconds）
    # datapack : 服务端数据包逐 tick 统计、排序、显示；不依赖 RCON 或统计文件落盘
    'realtime_mode': 'datapack',
    'config_version': 3,
    'minecraft_version': '1.20.4',
    'datapack_registry_file': '',       # 可选：包含 block/item 的完整注册表，支持模组方块
    'datapack_extra_blocks': [],
    'datapack_extra_items': [],
    'datapack_dir': '',               # 留空 = <世界目录>/datapacks
    'datapack_pack_format': 0,        # 0 = 自动推断
    # 排除某些统计键（跨版本降级 / 移除了模组后，旧方块 id 会让准则项建失败）。
    # 写完整的 JSON 键，例如 ["minecraft:stone"]。
    'datapack_exclude_items': [],

    # ---- 榜单显示 ----
    # 默认显示哪个榜：break（破坏总和榜）| place（放置总和榜）| total（总榜）
    # 游戏中用 !!spms break / place / total 随时切换，!!spms hide 统一隐藏
    'active_board': 'break',
    # 每个榜显示前几名（配置文件项：board_top_n，改完 !!spms dp rebuild 生效）。
    # 侧边栏上限 15 行：总和行占 1 行（换行默认关），所以最多 14；
    # 打开换行自动降为 13，关闭总和行可到 15。默认 10。
    'board_top_n': 10,
    'hide_number_column': False,     # 已废弃：数值现在走原版分数列（右对齐），不再隐藏
    'break_title': '破坏总和榜',
    'place_title': '放置总和榜',
    'both_title': '放置/破坏榜',
    # 同榜（!!spms both）里每个分榜各显示几条：2 个总和行 + 2*N <= 15，最多 6
    'both_top_n': 6,
    # 原版其余统计分类的榜
    'craft_title': '合成榜',
    'drop_title': '丢弃榜',
    'pick_title': '捡起榜',
    'kill_title': '击杀榜',
    'custom_title': '综合榜',
    'total_craft_label': '总合成',
    'total_drop_label': '总丢弃',
    'total_pick_label': '总捡起',
    'total_kill_label': '总击杀',
    'total_custom_label': '总综合',
    # 新分类（合成/丢弃/捡起/击杀）的增量消费节奏：每 N 个 tick 批量一次。
    # 这四类的准则项太多（每物品/每实体一项），逐 tick 跑会把指令量涨 3 倍；
    # 5 = 250ms 延迟，侧边栏上无感知。1 = 逐 tick（指令量最大）。
    'extra_scan_interval_ticks': 5,
    'show_rank_number': True,        # 行内是否带 #1 #2 名次
    # 侧边栏顶部的全服总和行（所有玩家的分数之和）
    'show_total_row': True,
    # 总和行与排行榜之间的换行（空行）；默认关闭，打开就有空行
    'show_blank_row': False,
    'total_break_label': '总破坏',
    'total_place_label': '总放置',
    'total_label': '总计数',
    # 所有统计数字的千分位分隔符；留空 = 不加分隔（默认）。
    # 想恢复逗号就填 ","（也支持 " " / "." 之类）
    'thousands_separator': '',
    'title_color': 'gold',
    'name_color': 'white',
    'value_color': 'aqua',

    # ---- 玩家过滤 ----
    # 规格：排除所有以 bot_ 开头的玩家（可用游戏内 !!spms botboard on 切换到纯假人榜）
    'bot_name_prefixes': ['bot_'],
    # 额外的正则黑名单（参考插件里是 ['^bot.*','Steve','Alex']，这里默认留空，按规格只用 bot_ 前缀）
    'bot_name_patterns': [],
    # 纯假人榜单：打开后所有榜单只上榜 bot_ 假人；!!spms botboard on/off 切换
    'bot_only_mode': False,

    # ---- 其他 ----
    'auto_show': True,               # 插件加载后自动开榜
    # 侧边栏是否显示（启动时的默认状态）；!!spms hide / break|place|total|show 会记住到这里
    'sidebar_visible': True,
    'refresh_uuid_interval_seconds': 30,   # uuid/玩家名映射的重扫间隔
    # 如果玩家 JSON 里已经有一个“放置总和”字段，优先读它，例如：
    #   "minecraft:custom.minecraft:placed_total"    (点号分隔的 JSON 路径，键本身不含点)
    # 留空 = 按 used ∩ mined 现算
    'placed_precomputed_path': '',
    'debug_log': False,
}

_config: Dict[str, Any] = dict(CONFIG_DEFAULT)


def cfg(key: str) -> Any:
    value = _config.get(key, CONFIG_DEFAULT[key])
    return CONFIG_DEFAULT[key] if value is None else value


def load_config(si: PluginServerInterface):
    global _config
    _config = dict(CONFIG_DEFAULT)
    path = os.path.join(os.path.abspath(si.get_data_folder()), CONFIG_FILE)
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in CONFIG_DEFAULT:
                        _config[k] = v
                if data.get('config_version', 1) < 2:
                    _config['realtime_mode'] = 'datapack'
                    _config['config_version'] = 2
                    save_config(si)
                    si.logger.info('[spms] 已迁移旧配置，启用服务端逐 tick 实时模式')
                if _config['config_version'] < 3:
                    # 1.4.0：三榜分开显示；旧配置里的 sidebar_layout / sidebar_title 等键已废弃
                    _config['config_version'] = 3
                    save_config(si)
                    si.logger.info('[spms] 已迁移配置到 v3：破坏/放置/总榜分开显示，'
                                   '用 !!spms break|place|total 切换，!!spms hide 隐藏')
                return
    except Exception as e:
        si.logger.warning('[spms] 读取配置失败，使用默认值: {}'.format(e))
    save_config(si)


def save_config(si: PluginServerInterface):
    path = os.path.join(os.path.abspath(si.get_data_folder()), CONFIG_FILE)
    try:
        _ensure_parent_dir(path)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(_config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        si.logger.warning('[spms] 写入配置失败: {}'.format(e))


# ===========================================================================
# 全局状态
# ===========================================================================

server: Optional[PluginServerInterface] = None
PLUGIN_ID = PLUGIN_METADATA['id']

uuid_list: Dict[str, str] = {}        # 玩家名 -> uuid（复用参考插件的数据结构）
_states: Dict[str, 'PlayerState'] = {}   # 玩家名 -> 榜单状态
_online: Set[str] = set()

_obj_ready = False                    # 计分项是否已建好
_last_render: Dict[str, Tuple[int, str]] = {}          # poll: holder -> (score, component_json)
_last_scores: Dict[str, Dict[str, int]] = {}           # poll : obj -> {name: value}（兼容旧字段）
_active_layout = 'board'              # 1.4.0 起只有「单榜」布局；保留字段兼容旧诊断输出
_last_save_all = 0.0
_last_uuid_refresh = 0.0
_tick_count = 0
_rendered_once = False
_boards_started = False        # 首次建榜是否已经做过了（要等服务端就绪）

_side_visible = True

_stop_event = threading.Event()
_start_done = threading.Event()
_wake_event = threading.Event()
# 串行化 tick：后台线程和 !!spms refresh / dp rebuild 可能同时跑，两边都在做
# 「读文件 -> 和上次快照比 -> 累加 -> 覆盖快照」，并发会重复累加。用可重入锁兜住。
_tick_lock = threading.RLock()
_tick_thread: Optional[threading.Thread] = None
_lock = threading.RLock()
_unloaded = False

# 统计计数（debug 用）
_stat_scans = 0
_stat_reads = 0
_stat_renders = 0


class PlayerState(object):
    """历史初始化快照；实时模式的当前值由服务端计分板持有。"""

    __slots__ = ('name', 'uuid', 'used', 'mined', 'placed', 'break_total', 'baseline',
                 'stamp', 'loaded', 'craft_total', 'drop_total', 'pick_total', 'kill_total',
                 'custom_total', 'kill_entities', 'custom_stats')

    def __init__(self, name: str, uuid: str):
        self.name = name
        self.uuid = uuid
        self.used: Dict[str, int] = {}        # 上一次的 used 快照（整份，不只交集）
        self.mined: Dict[str, int] = {}       # 上一次的 mined 快照（整份，用于回填准则项）
        self.placed: int = 0                  # 放置总和 = 初始值 + 逐 tick 累计的 used 新增
        self.break_total: int = 0             # 破坏总和 = sum(mined.values())
        # 其余原版统计分类的历史总和（初始值来自 JSON，实时值由服务端增量维护）
        self.craft_total: int = 0             # sum(minecraft:crafted)
        self.drop_total: int = 0              # sum(minecraft:dropped)
        self.pick_total: int = 0              # sum(minecraft:picked_up)
        self.kill_total: int = 0              # sum(minecraft:killed)
        self.custom_total: int = 0            # sum(minecraft:custom 所有项)
        self.kill_entities: Set[str] = set()  # 该玩家 killed 里出现过的实体 id（并入 criteria）
        self.custom_stats: Set[str] = set()   # 该玩家 custom 里出现过的统计 id（并入 criteria）
        # 保留旧状态字段以兼容 poll 模式；实时模式改用服务端增量与“曾挖过”标记。
        self.baseline: int = 0
        self.stamp: Optional[Tuple[int, int]] = None   # (mtime_ns, size)，用于变化检测
        self.loaded: bool = False             # 是否成功读过一次文件


# ===========================================================================
# 路径（参考插件：config.server_path + config.world_folder + 'stats'）
# ===========================================================================

def mcdr_working_dir() -> str:
    if server is None:
        return os.path.abspath('.')
    configured = str(cfg('server_path') or '').strip()
    if configured:
        return os.path.abspath(configured)
    try:
        mcdr_config = server.get_mcdr_config()
        working_dir = mcdr_config.get('working_directory', 'server')
    except Exception:
        working_dir = 'server'
    return os.path.abspath(working_dir)


def world_dir() -> str:
    """世界目录；world_folder 留空则读 server.properties 的 level-name（参考插件用固定值）"""
    working_dir = mcdr_working_dir()
    folder = str(cfg('world_folder') or '').strip()
    if not folder:
        folder = 'world'
        prop = os.path.join(working_dir, 'server.properties')
        try:
            if os.path.isfile(prop):
                with open(prop, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('level-name'):
                            value = line.split('=', 1)[1].strip()
                            if value:
                                folder = value
                            break
        except Exception as e:
            if server is not None:
                server.logger.warning('[spms] 读取 server.properties 失败: {}'.format(e))
    return os.path.join(working_dir, folder)


def stats_dir() -> str:
    return os.path.join(world_dir(), 'stats')


def stats_path(uuid: str) -> str:
    return os.path.join(stats_dir(), uuid + '.json')


def _ensure_parent_dir(path: str):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)


def data_file(name: str) -> str:
    base = os.path.abspath(server.get_data_folder()) if server is not None else os.path.abspath('.')
    return os.path.join(base, name)


# ===========================================================================
# 玩家名 / UUID 映射 —— 逻辑复用参考插件 refresh_uuid_list()
#   1) <plugins>/StatsHelper/uuid.json（旧位置，兼容）
#   2) config/StatsHelper/uuid.json（参考插件的映射文件）
#   3) <服务端目录>/usercache.json（带 expiresOn 的新旧比较）
#   4) 本插件自己的 uuid.json
# ===========================================================================

def _load_json_dict(path: str) -> Optional[dict]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def refresh_uuid_list(si: PluginServerInterface):
    """复用参考插件 StatsHelper 的 uuid 映射逻辑（uuid.json + usercache.json）"""
    global uuid_list, _last_uuid_refresh

    mapping: Dict[str, str] = {}

    # 兼容：参考插件放在 config/StatsHelper/uuid.json
    if os.path.isfile(UUID_FILE_PREV):
        data = _load_json_dict(UUID_FILE_PREV)
        if data:
            mapping.update({k: v for k, v in data.items() if isinstance(v, str)})

    # 本插件自己的缓存
    own = data_file(UUID_FILE)
    if os.path.isfile(own):
        data = _load_json_dict(own)
        if data:
            mapping.update({k: v for k, v in data.items() if isinstance(v, str)})

    # usercache.json：同名多条时取 expiresOn 最新的那条（和参考插件一致）
    uuid_cache: Dict[str, str] = {}
    uuid_cache_time: Dict[str, Any] = {}
    usercache = os.path.join(mcdr_working_dir(), 'usercache.json')
    if os.path.isfile(usercache):
        try:
            with open(usercache, 'r', encoding='utf-8') as f:
                for item in json.load(f):
                    player, uuid = item['name'], item['uuid']
                    try:
                        expired_time = time.strptime(item['expiresOn'].rsplit(' ', 1)[0], '%Y-%m-%d %X')
                    except Exception:
                        expired_time = time.gmtime(0)
                    if player not in uuid_cache or expired_time > uuid_cache_time.get(player, time.gmtime(0)):
                        uuid_cache[player] = uuid
                        uuid_cache_time[player] = expired_time
        except Exception as e:
            si.logger.debug('[spms] 解析 usercache.json 失败: {}'.format(e))

    mapping.update(uuid_cache)

    with _lock:
        uuid_list = mapping
    _last_uuid_refresh = time.monotonic()
    _save_uuid_list(si)


def _save_uuid_list(si: PluginServerInterface):
    path = data_file(UUID_FILE)
    try:
        _ensure_parent_dir(path)
        with _lock:
            data = dict(uuid_list)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        si.logger.debug('[spms] 写入 uuid 缓存失败: {}'.format(e))


# ===========================================================================
# bot 过滤
#   规格：排除所有玩家名以 bot_ 开头的数据。
#   注意：这里刻意不使用参考插件 utils.isBot() 里的“名字长度 <4 或 >16 也算 bot”启发式，
#         那个规则会把 Bob / Ann 这种正常短名误判掉。
#   bot_only_mode = True 时（游戏内 !!spms botboard on）反过来：所有榜单只上榜假人，
#   方便给 carpet 假人单独开榜/刷数据。
# ===========================================================================

def is_bot(name: str) -> bool:
    lowered = (name or '').lower()
    for prefix in cfg('bot_name_prefixes') or []:
        if prefix and lowered.startswith(str(prefix).lower()):
            return True
    for pattern in cfg('bot_name_patterns') or []:
        try:
            if re.fullmatch(pattern, name, re.IGNORECASE):
                return True
        except re.error:
            pass
    return False


def known_players() -> List[Tuple[str, str]]:
    """[(玩家名, uuid)]；默认排除假人；bot_only_mode 下只保留假人。"""
    with _lock:
        items = list(uuid_list.items())
    if cfg('bot_only_mode'):
        return [(name, uuid) for name, uuid in items if is_bot(name)]
    return [(name, uuid) for name, uuid in items if not is_bot(name)]


# ===========================================================================
# 统计文件读取
#
#   get_stat_data() 是参考插件 stats_helper/utils.py::get_stat_data 的原样实现，
#   包括 '#all' -> sum(target_data.values()) 这条求和逻辑。
# ===========================================================================

def get_stat_data(uuid: str, cls: str, target: str) -> Optional[int]:
    """原样复用参考插件 StatsHelper/stats_helper/utils.py::get_stat_data"""
    try:
        with open(stats_path(uuid), 'r', encoding='utf-8') as f:
            target_data = json.load(f)['stats']['minecraft:' + cls]
            if target == ALL_TAG:
                return sum(target_data.values())
            return target_data['minecraft:' + target]
    except Exception:
        return None


def read_stats(si: PluginServerInterface, uuid: str) -> Optional[Dict[str, Any]]:
    """读一次文件，返回 stats 字典；失败返回 None（调用方保持旧缓存，下个 tick 重试）"""
    global _stat_reads
    try:
        with open(stats_path(uuid), 'r', encoding='utf-8') as f:
            data = json.load(f)
        stats = data.get('stats')
        if not isinstance(stats, dict):
            return None
        _stat_reads += 1
        return stats
    except FileNotFoundError:
        return None
    except Exception as e:
        # 文件正在被服务端写入 -> 半截 JSON。不要更新 stamp，下个 tick 会重试。
        if server is not None and cfg('debug_log'):
            server.logger.debug('[spms] 解析 {} 失败（下个 tick 重试）: {}'.format(uuid, e))
        return None


def stat_class_sum(stats: Dict[str, Any], cls: str) -> int:
    """
    参考插件 #all 的求和逻辑，抽出来以复用：
        target_data = json['stats']['minecraft:' + cls]
        return sum(target_data.values())
    破坏榜就是把 cls 换成 'mined'。
    """
    target_data = stats.get('minecraft:' + cls)
    if not isinstance(target_data, dict):
        return 0
    return sum(v for v in target_data.values() if isinstance(v, int))


def stat_class_map(stats: Dict[str, Any], cls: str) -> Dict[str, int]:
    target_data = stats.get('minecraft:' + cls)
    if not isinstance(target_data, dict):
        return {}
    return {k: v for k, v in target_data.items() if isinstance(v, int)}


# ===========================================================================
# 放置榜：初始值
# ===========================================================================

def read_precomputed_placed(stats: Dict[str, Any]) -> Optional[int]:
    """
    规格第 4 条：如果 JSON 里已有现成的 placed 总和字段，优先读它。
    路径由配置 placed_precomputed_path 指定，点号分隔（JSON 键本身不含点），
    例如 "minecraft:custom.minecraft:placed_total"。留空则不做这一步。
    """
    path = str(cfg('placed_precomputed_path') or '').strip()
    if not path:
        return None
    node: Any = stats
    for part in path.split('.'):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, bool):
        return None
    if isinstance(node, int):
        return node
    if isinstance(node, float):
        return int(node)
    return None


def initial_placed_total(stats: Dict[str, Any], used: Dict[str, int], mined: Dict[str, int]) -> int:
    """
    规格：
      1. 读该玩家的 used / mined
      2. 目标集合 = 该玩家自己的 used.keys() ∩ mined.keys()   <- 各自交集
      3. 初始 placed = used 中这些目标项的计数总和
      4. JSON 里已有现成 placed 总和字段则优先用它
    """
    precomputed = read_precomputed_placed(stats)
    if precomputed is not None:
        return precomputed
    targets = set(used.keys()) & set(mined.keys())
    return sum(used[item] for item in targets)


# ===========================================================================
# 放置榜：逐 tick 增量累计
# ===========================================================================

def accumulate_placed(state: 'PlayerState', used: Dict[str, int], mined: Dict[str, int]) -> int:
    """
    ★ 放置榜新增逻辑的核心（README 第 6.2 节会指到这里）

    目标集合 = 当前 used.keys() ∩ 当前 mined.keys()（各自交集，每个玩家一份）
    对目标集合里的每个物品/类型：
        当前 used[item] > 上次 used[item]  ->  placed += 当前 - 上次
        只按 used 的新增数量累计，增加多少加多少；没增加就不加。

    为什么“新进入交集的物品不会被补计历史数量”：
        下面的快照 state.used 保存的是【整份 used】，不是只保存交集内的项。
        所以某个物品从“不在交集”变成“在交集”时，它的上次值仍然是真实的历史值，
        prev（真实历史值）和 cur（当前值）相等 -> 增量 0 -> 不补计。
        只有当它真的又被使用/放置了，cur 才会大于 prev，才会被计入。
    """
    targets = set(used.keys()) & set(mined.keys())
    added = 0
    for item in targets:
        cur = used[item]
        prev = state.used.get(item, 0)
        if cur > prev:
            added += cur - prev
    return added


# ===========================================================================
# 服务端逐 tick 统计与渲染（MC 1.20.4 / Fabric，无须附加模组或 RCON）
# ===========================================================================

DP_NAME = 'sidebar_boards'

SCORE_MIN = -(2 ** 31)
SCORE_MAX = 2 ** 31 - 1

_dp_index: Dict[str, int] = {}
_dp_next_slot = 0
_dp_slugs: Dict[str, str] = {}
_dp_created_m: Set[str] = set()
_dp_created_u: Set[str] = set()
_dp_blocks: Set[str] = set()
_dp_items: Set[str] = set()
_dp_players: Set[str] = set()
_dp_seeded = False
_dp_existing_runtime = False
_dp_last_error = ''
_dp_save_done = threading.Event()

# Minecraft 1.20.4 原版注册表，压缩保存以维持单文件插件、运行时无需下载依赖。
# 来源：https://github.com/misode/mcmeta/tree/1.20.4-summary/registries
_REGISTRY_1204 = (
    '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'
    'AlVTy9gWt8EUu1D+/X//D2z8Pow='
)


def _dp_grouping() -> str:
    """千分位分隔符；返回空字符串表示不做分组。"""
    return str(cfg('thousands_separator') if cfg('thousands_separator') is not None else '')


def _dp_mode() -> bool:
    return str(cfg('realtime_mode')).lower() == 'datapack'


def dp_can_read() -> bool:
    """RCON 仅用于诊断；实时统计、排序和显示均不依赖它。"""
    return server is not None and bool(server.is_rcon_running())


def _server_mc_version() -> str:
    if server is not None:
        info = server.get_server_information()
        for attr in ('mc_version', 'version'):
            value = getattr(info, attr, None)
            if value:
                match = re.search(r'\d+\.\d+(?:\.\d+)?', str(value))
                if match:
                    return match.group(0)
    return str(cfg('minecraft_version')).strip()


def _mc_version_tuple() -> Tuple[int, int, int]:
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.(\d+))?', _server_mc_version())
    if not match:
        raise ValueError('无法识别 Minecraft 版本，请配置 minecraft_version')
    return tuple(int(v or 0) for v in match.groups())


def dp_root() -> str:
    base = str(cfg('datapack_dir') or '').strip() or os.path.join(world_dir(), 'datapacks')
    return os.path.join(base, DP_NAME)


def _dp_write(path: str, content: str):
    _ensure_parent_dir(path)
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)
    os.replace(temporary, path)


def _dp_function_folder() -> str:
    return 'function' if _mc_version_tuple() >= (1, 21, 0) else 'functions'


def dp_write_file(rel: str, content: str):
    _dp_write(os.path.join(dp_root(), 'data', DP_NAME, _dp_function_folder(), *rel.split('/')), content)


def dp_write_tag(rel: str, content: str):
    _dp_write(os.path.join(dp_root(), 'data', 'minecraft', 'tags', _dp_function_folder(), rel), content)


def dp_pack_format() -> int:
    configured = int(cfg('datapack_pack_format') or 0)
    if configured > 0:
        return configured
    formats = [((1, 21, 9), 88), ((1, 21, 7), 81), ((1, 21, 6), 80),
               ((1, 21, 5), 71), ((1, 21, 4), 61), ((1, 21, 2), 57),
               ((1, 21, 0), 48), ((1, 20, 5), 41), ((1, 20, 3), 26),
               ((1, 20, 2), 18), ((1, 20, 0), 15), ((1, 19, 4), 13)]
    for bound, value in formats:
        if _mc_version_tuple() >= bound:
            return value
    raise ValueError('此版本需要显式配置 datapack_pack_format')


def dp_write_pack_meta():
    fmt = dp_pack_format()
    pack = {'description': '简单放置破坏榜: 每 tick 统计、排序与侧边栏显示'}
    if fmt >= 82:
        pack.update(min_format=fmt, max_format=fmt)
    else:
        pack['pack_format'] = fmt
    _dp_write(os.path.join(dp_root(), 'pack.mcmeta'), json.dumps({'pack': pack}, ensure_ascii=False, indent=2) + '\n')


def dp_load_index():
    global _dp_index, _dp_next_slot, _dp_existing_runtime
    data = _load_json_dict(data_file(DP_INDEX_FILE)) or {}
    index = data.get('items', {})
    if isinstance(index, dict):
        _dp_index = {k: v for k, v in index.items()
                     if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
        if len(set(_dp_index.values())) != len(_dp_index):
            raise ValueError('datapack_index.json 中存在重复槽位；请备份后修复索引')
    _dp_next_slot = max(_dp_index.values(), default=-1) + 1
    _dp_existing_runtime = data.get('runtime_schema') == DP_SCHEMA


def dp_save_index():
    data = {'runtime_schema': DP_SCHEMA, 'items': _dp_index}
    _dp_write(data_file(DP_INDEX_FILE), json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def dp_slot(item: str) -> int:
    global _dp_next_slot
    if item not in _dp_index:
        _dp_index[item] = _dp_next_slot
        _dp_next_slot += 1
    return _dp_index[item]


def dp_slug(name: str) -> str:
    slug = hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]
    _dp_slugs[name] = slug
    return slug


def _registry_ids(data: dict, key: str) -> Set[str]:
    values = data.get(key, data.get('minecraft:' + key))
    if isinstance(values, dict):
        values = values.get('entries', values)
    if not isinstance(values, (list, dict)):
        raise ValueError('注册表缺少 {} 列表或 entries 字典'.format(key))
    result = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError('注册表 ID 必须是字符串')
        value = value if ':' in value else 'minecraft:' + value
        if not re.fullmatch(r'[a-z0-9_.-]+:[a-z0-9_./-]+', value):
            raise ValueError('非法注册表 ID: {}'.format(value))
        result.add(value)
    return result


def dp_load_registry():
    global _dp_blocks, _dp_items
    path = str(cfg('datapack_registry_file') or '').strip()
    if path:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    elif _mc_version_tuple() == (1, 20, 4):
        data = json.loads(zlib.decompress(base64.b64decode(_REGISTRY_1204)).decode('utf-8'))
    else:
        raise ValueError('内置注册表针对 Minecraft 1.20.4；其他版本请配置 datapack_registry_file')
    _dp_blocks = _registry_ids(data, 'block')
    _dp_items = _registry_ids(data, 'item')
    _dp_blocks |= _registry_ids({'block': cfg('datapack_extra_blocks') or []}, 'block')
    _dp_items |= _registry_ids({'item': cfg('datapack_extra_items') or []}, 'item')
    excluded = set(cfg('datapack_exclude_items') or [])
    _dp_blocks -= excluded
    _dp_items -= excluded


_unsafe_names_warned: Set[str] = set()


def _warn_unsafe_name(name: str):
    """玩家名不能安全用于原版命令时告警一次（不刷屏、不抛异常）。"""
    if name in _unsafe_names_warned:
        return
    _unsafe_names_warned.add(name)
    if server is not None:
        server.logger.warning('[spms] 玩家名含不安全字符或过长，已跳过: {!r}'.format(name))


def dp_collect() -> Dict[str, Dict[str, Set[str]]]:
    # 不再由每位玩家的历史交集决定生成哪些函数；完整注册表覆盖首次出现的方块。
    names = set(name for name, _ in known_players()) | set(_online)
    result = {}
    # 过滤口径必须和 known_players 一致：默认排除假人；bot_only_mode 下只保留假人。
    # （以前这里无条件排除假人，导致纯假人模式下 known_players 返回的假人又被过滤掉，
    #   只剩 | _online 里的在线真人——榜单就变成只统计真人。）
    bot_only = bool(cfg('bot_only_mode'))
    for name in sorted(names):
        if is_bot(name) != bot_only:
            continue
        # 只校验“命令安全”字符，不套用原版玩家名的 16 位上限：carpet 假人名可以更长
        # （如 bot_danillaurora520 是 19 位），计分板 holder 本身没有 16 位限制。
        if not re.fullmatch(r'[A-Za-z0-9_.\-]{1,64}', name):
            # 跳过并告警，不抛异常：抛异常会连带整个 tick 循环一起崩（榜单彻底停更）
            _warn_unsafe_name(name)
            continue
        state = _states.get(name)
        mined = set(state.mined) if state else set()
        used = set(state.used) if state else set()
        result[name] = {'mined': mined, 'targets': mined & used}
    return result


def dp_needed_items(info_map: Dict[str, Dict[str, Set[str]]]) -> Set[str]:
    return set(_dp_blocks)


def _dp_score(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= SCORE_MAX:
        raise ValueError('统计总和超出原版计分板非负 32 位整数范围: {!r}'.format(value))
    return value


def dp_write_functions(info_map: Dict[str, Dict[str, Set[str]]], needed: Set[str]):
    """生成真实的增量计数、历史导入、完整排名和服务端宏渲染。"""
    mined = sorted(needed)
    used = sorted(set(mined) & _dp_items)
    used_set = set(used)
    names = sorted(info_map)
    limit = _board_row_limit(len(names))
    both_limit = _both_row_limit(len(names))
    active = _board_spec(_active_board())
    specs = _board_specs()

    # ---- 新增的统计分类：准则项 -> 每玩家总分 ----
    # crafted/dropped/picked_up 走物品注册表；killed 走实体注册表；custom 走统计项清单
    # （都会并入玩家 stats 里出现过的 id，覆盖模组内容）
    item_criteria = sorted(_dp_items)
    kill_entities = set(ENTITIES_1204)
    custom_stats = set(CUSTOM_STATS_1204)
    for state in _states.values():
        kill_entities |= set(state.kill_entities or set())
        custom_stats |= set(state.custom_stats or set())
    # 统一补上 minecraft: 命名空间（stats 里带来的可能带其他命名空间，保持原样）
    kill_entities = sorted(e if ':' in e else 'minecraft:' + e for e in kill_entities)
    custom_stats = sorted(s if ':' in s else 'minecraft:' + s for s in custom_stats)
    extra_classes = (
        ('k', CRAFT_CLASS, item_criteria, DP_OBJ_CRAFT),
        ('o', DROP_CLASS, item_criteria, DP_OBJ_DROP),
        ('i', PICK_CLASS, item_criteria, DP_OBJ_PICK),
        ('x', KILLED_CLASS, kill_entities, DP_OBJ_KILL),
        ('j', CUSTOM_CLASS, custom_stats, DP_OBJ_CUSTOM),
    )
    extra_interval = max(1, int(cfg('extra_scan_interval_ticks') or 1))

    # ---- load.mcfunction：先建项，再调用 init 做幂等初始化 ----
    # 兼容说明：目标已存在时「add」会失败并中断本函数（原版函数语义），此时 init 由
    # tick.mcfunction 的 #ready 门控在 reload 后的下一个 tick 补跑，幂等安全。
    load = []
    dummy = [DP_OBJ_BREAK, DP_OBJ_PLACE,
             DP_OBJ_CRAFT, DP_OBJ_DROP, DP_OBJ_PICK, DP_OBJ_KILL, DP_OBJ_CUSTOM,
             DP_OBJ_TOTAL_BREAK, DP_OBJ_TOTAL_PLACE,
             DP_OBJ_TOTAL_CRAFT, DP_OBJ_TOTAL_DROP, DP_OBJ_TOTAL_PICK,
             DP_OBJ_TOTAL_KILL, DP_OBJ_TOTAL_CUSTOM,
             DP_OBJ_FMT, 'dsb_init', 'dsb_ctl', 'dsb_work', 'dsb_owner', 'dsb_value'] + \
            [spec['display'] for spec in specs]
    for obj in dummy:
        load.append('scoreboard objectives add {} dummy'.format(obj))
    if _dp_grouping():
        # 千分位格式化器的常数（#g1/g2/g3 每次调用时算）
        load.append('scoreboard players set #1000 {} 1000'.format(DP_OBJ_FMT))
        load.append('scoreboard players set #1000000 {} 1000000'.format(DP_OBJ_FMT))
        _dp_write_number_format()
    for item in mined:
        slot = dp_slot(item)
        load.append('scoreboard objectives add dsbm{} minecraft.mined:{}'.format(slot, item.replace(':', '.')))
    for item in used:
        slot = dp_slot(item)
        load.append('scoreboard objectives add dsbu{} minecraft.used:{}'.format(slot, item.replace(':', '.')))
        load.append('scoreboard objectives add dsbh{} dummy'.format(slot))
    # 新分类的准则项（k=crafted / o=dropped / i=picked_up / x=killed / j=custom）
    for tag, _cls, items, _obj in extra_classes:
        for item in items:
            slot = dp_slot(item)
            load.append('scoreboard objectives add dsb{}{} minecraft.{}:{}'.format(
                tag, slot, _cls, item.replace(':', '.')))
    load.append('function {}:init'.format(DP_NAME))

    # ---- init.mcfunction：全部幂等；load 结尾与每次插件 reload 后都会跑到 ----
    init = []
    for spec in specs:
        disp = spec['display']
        init.append('scoreboard objectives modify {} displayname {}'.format(disp, _title_component(spec['title'])))
        if 'subs' in spec:
            # 同榜用名次分排序，分数列要藏起来（数值写进显示名）
            init.append('scoreboard objectives modify {} numberformat blank'.format(disp))
        else:
            # 数值走原版分数列（右对齐）；styled 会覆盖旧版遗留的 numberformat blank
            init.append('scoreboard objectives modify {} numberformat styled {}'.format(
                disp, _json_component({'color': cfg('value_color')})))
    # 占位行全部清空（render 会重写当前榜的全部行）
    for spec in specs:
        for holder in _board_all_holders(spec):
            init.append('scoreboard players reset {} {}'.format(holder, spec['display']))
    # 历史导入（dsb_init 守卫：同一 schema 只做一次）
    for name in names:
        init.append('execute unless score {} dsb_init matches {} run function {}:{}'.format(
            name, DP_SCHEMA, DP_NAME, 'seed/' + dp_slug(name)))
    # 当前显示的榜 / 可见性：已设置过就保留（切换命令写的就是这些分数）
    init.append('execute unless score #board dsb_ctl matches 0..{} run scoreboard players set #board dsb_ctl {}'.format(
        len(specs) - 1, active['index']))
    init.append('execute unless score #visible dsb_ctl matches 0..1 run scoreboard players set #visible dsb_ctl {}'.format(int(_side_visible)))
    # #enabled 必须无条件置 1：插件卸载时会把它置 0 暂停 tick，
    # 若沿用 unless 守卫，热重载后会被当成“已设置”而永远停在 0（榜单停止更新）。
    init.append('scoreboard players set #enabled dsb_ctl 1')
    # 新分类批量消费倒计时（未设置/越界时才初始化）
    init.append('execute unless score #xcd dsb_ctl matches 0..{} run scoreboard players set #xcd dsb_ctl {}'.format(
        extra_interval - 1, extra_interval - 1))
    # 隐藏状态（sidebar_visible=false 或 !!spms hide）时不画、不占 display slot
    init.append('execute if score #visible dsb_ctl matches 1 run function {}:render'.format(DP_NAME))
    init.append('execute if score #visible dsb_ctl matches 1 run scoreboard objectives setdisplay sidebar {}'.format(active['display']))
    init.append('scoreboard players set #ready dsb_ctl {}'.format(DP_SCHEMA))
    dp_write_file('init.mcfunction', '\n'.join(init) + '\n')

    # ---- seed：历史导入。准则分数是“新增计数”，游戏 awardStat 会做加法，
    #      并非把历史统计绝对赋值。只回填汇总值与“曾挖过”标记。 ----
    extra_seed = ((DP_OBJ_CRAFT, 'craft_total'), (DP_OBJ_DROP, 'drop_total'),
                  (DP_OBJ_PICK, 'pick_total'), (DP_OBJ_KILL, 'kill_total'),
                  (DP_OBJ_CUSTOM, 'custom_total'))
    for name in names:
        state = _states.get(name)
        brk = _dp_score(state.break_total if state else 0)
        plc = _dp_score(state.placed if state else 0)
        # init=1 表示玩家已被原生 tick 捕获、尚未导入历史；保留这段先到的在线增量。
        seed = ['execute unless score {} dsb_init matches 1 run scoreboard players set {} {} 0'.format(name, name, DP_OBJ_BREAK),
                'execute unless score {} dsb_init matches 1 run scoreboard players set {} {} 0'.format(name, name, DP_OBJ_PLACE)]
        for obj, _attr in extra_seed:
            seed.append('execute unless score {} dsb_init matches 1 run scoreboard players set {} {} 0'.format(name, name, obj))
        seed += ['scoreboard players add {} {} {}'.format(name, DP_OBJ_BREAK, brk),
                 'scoreboard players add {} {} {}'.format(name, DP_OBJ_PLACE, plc)]
        for obj, attr in extra_seed:
            seed.append('scoreboard players add {} {} {}'.format(
                name, obj, _dp_score(getattr(state, attr) if state else 0)))
        for item in used:
            if state and state.mined.get(item, 0) > 0:
                seed.append('scoreboard players set {} dsbh{} 1'.format(name, dp_slot(item)))
        seed.append('scoreboard players set {} dsb_init {}'.format(name, DP_SCHEMA))
        dp_write_file('seed/' + dp_slug(name) + '.mcfunction', '\n'.join(seed) + '\n')

    new_player = ['scoreboard players set @s {} 0'.format(DP_OBJ_BREAK),
                  'scoreboard players set @s {} 0'.format(DP_OBJ_PLACE)]
    for obj, _attr in extra_seed:
        new_player.append('scoreboard players set @s {} 0'.format(obj))
    new_player += ['scoreboard players set @s dsb_init 1',
                   'scoreboard players set #dirty dsb_ctl 1']
    dp_write_file('new_player.mcfunction', '\n'.join(new_player) + '\n')

    # ---- update：逐 tick 消费准则增量；破坏/放置的每次变化同时累加总榜 ----
    update = ['scoreboard players set #dirty dsb_ctl 0',
              'execute as @a unless score @s dsb_init matches 1..{} run function {}:new_player'.format(DP_SCHEMA, DP_NAME)]
    for cls, items in (('m', mined), ('u', used)):
        for item in items:
            slot = dp_slot(item)
            objective = 'dsb{}{}'.format(cls, slot)
            rel = 'event/{}{}'.format(cls, slot)
            update.append('execute as @a[scores={{{}=1..}}] run function {}:{}'.format(objective, DP_NAME, rel))
            if cls == 'm':
                event = ['scoreboard players operation @s {} += @s {}'.format(DP_OBJ_BREAK, objective)]
                if item in used_set:
                    event.append('scoreboard players set @s dsbh{} 1'.format(slot))
            else:
                event = ['execute if score @s dsbh{} matches 1 run scoreboard players operation @s {} += @s {}'.format(
                    slot, DP_OBJ_PLACE, objective)]
            event += ['scoreboard players set @s {} 0'.format(objective), 'scoreboard players set #dirty dsb_ctl 1']
            dp_write_file(rel + '.mcfunction', '\n'.join(event) + '\n')
    update.append('execute if score #visible dsb_ctl matches 1 if score #dirty dsb_ctl matches 1 run function {}:render'.format(DP_NAME))

    # ---- consume_extra：批量消费新分类（crafted/dropped/picked_up/killed）的准则增量 ----
    # 这四类准则项有几千项，逐 tick 跑会把指令量涨 3 倍，所以按 extra_scan_interval_ticks
    # 批量消费（增量会在准则项里累积，不会丢）；侧边栏延迟 = interval * tick_interval_ms。
    consume = ['scoreboard players set #dirty dsb_ctl 0']
    for tag, _cls, items, total_obj in extra_classes:
        for item in items:
            slot = dp_slot(item)
            objective = 'dsb{}{}'.format(tag, slot)
            rel = 'event/{}{}'.format(tag, slot)
            consume.append('execute as @a[scores={{{}=1..}}] run function {}:{}'.format(objective, DP_NAME, rel))
            event = ['scoreboard players operation @s {} += @s {}'.format(total_obj, objective),
                     'scoreboard players set @s {} 0'.format(objective),
                     'scoreboard players set #dirty dsb_ctl 1']
            dp_write_file(rel + '.mcfunction', '\n'.join(event) + '\n')
    consume.append('execute if score #visible dsb_ctl matches 1 if score #dirty dsb_ctl matches 1 run function {}:render'.format(DP_NAME))
    dp_write_file('consume_extra.mcfunction', '\n'.join(consume) + '\n')

    # ---- 各榜的渲染函数（总和 + 换行 + 排行榜）----
    _dp_write_boards(names, limit, both_limit)

    dp_write_file('load.mcfunction', '\n'.join(load) + '\n')
    dp_write_file('update.mcfunction', '\n'.join(update) + '\n')
    # tick：init 门控 -> update（mined/used 逐 tick）-> consume_extra（新分类每 N tick）
    # #xcd 是批量倒计时，init 里初始化为 N-1
    tick_lines = [
        'execute unless score #ready dsb_ctl matches {} run function {}:init'.format(DP_SCHEMA, DP_NAME),
        'execute if score #enabled dsb_ctl matches 1 run function {}:update'.format(DP_NAME),
        'execute if score #enabled dsb_ctl matches 1 run scoreboard players remove #xcd dsb_ctl 1',
        'execute if score #enabled dsb_ctl matches 1 if score #xcd dsb_ctl matches ..0 run function {}:consume_extra'.format(DP_NAME),
        'execute if score #enabled dsb_ctl matches 1 if score #xcd dsb_ctl matches ..0 run scoreboard players set #xcd dsb_ctl {}'.format(extra_interval - 1),
    ]
    dp_write_file('tick.mcfunction', '\n'.join(tick_lines) + '\n')
    for tag in ('load', 'tick'):
        dp_write_tag(tag + '.json', json.dumps({'values': [DP_NAME + ':' + tag]}) + '\n')


_FORMAT_PAD = {3: '', 2: '0', 1: '00'}
_FORMAT_RANGE = {3: '100..', 2: '10..99', 1: '0..9'}


def _dp_write_number_format():
    """
    生成千分位格式化器：输入「#fmt <obj> = 数值」，输出「storage <插件>:fmt out = 字符串」。

    原版没有数字分组格式化，只能自己拼：
        g1 = n / 1000000          （高位组，可能为 0）
        g2 = (n / 1000) % 1000
        g3 = n % 1000
    补零交给宏模板里的字面 0（"0$(g3)" 就是补到三位），一共 13 个模板：
        3 组（n >= 1e6）：$(g1) SEP [$(g2)|0$(g2)|00$(g2)] SEP [$(g3)|0$(g3)|00$(g3)]
        2 组（1e3 <= n < 1e6）：$(g2) SEP [$(g3)|0$(g3)|00$(g3)]
        1 组（n < 1e3）：$(g3)
    """
    sep = _dp_grouping()
    for mid in (3, 2, 1):
        for low in (3, 2, 1):
            body = '$(g1)' + sep + _FORMAT_PAD[mid] + '$(g2)' + sep + _FORMAT_PAD[low] + '$(g3)'
            dp_write_file('fmt/g3_m{}_l{}.mcfunction'.format(mid, low),
                          '$data modify storage {}:fmt out set value "{}"\n'.format(DP_NAME, body))
    for low in (3, 2, 1):
        body = '$(g2)' + sep + _FORMAT_PAD[low] + '$(g3)'
        dp_write_file('fmt/g2_l{}.mcfunction'.format(low),
                      '$data modify storage {}:fmt out set value "{}"\n'.format(DP_NAME, body))
    dp_write_file('fmt/plain.mcfunction',
                  '$data modify storage {}:fmt out set value "$(g3)"\n'.format(DP_NAME))
    o = DP_OBJ_FMT
    lines = ['# 输入：#fmt {} = 数值；输出：storage {}:fmt out = 带千分位分隔符的字符串'.format(o, DP_NAME),
             'scoreboard players operation #g1 {o} = #fmt {o}'.format(o=o),
             'scoreboard players operation #g1 {o} /= #1000000 {o}'.format(o=o),
             'scoreboard players operation #g2 {o} = #fmt {o}'.format(o=o),
             'scoreboard players operation #g2 {o} /= #1000 {o}'.format(o=o),
             'scoreboard players operation #g2 {o} %= #1000 {o}'.format(o=o),
             'scoreboard players operation #g3 {o} = #fmt {o}'.format(o=o),
             'scoreboard players operation #g3 {o} %= #1000 {o}'.format(o=o)]
    for g in ('g1', 'g2', 'g3'):
        lines.append('execute store result storage {}:fmt in.{} int 1 run scoreboard players get #{} {}'.format(DP_NAME, g, g, o))
    for mid in (3, 2, 1):
        for low in (3, 2, 1):
            lines.append('execute if score #g1 {o} matches 1.. if score #g2 {o} matches {mr} if score #g3 {o} matches {lr} run function {p}:fmt/g3_m{mid}_l{low} with storage {p}:fmt in'.format(
                o=o, mr=_FORMAT_RANGE[mid], lr=_FORMAT_RANGE[low], p=DP_NAME, mid=mid, low=low))
    for low in (3, 2, 1):
        lines.append('execute if score #g1 {o} matches 0 if score #g2 {o} matches 1.. if score #g3 {o} matches {lr} run function {p}:fmt/g2_l{low} with storage {p}:fmt in'.format(
            o=o, lr=_FORMAT_RANGE[low], p=DP_NAME, low=low))
    lines.append('execute if score #g1 {o} matches 0 if score #g2 {o} matches 0 run function {p}:fmt/plain with storage {p}:fmt in'.format(o=o, p=DP_NAME))
    dp_write_file('fmt/format.mcfunction', '\n'.join(lines) + '\n')


def _dp_write_both_board(spec: Dict[str, Any], names: List[str], limit: int):
    """
    同榜（!!spms both）：破坏、放置两个独立排行榜堆在同一个侧边栏里，互不相加。

    视觉顺序（自上而下）：
        破坏总和行 -> 破坏 #1..#N -> 放置总和行 -> 放置 #1..#N
    这里数值写进显示名（最老版本的做法），用「名次分」控制顺序：
        破坏总和行 2N+2，破坏 #r = 2N+2-r；放置总和行 N+1，放置 #r = N+1-r
    分数列对同榜是 blank（否则会露出名次分）。
    """
    disp = spec['display']
    grouped = bool(_dp_grouping())

    def value_lines(source: str, target: str) -> List[str]:
        if not grouped:
            return ['execute store result storage {}:render {} int 1 run scoreboard players get {}'.format(DP_NAME, target, source)]
        return ['scoreboard players operation #fmt {} = {}'.format(DP_OBJ_FMT, source),
                'function {}:fmt/format'.format(DP_NAME),
                'data modify storage {}:render {} set from storage {}:fmt out'.format(DP_NAME, target, DP_NAME)]

    render = []
    for holder in _board_all_holders(spec):
        render.append('scoreboard players reset {} {}'.format(holder, disp))

    for sub_index, sub in enumerate(spec['subs']):
        tag = sub['tag']
        objective = sub['objective']
        total_obj = sub['total_objective']
        row_prefix = sub['row_prefix']
        total_holder = sub['total_holder']
        base = (2 * limit + 2) if sub_index == 0 else (limit + 1)   # 该分榜总和行的名次分
        # 全服合计
        render.append('scoreboard players set #total {} 0'.format(total_obj))
        for name in names:
            render.append('scoreboard players operation #total {} += {} {}'.format(total_obj, name, objective))
        # 排名
        for name in names:
            render.append('scoreboard players operation {} dsb_work = {} {}'.format(name, name, objective))
        for rank in range(1, limit + 1):
            holder = row_prefix + '{:02d}'.format(rank)
            render.append('scoreboard players set #best dsb_work {}'.format(SCORE_MIN))
            for name in names:
                render.append('scoreboard players operation #best dsb_work > {} dsb_work'.format(name))
            render.append('scoreboard players set #picked dsb_ctl 0')
            for player_id, name in enumerate(names, 1):
                rel = 'render/d_{}_{}_{}'.format(tag, rank, dp_slug(name))
                render.append('execute if score #best dsb_work matches {}.. if score #picked dsb_ctl matches 0 '
                              'if score {} dsb_work = #best dsb_work run function {}:{}'.format(
                                  SCORE_MIN + 1, name, DP_NAME, rel))
                chosen = ['scoreboard players set #picked dsb_ctl 1',
                          'scoreboard players set {} dsb_work {}'.format(name, SCORE_MIN),
                          # ⚠ ensure_ascii=False：Brigadier 的 NBT 字符串不接受 \uXXXX 转义
                          'data modify storage {}:render row set value {}'.format(
                              DP_NAME, json.dumps({'name': name}, ensure_ascii=False, separators=(',', ':')))]
                chosen += value_lines('{} {}'.format(name, objective), 'row.value')
                chosen.append('function {}:render/row_d_{}{} with storage {}:render row'.format(DP_NAME, tag, rank, DP_NAME))
                dp_write_file(rel + '.mcfunction', '\n'.join(chosen) + '\n')
            extra = []
            if cfg('show_rank_number'):
                extra.append({'text': '#{} '.format(rank), 'color': RANK_COLORS[min(rank - 1, len(RANK_COLORS) - 1)]})
            extra += [{'text': '$(name) ', 'color': cfg('name_color')},
                      {'text': '$(value)', 'color': cfg('value_color')}]
            row = ['scoreboard players set {} {} {}'.format(holder, disp, base - rank),
                   '$scoreboard players display name {} {} {}'.format(holder, disp, _json_component({'text': '', 'extra': extra}))]
            dp_write_file('render/row_d_{}{}.mcfunction'.format(tag, rank), '\n'.join(row) + '\n')
        # 总和行：label + 全服合计（数值同样在显示名里）
        render.append('data modify storage {}:render total set value {}'.format(
            DP_NAME, json.dumps({'label': str(sub['total_label'])}, ensure_ascii=False, separators=(',', ':'))))
        render += value_lines('#total {}'.format(total_obj), 'total.value')
        render.append('scoreboard players set {} {} {}'.format(total_holder, disp, base))
        render.append('function {}:render/row_d_total_{} with storage {}:render total'.format(DP_NAME, tag, DP_NAME))
        row_total = ['$scoreboard players display name {} {} {}'.format(total_holder, disp, _json_component({'text': '', 'extra': [
            {'text': '$(label) ', 'color': cfg('title_color')},
            {'text': '$(value)', 'color': cfg('value_color')}]}))]
        dp_write_file('render/row_d_total_{}.mcfunction'.format(tag), '\n'.join(row_total) + '\n')
    dp_write_file('render/{}.mcfunction'.format(spec['kind']), '\n'.join(render) + '\n')


def _dp_write_boards(names: List[str], limit: int, both_limit: int):
    """
    生成三个榜的渲染函数与 render 分发。

    每个榜的侧边栏格式（自上而下）：
        全服总和行（label + 数值） -> 换行（空行） -> 排行榜（#名次 玩家 数值）
    侧边栏标题 = 该榜计分项的 displayname（破坏总和榜 / 放置总和榜 / 总榜）。
    render.mcfunction 只渲染 #board 指向的当前榜；切换榜单由插件写 #board + setdisplay。
    """
    specs = _board_specs()
    total_on = bool(cfg('show_total_row'))
    blank_on = bool(cfg('show_blank_row'))

    # render.mcfunction：按 #board 分发到当前榜
    dispatch = ['execute if score #board dsb_ctl matches {} run function {}:render/{}'.format(
        spec['index'], DP_NAME, spec['kind']) for spec in specs]
    dp_write_file('render.mcfunction', '\n'.join(dispatch) + '\n')

    for spec in specs:
        kind = spec['kind']
        disp = spec['display']
        if 'subs' in spec:
            # 同榜（放置+破坏堆叠）：走独立的堆叠渲染器
            _dp_write_both_board(spec, names, both_limit)
            continue
        objective = spec['objective']
        total_obj = spec['total_objective']
        prefix = spec['prefix']
        total_holder = prefix + 't'
        blank_holder = prefix + 'x'
        render = []
        # 先清空本榜全部占位行，再把当前前 N 名写回去（玩家变少时不会留旧行）
        for holder in _board_holders(prefix, limit):
            render.append('scoreboard players reset {} {}'.format(holder, disp))
        if total_on:
            # 全服合计 = 所有候选玩家分数之和，每次重画时重新算
            render.append('scoreboard players set #total {} 0'.format(total_obj))
            for name in names:
                render.append('scoreboard players operation #total {} += {} {}'.format(total_obj, name, objective))
        # 排名：把每个玩家的分数复制到 dsb_work，再逐名次取最大
        for name in names:
            render.append('scoreboard players operation {} dsb_work = {} {}'.format(name, name, objective))
        for rank in range(1, limit + 1):
            holder = prefix + 'r{:02d}'.format(rank)
            render.append('scoreboard players set #best dsb_work {}'.format(SCORE_MIN))
            for name in names:
                render.append('scoreboard players operation #best dsb_work > {} dsb_work'.format(name))
            render.append('scoreboard players set #picked dsb_ctl 0')
            # #best 沉底（<= SCORE_MIN）说明没有更多可选玩家，该名次留空
            for player_id, name in enumerate(names, 1):
                rel = 'render/{}_{}_{}'.format(kind, rank, dp_slug(name))
                render.append('execute if score #best dsb_work matches {}.. if score #picked dsb_ctl matches 0 '
                              'if score {} dsb_work = #best dsb_work run function {}:{}'.format(
                                  SCORE_MIN + 1, name, DP_NAME, rel))
                chosen = ['scoreboard players set #picked dsb_ctl 1',
                          'scoreboard players set {} dsb_work {}'.format(name, SCORE_MIN),
                          # 数值写进占位行的【分数】：原版侧边栏名字列左对齐、分数列右对齐，
                          # 同分时按占位行名字（dsb_*r01..r13 零填充）升序，正好维持名次顺序。
                          'scoreboard players operation {} {} = {} {}'.format(holder, disp, name, objective),
                          # ⚠ ensure_ascii=False：Brigadier 的 NBT 字符串不接受 \uXXXX 转义，
                          #   JSON 组件（display name）才接受。玩家名虽是 ASCII，统一按字面输出。
                          'data modify storage {}:render row set value {}'.format(
                              DP_NAME, json.dumps({'name': name}, ensure_ascii=False, separators=(',', ':')))]
                chosen += ['function {}:render/row_{}{} with storage {}:render row'.format(DP_NAME, kind, rank, DP_NAME),
                           'scoreboard players set {} dsb_owner {}'.format(holder, player_id),
                           'scoreboard players operation {} dsb_value = {} {}'.format(holder, name, objective)]
                dp_write_file(rel + '.mcfunction', '\n'.join(chosen) + '\n')
            # 显示名只放「名次 + 名字」（左对齐列）；数值在右侧分数列
            extra = []
            if cfg('show_rank_number'):
                extra.append({'text': '#{} '.format(rank), 'color': RANK_COLORS[min(rank - 1, len(RANK_COLORS) - 1)]})
            extra += [{'text': '$(name)', 'color': cfg('name_color')}]
            row = ['$scoreboard players display name {} {} {}'.format(holder, disp, _json_component({'text': '', 'extra': extra}))]
            dp_write_file('render/row_{}{}.mcfunction'.format(kind, rank), '\n'.join(row) + '\n')
        if total_on:
            # 总和行：名字列放 label（左对齐），分数列放全服合计（右对齐）
            render.append('scoreboard players operation {} {} = #total {}'.format(total_holder, disp, total_obj))
            # 注意：没有宏变量就不要加 $ 前缀，否则函数加载报 "Macro without variables"
            render.append('scoreboard players display name {} {} {}'.format(
                total_holder, disp, _json_component({'text': str(spec['total_label']), 'color': cfg('title_color')})))
            render.append('scoreboard players operation {} dsb_value = #total {}'.format(total_holder, total_obj))
        if blank_on:
            # 换行：总和行与排行榜之间的空行。
            # 分数列即数值，空行没有数值——取「第一名分数+1」排在总和行与第一名之间；
            # 没有玩家入选时 guard 不成立，空行随开头的 reset 一起清掉（不显示）。
            rank1_holder = prefix + 'r01'
            blank_row = [
                'execute if score {} {} matches {}.. run scoreboard players operation {} {} = {} {}'.format(
                    rank1_holder, disp, SCORE_MIN + 1, blank_holder, disp, rank1_holder, disp),
                'execute if score {} {} matches {}.. run scoreboard players add {} {} 1'.format(
                    rank1_holder, disp, SCORE_MIN + 1, blank_holder, disp),
                'scoreboard players display name {} {} {}'.format(blank_holder, disp, _json_component({'text': ''})),
            ]
            dp_write_file('render/blank_{}.mcfunction'.format(kind), '\n'.join(blank_row) + '\n')
            render.append('function {}:render/blank_{}'.format(DP_NAME, kind))
        dp_write_file('render/{}.mcfunction'.format(kind), '\n'.join(render) + '\n')


def dp_save_all_sync():
    """仅首次迁移保存一次；不在运行时用存档冒充实时数据源。"""
    if dp_can_read():
        response = server.rcon_query('save-all flush')
        if not response or 'Saved' not in response:
            raise RuntimeError('首次迁移未确认存档完成: {!r}'.format(response))
        return
    _dp_save_done.clear()
    server.execute('save-all flush')
    deadline = time.monotonic() + 30
    while not _dp_save_done.wait(timeout=0.2):
        if _unloaded:
            raise RuntimeError('插件已卸载，取消首次迁移')
        if time.monotonic() >= deadline:
            raise RuntimeError('首次迁移等待 Saved the game 超时；请检查存档日志并运行 !!spms dp rebuild')


def _dp_migrate_objectives():
    """
    1.3.x -> 1.4.x 记分项迁移（只做一次）：
      - 补建新榜单的记分项（craft/drop/pick/kill/custom 的 per-player/全服/展示项）
      - 删除废弃项（旧 stacked 展示项 dsb_rank、旧总榜 dsb_tot/dsb_ttot/dsb_rank_tot）
    控制台指令失败（已存在/不存在）只是在控制台留一行错误，无害；
    之后 reload 时数据包 load 会自行建齐其余计分项。
    """
    if server is None:
        return
    mark = data_file(DP_OBJECTIVES_MARK)
    if os.path.isfile(mark):
        return
    if not _server_ready():
        return  # 服务端没就绪，指令会被丢掉；下次 dp_setup 再试
    try:
        for obj in DP_LEGACY_OBJECTIVES:
            server.execute('scoreboard objectives remove {}'.format(obj))
        for obj in DP_NEW_OBJECTIVES:
            server.execute('scoreboard objectives add {} dummy'.format(obj))
        _ensure_parent_dir(mark)
        with open(mark, 'w', encoding='utf-8') as f:
            f.write('ok\n')
        server.logger.info('[spms] 已补建 1.4.0 新记分项（总榜 + 三榜展示项），并移除废弃的 dsb_rank')
    except Exception as e:
        server.logger.debug('[spms] 迁移记分项失败（reload 后会由数据包 load 重建）: {}'.format(e))


def dp_setup(force_reseed: bool = False):
    global _dp_seeded, _dp_players, _dp_created_m, _dp_created_u, _dp_existing_runtime
    if not _dp_mode() or server is None:
        return
    with _tick_lock:
        # rebuild 仅重建生成文件；持久化初始化标记使历史值永远只导入一次。
        if not _dp_seeded and not _dp_existing_runtime:
            dp_save_all_sync()
            sync_player_states(initial=True)
        if not _dp_blocks or force_reseed:
            dp_load_registry()
        if _unloaded or not server.is_server_running():
            return
        dp_write_pack_meta()
        info = dp_collect()
        # 旧版的玩家函数引用的是旧准则项，清理本插件生成的函数，避免 reload 加载残留文件。
        for folder in ('function', 'functions'):
            path = os.path.join(dp_root(), 'data', DP_NAME, folder)
            if os.path.isdir(path):
                shutil.rmtree(path)
        dp_write_functions(info, dp_needed_items(info))
        _dp_created_m, _dp_created_u = set(_dp_blocks), _dp_blocks & _dp_items
        _dp_players = set(info)
        dp_save_index()
        _dp_seeded = True
        _dp_existing_runtime = True
        # 迁移 + 让数据包在 reload 后的下一个 tick 重跑一次幂等 init
        _dp_migrate_objectives()
        try:
            server.execute('scoreboard players set #ready dsb_ctl 0')
            server.execute('scoreboard players set #board dsb_ctl {}'.format(_board_spec(_active_board())['index']))
            server.execute('scoreboard players set #visible dsb_ctl {}'.format(int(bool(cfg('sidebar_visible')))))
        except Exception as e:
            server.logger.debug('[spms] 预置记分失败（init 会补上）: {}'.format(e))
        server.execute('reload')
        server.logger.info('[spms] 已生成实时数据包并请求 reload：{} 名玩家、{} 种方块；统计和显示由服务端逐 tick 执行，无需 RCON'.format(len(info), len(_dp_blocks)))


def _parse_score_response(resp: Optional[str]) -> Optional[int]:
    if not resp:
        return None
    match = re.fullmatch(r'.+ has (-?\d+) \[[^\]]+\]', resp.strip())
    return int(match.group(1)) if match else None


def dp_read_score(name: str, obj: str) -> Optional[int]:
    return _parse_score_response(server.rcon_query('scoreboard players get {} {}'.format(name, obj)))


def dp_candidates() -> List[str]:
    return sorted(dp_collect())


def dp_read_totals() -> int:
    if not dp_can_read():
        return 0
    updated = 0
    for name in dp_candidates():
        state = _states.get(name)
        if state is None:
            continue
        for obj, attr in ((DP_OBJ_BREAK, 'break_total'), (DP_OBJ_PLACE, 'placed')):
            value = dp_read_score(name, obj)
            if value is not None and value != getattr(state, attr):
                setattr(state, attr, value)
                updated += 1
    return updated


def dp_diagnose() -> List[str]:
    return ['§6======== 简单放置破坏榜 数据包 ========',
            '§7Minecraft: §f{}   §7pack_format: §f{}'.format(_server_mc_version(), dp_pack_format()),
            '§7当前榜单: §f{}   §7数据包: §f{}'.format(_board_spec(_active_board())['title'], dp_root()),
            '§7统计/排名/显示: §a服务端逐 tick；无需 RCON、无需周期存档',
            '§7注册方块: §f{}   §7used 准则: §f{}   §7排名玩家: §f{}'.format(len(_dp_created_m), len(_dp_created_u), len(_dp_players)),
            '§7最近错误: §f{}'.format(_dp_last_error or '(无)'),
            '§7可用 /datapack list 核对加载，用 /scoreboard players get #ready dsb_ctl 核对值为 {}'.format(DP_SCHEMA),
            '§8新增方块模组请提供完整 datapack_registry_file，或填写 extra_blocks / extra_items。']


# ===========================================================================
# 逐 tick：读盘 / 增量
# ===========================================================================

def file_stamp(uuid: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(stats_path(uuid))
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def refresh_player(state: 'PlayerState', initial: bool) -> bool:
    """
    重读一个玩家的统计文件并更新缓存。
    返回 True 表示榜单数值有变化。读失败时保持旧缓存并把 stamp 留着（下个 tick 重试）。
    """
    # 先取 stamp 再读文件：万一读取期间服务端刚好在写，下次 tick 的 stat 会和这里
    # 记录的值不同，从而被重新读一遍，不会漏掉这次变化。
    stamp = file_stamp(state.uuid)

    stats = read_stats(server, state.uuid)
    if stats is None:
        if initial:
            # 没有统计文件（从没玩过）-> 全部按 0 起步，仍然参与榜单。
            # loaded 保持 False：等这个玩家的统计文件第一次出现时，要按“初始化”读一遍
            # 历史累计值，而不是把它当成从 0 开始的增量。
            state.used = {}
            state.mined = {}
            state.placed = 0
            state.break_total = 0
            state.craft_total = 0
            state.drop_total = 0
            state.pick_total = 0
            state.kill_total = 0
            state.kill_entities = set()
            state.baseline = 0
            state.loaded = False
            state.stamp = stamp
            return True
        return False

    used = stat_class_map(stats, USED_CLASS)
    mined = stat_class_map(stats, MINED_CLASS)
    killed = stat_class_map(stats, KILLED_CLASS)
    custom = stat_class_map(stats, CUSTOM_CLASS)

    changed = False
    if _dp_mode():
        # 数据包只在首次初始化时导入历史总和；在线增量完全由服务端维护。
        if initial or not state.loaded:
            state.break_total = stat_class_sum(stats, MINED_CLASS)
            state.placed = initial_placed_total(stats, used, mined)
            state.craft_total = stat_class_sum(stats, CRAFT_CLASS)
            state.drop_total = stat_class_sum(stats, DROP_CLASS)
            state.pick_total = stat_class_sum(stats, PICK_CLASS)
            state.kill_total = stat_class_sum(stats, KILLED_CLASS)
            state.custom_total = stat_class_sum(stats, CUSTOM_CLASS)
            state.kill_entities = set(killed.keys())
            state.custom_stats = set(custom.keys())
            state.baseline = 0
            changed = True
        elif used != state.used or mined != state.mined:
            changed = True
    else:
        # ---- poll 模式：插件自己算 ----
        new_break = stat_class_sum(stats, MINED_CLASS)
        if new_break != state.break_total:
            state.break_total = new_break
            changed = True
        if initial:
            state.placed = initial_placed_total(stats, used, mined)
            changed = True
        else:
            added = accumulate_placed(state, used, mined)
            if added:
                state.placed += added
                changed = True
        for cls_name, attr in ((CRAFT_CLASS, 'craft_total'), (DROP_CLASS, 'drop_total'),
                               (PICK_CLASS, 'pick_total'), (KILLED_CLASS, 'kill_total'),
                               (CUSTOM_CLASS, 'custom_total')):
            new_value = stat_class_sum(stats, cls_name)
            if new_value != getattr(state, attr):
                setattr(state, attr, new_value)
                changed = True
        state.kill_entities = set(killed.keys())
        state.custom_stats = set(custom.keys())

    # 更新快照（整份 used / mined）
    state.used = used
    state.mined = mined
    state.loaded = True
    state.stamp = stamp
    return changed


def sync_player_states(initial: bool = False) -> bool:
    """按当前 uuid_list 建/删缓存，并只重读文件变了的玩家（不每 tick 全量重读所有 JSON）"""
    global _stat_scans
    changed = False
    players = known_players()
    names = set(name for name, _ in players)

    with _lock:
        for name in list(_states.keys()):
            if name not in names:
                _states.pop(name, None)

    force_all = initial
    dirty: List['PlayerState'] = []
    for name, uuid in players:
        with _lock:
            state = _states.get(name)
            if state is None or state.uuid != uuid:
                state = PlayerState(name, uuid)
                _states[name] = state
                force_all_this = True
            else:
                force_all_this = False
        if force_all or force_all_this:
            dirty.append(state)
            continue
        stamp = file_stamp(uuid)
        _stat_scans += 1
        if stamp is None:
            continue
        if stamp != state.stamp:
            dirty.append(state)

    for state in dirty:
        try:
            if refresh_player(state, initial=force_all or not state.loaded):
                changed = True
        except Exception as e:
            if server is not None:
                server.logger.error('[spms] 刷新玩家 {} 失败: {}'.format(state.name, e))
    return changed


# ===========================================================================
# 榜单排序
# ===========================================================================

def board_full() -> Dict[str, List[Tuple[str, int]]]:
    """各榜的完整排序（降序，同分按玩家名升序），不截断。键为榜单 id。"""
    with _lock:
        states = list(_states.values())
    result: Dict[str, List[Tuple[str, int]]] = {}
    for board_id, attr in ((BOARD_BREAK, 'break_total'), (BOARD_PLACE, 'placed'),
                           (BOARD_CRAFT, 'craft_total'), (BOARD_DROP, 'drop_total'),
                           (BOARD_PICK, 'pick_total'), (BOARD_KILL, 'kill_total'),
                           (BOARD_CUSTOM, 'custom_total')):
        rows = [(s.name, getattr(s, attr)) for s in states]
        rows.sort(key=lambda x: (-x[1], x[0]))
        result[board_id] = rows
    return result


def board_rows(limit: Optional[int] = None) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    """返回 (破坏榜, 放置榜) 前 N 行"""
    full = board_full()
    top_n = max(0, int(cfg('board_top_n'))) if limit is None else max(0, int(limit))
    return full[BOARD_BREAK][:top_n], full[BOARD_PLACE][:top_n]


def _board_rows_for(board_id: str) -> List[Tuple[str, int]]:
    return board_full().get(board_id, [])


# ===========================================================================
# 侧边栏渲染
#
# 关键点：原版一个 display slot 只能挂一个计分项（sidebar.team.<color> 只是按队伍颜色
# 控制可见性，不会把多个榜单堆起来）。所以每个榜各用一个 dummy 计分项，切换榜单就是
# setdisplay 到对应计分项：
#   假玩家名占行 -> scoreboard players display name 写入「名次 + 名字」（左对齐列）
#                -> 占位行的分数 = 该行数值：原版分数列右对齐，排序也按分数来
# 每个榜的视觉顺序：全服总和行 -> 换行 -> 排行榜。
# ===========================================================================

def _json_component(obj: Dict[str, Any]) -> str:
    """
    输出紧凑 JSON 文本组件。Brigadier 的字符串参数以空格断词，所以把字符串里的空格
    转义成 \\u0020，保证整条命令里不出现裸露空格。
    """
    text = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    return text.replace(' ', '\\u0020')


def _title_component(title: str) -> str:
    return _json_component({'text': str(title), 'color': cfg('title_color'), 'bold': True})


def _row_component(rank: Optional[int], name: str) -> str:
    """名次 + 玩家名（数值在右侧分数列，这里不放）"""
    extra: List[Dict[str, Any]] = []
    if rank is not None and cfg('show_rank_number'):
        extra.append({'text': '#{} '.format(rank), 'color': RANK_COLORS[min(rank - 1, len(RANK_COLORS) - 1)]})
    extra.append({'text': str(name), 'color': cfg('name_color')})
    return _json_component({'text': '', 'extra': extra})


def _row_component_with_value(rank: Optional[int], name: str, value: int) -> str:
    """名次 + 玩家名 + 数值（同榜用：数值写进显示名，名次分排序）"""
    extra: List[Dict[str, Any]] = []
    if rank is not None and cfg('show_rank_number'):
        extra.append({'text': '#{} '.format(rank), 'color': RANK_COLORS[min(rank - 1, len(RANK_COLORS) - 1)]})
    extra.append({'text': str(name) + ' ', 'color': cfg('name_color')})
    extra.append({'text': str(value), 'color': cfg('value_color')})
    return _json_component({'text': '', 'extra': extra})


def _total_row_component(label: str) -> str:
    return _json_component({'text': str(label), 'color': cfg('title_color')})


def _total_row_component_with_value(label: str, value: int) -> str:
    return _json_component({'text': '', 'extra': [
        {'text': str(label) + ' ', 'color': cfg('title_color')},
        {'text': str(value), 'color': cfg('value_color')}]})


def _remove_objectives(objs: List[str]):
    for obj in objs:
        server.execute('scoreboard objectives remove {}'.format(obj))


def _clear_render_cache():
    global _last_render, _last_scores, _obj_ready
    with _lock:
        _last_render = {}
        _last_scores = {}
        _obj_ready = False


def setup_objectives():
    """建 poll 模式的展示计分项（数据包模式由 load/init 负责建项与显示）。"""
    global _obj_ready
    if _dp_mode():
        # 数据包 load 函数负责建项与显示，不能在异步 reload 后又删除它们。
        _obj_ready = True
        return
    spec = _board_spec(_active_board())
    # 切换榜单时删旧建新：顺带清掉另一个榜残留的占位行
    _remove_objectives([OBJ_BOARD])
    server.execute('scoreboard objectives add {} dummy {}'.format(OBJ_BOARD, _title_component(spec['title'])))
    if 'subs' in spec:
        # 同榜用名次分排序，分数列藏起来（数值写进显示名）
        server.execute('scoreboard objectives modify {} numberformat blank'.format(OBJ_BOARD))
    else:
        # 数值走原版分数列（右对齐），用 value_color 上色
        server.execute('scoreboard objectives modify {} numberformat styled {}'.format(
            OBJ_BOARD, _json_component({'color': cfg('value_color')})))
    _obj_ready = True
    if _side_visible:
        show_sidebar()


def _set_visible(visible: bool):
    """记住侧边栏可见性到配置（启动时按它决定显示与否）。"""
    global _side_visible
    _side_visible = visible
    _config['sidebar_visible'] = bool(visible)
    try:
        save_config(server)
    except Exception:
        pass


def show_sidebar():
    global _side_visible
    _side_visible = True
    _config['sidebar_visible'] = True
    if not _obj_ready:
        return
    if _dp_mode():
        server.execute('scoreboard players set #visible dsb_ctl 1')
    server.execute('scoreboard objectives setdisplay sidebar {}'.format(_board_spec(_active_board())['display'] if _dp_mode() else OBJ_BOARD))
    if _dp_mode():
        server.execute('function {}:render'.format(DP_NAME))


def hide_sidebar():
    global _side_visible
    _set_visible(False)
    if _dp_mode():
        server.execute('scoreboard players set #visible dsb_ctl 0')
    server.execute('scoreboard objectives setdisplay sidebar')


def render(force: bool = False):
    """渲染当前榜；数据包模式下由服务端逐 tick 自己画，插件只在 force 时触发。"""
    global _stat_renders
    if server is None or not _obj_ready:
        return
    if not _side_visible:
        return
    if _dp_mode():
        if force:
            server.execute('function {}:render'.format(DP_NAME))
        return
    render_board(force=force)
    _stat_renders += 1


def render_board(force: bool = False):
    """poll 模式：把当前榜画到侧边栏（同榜为堆叠式，数值写进显示名）。"""
    global _last_render
    spec = _board_spec(_active_board())

    # 视觉顺序 + 分数：单榜分数=数值（右对齐）；同榜用名次分，数值进显示名
    desired: List[Tuple[str, int, str]] = []          # (holder, score, component)
    if 'subs' in spec:
        both_n = max(0, min(int(cfg('both_top_n')), MAX_BOTH_ROWS))
        for sub_index, sub in enumerate(spec['subs']):
            rows = _board_rows_for({DP_OBJ_BREAK: BOARD_BREAK, DP_OBJ_PLACE: BOARD_PLACE}[sub['objective']])[:both_n]
            base = (2 * both_n + 2) if sub_index == 0 else (both_n + 1)
            total_value = sum(v for _, v in _board_rows_for(
                {DP_OBJ_BREAK: BOARD_BREAK, DP_OBJ_PLACE: BOARD_PLACE}[sub['objective']]))
            desired.append((sub['total_holder'], base, _total_row_component_with_value(sub['total_label'], total_value)))
            for i, (name, value) in enumerate(rows):
                desired.append((sub['row_prefix'] + '{:02d}'.format(i + 1), base - i - 1,
                                _row_component_with_value(i + 1, name, value)))
    else:
        prefix = spec['prefix']
        rows = _board_rows_for(spec['id'])
        top_n = max(0, min(int(cfg('board_top_n')), _board_row_budget()))
        rows = rows[:top_n]
        if cfg('show_total_row'):
            total_value = sum(v for _, v in _board_rows_for(spec['id']))
            desired.append((prefix + 't', total_value, _total_row_component(spec['total_label'])))
        if cfg('show_blank_row'):
            desired.append((prefix + 'x', 0, _json_component({'text': ''})))
        for i, (name, value) in enumerate(rows):
            desired.append((prefix + 'r{:02d}'.format(i + 1), value, _row_component(i + 1, name)))

    # 分数只用来自定义排序：视觉第 1 行分数最高，依次递减
    total = len(desired)
    plan: Dict[str, Tuple[int, str]] = {}
    for idx, (holder, _unused, component) in enumerate(desired):
        plan[holder] = (total - idx, component)

    with _lock:
        previous = dict(_last_render)

    to_remove = [h for h in previous if h not in plan]
    to_write = [h for h, val in plan.items() if force or previous.get(h) != val]
    # 保持视觉顺序发指令（纯粹为了日志好看）
    to_write.sort(key=lambda h: -plan[h][0])

    for holder in to_remove:
        server.execute('scoreboard players reset {} {}'.format(holder, OBJ_BOARD))
    for holder in to_write:
        score, component = plan[holder]
        server.execute('scoreboard players set {} {} {}'.format(holder, OBJ_BOARD, score))
        server.execute('scoreboard players display name {} {} {}'.format(holder, OBJ_BOARD, component))

    with _lock:
        _last_render = plan

    if to_remove or to_write:
        _log('渲染 {}：写 {} 行，清 {} 行'.format(spec['id'], len(to_write), len(to_remove)))


# ===========================================================================
# 逐 tick 主循环
# ===========================================================================

def _log(msg: str):
    if server is not None and cfg('debug_log'):
        server.logger.info('[spms] ' + msg)


def maybe_save_all():
    """
    原版 stats/*.json 只在保存时落盘。要“实时”，就必须周期性地把内存里的统计刷到磁盘。
    RCON 可用时用 rcon 的 save-all flush，否则写一条 save-all 到服务端 stdin。
    没人在线时不做（统计不会变），省掉无谓的写盘。
    """
    global _last_save_all
    interval = float(cfg('save_all_interval_seconds') or 0)
    if interval <= 0:
        return
    with _lock:
        if not _online:
            return
    now = time.monotonic()
    if now - _last_save_all < interval:
        return
    _last_save_all = now
    try:
        if server.is_rcon_running():
            server.rcon_query('save-all flush')
        else:
            server.execute('save-all')
    except Exception as e:
        server.logger.debug('[spms] save-all 失败: {}'.format(e))


def tick_once(force: bool = False) -> bool:
    """一个“tick”：检查数据变化 -> 只在变化时刷新 Sidebar"""
    # force（!!spms refresh / dp rebuild）会等当前这次 tick 跑完；周期性的 tick 直接跳过，
    # 不排队堆积。反正下一次 tick 50ms 后就到。
    if not _tick_lock.acquire(blocking=bool(force)):
        return False
    try:
        return _tick_once_locked(force)
    finally:
        _tick_lock.release()


def _tick_once_locked(force: bool = False) -> bool:
    global _last_uuid_refresh, _tick_count

    if server is None or _unloaded:
        return False
    if not force and not server.is_server_running():
        return False
    # 兜底：服务端刚就绪就补建榜。MCDR 先加载插件再启动服务端，on_load 时建榜会失败。
    if cfg('auto_show') and not _boards_started and _server_ready():
        try:
            _start_boards_once()
        except Exception as e:
            server.logger.error('[spms] 补建榜单失败: {}'.format(e), exc_info=True)
    # 侧边栏没显示 / 计分项还没建：不需要轮询磁盘。重新 show 时 stamp 已经变了，
    # 会把隐藏期间的增量一次性补上（快照没丢，所以放置榜不会漏计也不会重复计）。
    if not force and (not _side_visible or not _obj_ready):
        return False

    # uuid / 玩家名映射定期重扫（新玩家、名字变动）
    interval = float(cfg('refresh_uuid_interval_seconds') or 0)
    if interval > 0 and time.monotonic() - _last_uuid_refresh >= interval:
        try:
            refresh_uuid_list(server)
        except Exception as e:
            server.logger.debug('[spms] 刷新 uuid 映射失败: {}'.format(e))

    if _dp_mode():
        changed = _dp_tick(False)
        if force:
            render(force=True)
        return changed

    maybe_save_all()

    # 文件变化扫描：默认每 tick 一次（scan_interval_ticks = 1）。
    # 调大可以进一步降低 os.stat 的频率（代价是发现变化变慢）。
    scan_interval = max(1, int(cfg('scan_interval_ticks') or 1))
    changed = False
    if force or _tick_count % scan_interval == 0:
        changed = sync_player_states(initial=False)
    _tick_count += 1

    if force or changed or not _rendered_once:
        render(force=force and not _rendered_once)
    return changed


def _dp_tick(changed: bool) -> bool:
    # Python 线程仅维护玩家名单；在线数值不再扫描 JSON，也不经过 RCON。
    if set(dp_collect()) != _dp_players:
        sync_player_states(initial=False)
        dp_setup()
        return True
    return changed


def _tick_loop():
    while not _stop_event.is_set():
        started = time.monotonic()
        try:
            tick_once()
        except Exception as e:
            if server is not None:
                server.logger.error('[spms] tick 出错: {}'.format(e), exc_info=True)

        interval = max(0.005, float(cfg('tick_interval_ms')) / 1000.0)
        elapsed = time.monotonic() - started
        wait = max(0.0, interval - elapsed)
        if _wake_event.wait(wait):
            _wake_event.clear()


def _ensure_tick_thread():
    global _tick_thread
    with _lock:
        if _tick_thread is not None and _tick_thread.is_alive():
            return
        _stop_event.clear()
        _tick_thread = threading.Thread(target=_tick_loop, name='sidebar_boards-tick', daemon=True)
        _tick_thread.start()
    if server is not None:
        if _dp_mode():
            server.logger.info('[spms] 玩家名单维护线程已启动；统计与显示由数据包每 tick 执行')
        else:
            server.logger.info('[spms] 文件轮询线程已启动（{}ms，每 {} 次扫描一次）'.format(
                cfg('tick_interval_ms'), max(1, int(cfg('scan_interval_ticks') or 1))))


# ===========================================================================
# 启动 / 停止
# ===========================================================================

def start_boards():
    """初始化：从服务端玩家数据 JSON 读历史累计值（不从 0 开始），然后建项、渲染"""
    global _rendered_once, _last_save_all, _side_visible, _tick_count
    _side_visible = bool(cfg('sidebar_visible'))
    _clear_render_cache()
    with _lock:
        _states.clear()
    _rendered_once = False
    _last_save_all = 0.0
    _tick_count = 0

    sync_player_states(initial=True)

    if _dp_mode():
        # 数据包 load 函数建项、导入历史、初始化侧边栏。
        dp_setup()
        if _unloaded or not server.is_server_running():
            return

    setup_objectives()
    if not _dp_mode():
        render(force=True)
    _rendered_once = True

    top_n = max(0, int(cfg('board_top_n')))
    budget = _board_row_budget()
    if top_n > budget:
        server.logger.warning(
            '[spms] board_top_n={} 超过当前配置下的行数上限（侧边栏 15 行 - 总和/换行 = {} 行），已按 {} 行显示。'.format(
                top_n, budget, budget))
    server.logger.info('[spms] 已启动：模式 {}，当前榜 {}；统计目录 {}；识别玩家 {} 名（已排除 bot_ 前缀）'.format(
        cfg('realtime_mode'), _board_spec(_active_board())['title'], stats_dir(), len(known_players())))


def stop_boards():
    global _tick_thread
    _stop_event.set()
    _wake_event.set()
    with _lock:
        thread = _tick_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
    _tick_thread = None


# ===========================================================================
# 命令
# ===========================================================================

def _reply(src: CommandSource, text: str):
    try:
        src.reply(text)
    except Exception:
        if server is not None:
            server.logger.info(text)


def _need_perm(src: CommandSource) -> bool:
    try:
        return src.has_permission(2)
    except Exception:
        return False


def _board_status_sections(full):
    """按榜单配置生成 status 的各个小节（同榜输出两个分榜）。"""
    top_n = max(0, int(cfg('board_top_n')))
    both_n = max(0, min(int(cfg('both_top_n')), MAX_BOTH_ROWS))
    sections = []
    for spec in _board_specs():
        if 'subs' in spec:
            for sub in spec['subs']:
                board_id = {DP_OBJ_BREAK: BOARD_BREAK, DP_OBJ_PLACE: BOARD_PLACE}[sub['objective']]
                sections.append(('{} - {}'.format(spec['title'], sub['total_label']),
                                 full.get(board_id, [])[:both_n]))
        else:
            sections.append((spec['title'], full.get(spec['id'], [])[:top_n]))
    return sections


def _print_status(src: CommandSource):
    if _dp_mode():
        if dp_can_read():
            dp_read_totals()
        for line in dp_diagnose():
            _reply(src, line)
        if not dp_can_read():
            _reply(src, '§7实时排名见侧边栏；未开启 RCON 时不把本地历史快照当作实时值显示。')
            return
        for title, rows in _board_status_sections(board_full()):
            _reply(src, '§6---- {} ----'.format(title))
            for line in _status_rows(rows):
                _reply(src, line)
        return
    full = board_full()
    with _lock:
        n_states = len(_states)
        n_online = len(_online)
        n_uuid = len(uuid_list)
    lines = [
        '§6======== 简单放置破坏榜 状态 ========',
        '§7实时方式: §f{}   §7当前榜: §f{}   §7侧边栏: {}'.format(
            cfg('realtime_mode'), _board_spec(_active_board())['title'], '§a显示中' if _side_visible else '§c已隐藏'),
        '§7统计目录: §f{}'.format(stats_dir()),
        '§7已识别玩家: §f{}   §7已排除 bot_: §f{}   §7在线: §f{}'.format(
            n_uuid, n_uuid - n_states, n_online),
        '§7假人上榜: §f{} §8(!!spms botboard on/off 切换)'.format(
            '只上榜 bot_ 假人' if cfg('bot_only_mode') else '关（只上榜真人）'),
        '§7逐 tick: §f{}ms   §7扫描次数: §f{}   §7读取次数: §f{}   §7渲染次数: §f{}'.format(
            cfg('tick_interval_ms'), _stat_scans, _stat_reads, _stat_renders),
        '§7主动保存间隔: §f{}s{}'.format(
            cfg('save_all_interval_seconds'),
            '' if cfg('save_all_interval_seconds') else ' §8(关闭：数据只能等原版自动保存，约 5 分钟)'),
    ]
    for title, rows in _board_status_sections(full):
        lines.append('§6---- {} ----'.format(title))
        lines.extend(_status_rows(rows))
    for line in lines:
        _reply(src, line)


def _status_rows(rows: List[Tuple[str, int]]) -> List[str]:
    if not rows:
        return ['  §7(空)']
    return ['  §e#{} §f{} §b{}'.format(i + 1, name, value) for i, (name, value) in enumerate(rows)]


def _print_debug(src: CommandSource):
    sdir = stats_dir()
    files = []
    if os.path.isdir(sdir):
        files = [f for f in os.listdir(sdir) if f.endswith('.json')]
    rcon = '?'
    try:
        rcon = '§a是' if server.is_rcon_running() else '§c否'
    except Exception:
        pass
    lines = [
        '§6======== 简单放置破坏榜 诊断 ========',
        '§7MCDR working_directory: §f{}'.format(mcdr_working_dir()),
        '§7世界目录: §f{}'.format(world_dir()),
        '§7统计目录: §f{}   §7存在: {}   §7文件数: {}'.format(
            sdir, '§a是' if os.path.isdir(sdir) else '§c否', len(files)),
        '§7RCON: {} §8(save-all 走 rcon 更可靠)'.format(rcon),
        '§7计分项: {}'.format('§a已建立' if _obj_ready else '§c未建立'),
        '§7placed 预置字段路径: §f{}'.format(cfg('placed_precomputed_path') or '(未配置，按 used ∩ mined 现算)'),
        '§7bot 前缀: §f{}'.format(', '.join(cfg('bot_name_prefixes') or []) or '(无)'),
        '§8以下是磁盘/初始化快照；实时值用 /scoreboard players get <玩家> dsb_brk 或 dsb_plc 核对:',
    ]
    for name, uuid in known_players()[:5]:
        with _lock:
            state = _states.get(name)
        if state is None:
            continue
        stats = read_stats(server, uuid) or {}
        used = stat_class_map(stats, USED_CLASS)
        mined = stat_class_map(stats, MINED_CLASS)
        targets = set(used.keys()) & set(mined.keys())
        lines.append('  §f{} §7uuid={}'.format(name, uuid))
        lines.append('    §7mined 项数 §f{} §7总和 §b{}'.format(len(mined), state.break_total))
        lines.append('    §7used 项数 §f{} §7∩mined 目标 §f{} §7项 §7起始和 §f{} §7→ 当前 placed §b{}'.format(
            len(used), len(targets), sum(used[i] for i in targets), state.placed))
    for line in lines:
        _reply(src, line)


def _help_lines() -> List:
    """精简 help：插件标题 + 指令 + 榜单按钮（点击切换，悬停看统计项英文）。"""
    lines = [
        '§6======== 简单放置破坏榜 ========',
        '§7点击榜单按钮切换（悬停查看统计项）：',
    ]
    specs = _board_specs()
    for i in range(0, len(specs), 4):
        row = RTextList()
        for j, spec in enumerate(specs[i:i + 4]):
            if j:
                row.append(' ')
            row.append(
                RText('[{}]'.format(spec['title']), color=RColor.gold)
                .set_click_event(RAction.run_command, '!!spms ' + spec['id'])
                .set_hover_text('{}  {}'.format(spec['title'], spec['stat']))
            )
        lines.append(row)
    lines += [
        '§7!!spms §fbotboard on|off §7纯假人榜单（打开后所有榜单只上榜 bot_）',
        '§7!!spms §fshow|hide §7显示/隐藏侧边栏',
        '§7!!spms §fstatus|debug|dp|refresh §7状态/诊断/数据包/重画',
    ]
    return lines


def _run_async(func, label: str):
    """
    命令回调跑在 MCDR 的任务执行器线程上。datapack 模式的建立/重建里有 save-all 等待，
    在那里阻塞会触发 MCDR 的 watchdog（默认 10 秒没响应就报错），所以丢到独立线程。
    """
    def wrapper():
        try:
            func()
        except Exception as e:
            if server is not None:
                server.logger.error('[spms] {} 失败: {}'.format(label, e), exc_info=True)
            try:
                _reply(server.get_console_command_source(), '§c{} 失败: {}'.format(label, e))
            except Exception:
                pass
    threading.Thread(target=wrapper, daemon=True, name='sidebar_boards-cmd').start()


def _cmd_dp(src: CommandSource, rebuild: bool = False):
    if not _dp_mode():
        _reply(src, '§c当前 realtime_mode = "{}"，不是 datapack。'
                    '把 config.json 里的 realtime_mode 改成 "datapack" 再重载插件。'.format(cfg('realtime_mode')))
        return
    if rebuild:
        _run_async(lambda: dp_setup(force_reseed=True), '重建数据包')
        _reply(src, '§a开始重建数据包，保留累计值；完成后可用 !!spms dp 检查')
    for line in dp_diagnose():
        _reply(src, line)


def _apply_board(board_id: str):
    """把当前榜切到 board_id：写 #board、setdisplay、强制整榜重画，并记住到配置。"""
    global _side_visible
    spec = _board_spec(board_id)
    _config['active_board'] = spec['id']
    try:
        save_config(server)
    except Exception:
        pass
    # 「打开」即显示：切榜时顺带把侧边栏放出来
    _set_visible(True)
    if _dp_mode():
        server.execute('scoreboard players set #board dsb_ctl {}'.format(spec['index']))
        server.execute('scoreboard players set #visible dsb_ctl 1')
        server.execute('scoreboard objectives setdisplay sidebar {}'.format(spec['display']))
        # 清掉本榜占位行的旧分数，render 会把当前前 N 名整榜重写
        for holder in _board_all_holders(spec):
            server.execute('scoreboard players reset {} {}'.format(holder, spec['display']))
        server.execute('function {}:render'.format(DP_NAME))
    else:
        # poll 模式：删旧建新展示项（顺带清掉另一个榜的占位行）再重画
        _clear_render_cache()
        setup_objectives()
        render(force=True)


def _cmd_switch_board(src: CommandSource, board_id: str):
    spec = _board_spec(board_id)
    if not _obj_ready:
        # 计分项还没建（auto_show=false，或服务端刚起来还没补建）。
        # start_boards() 在 datapack 模式下会等 save-all，不能占着执行器线程。
        def _start_then_switch():
            try:
                _start_boards_once()
            finally:
                _apply_board(board_id)
        _run_async(_start_then_switch, '建立榜单并切换')
        _reply(src, '§a正在建立榜单并切换到{}，稍后看状态'.format(spec['title']))
        return
    _apply_board(board_id)
    _reply(src, '§a已切换到{}（!!spms hide 隐藏，break/place/total 自由切换）'.format(spec['title']))


def _cmd_show(src: CommandSource):
    if not _obj_ready:
        # 计分项还没建（auto_show=false，或服务端刚起来还没补建）。
        # start_boards() 在 datapack 模式下会等 save-all，不能占着执行器线程。
        _run_async(_start_boards_once, '建立榜单')
        _reply(src, '§a正在建立榜单（datapack 模式需要等保存完成），稍后看状态')
        return
    show_sidebar()
    tick_once(force=True)
    render(force=True)
    _reply(src, '§a侧边栏已显示（当前：{}）'.format(_board_spec(_active_board())['title']))


def _cmd_hide(src: CommandSource):
    hide_sidebar()
    _reply(src, '§a侧边栏已隐藏（计分项和分数都保留；break/place/both/craft/drop/pick/kill/custom 随时打开）')


def _cmd_botboard(src: CommandSource, value: Optional[bool]):
    """游戏内控制纯假人榜单：打开后所有榜单只上榜 bot_ 假人。"""
    if value is None:
        state = '§a开启（只上榜 bot_ 假人）' if cfg('bot_only_mode') else '§c关闭（只上榜真人）'
        _reply(src, '§7纯假人榜单: {}'.format(state))
        _reply(src, '§7!!spms §fbotboard on §7- 打开（只上榜假人）    §7!!spms §fbotboard off §7- 关闭（恢复真人）')
        return
    _config['bot_only_mode'] = bool(value)
    try:
        save_config(server)
    except Exception:
        pass

    def _rebuild():
        # 先同步玩家状态：纯假人模式下假人此前被排除、没有 PlayerState，
        # 不先 sync 的话 seed 会按 0 导入，假人历史值全丢。
        sync_player_states(initial=True)
        if _dp_mode():
            dp_setup()          # 重新生成数据包（假人/真人进、出 seed 与排名候选）
        else:
            tick_once(force=True)
        render(force=True)

    _run_async(_rebuild, '重建榜单（纯假人榜单开关）')
    if value:
        _reply(src, '§a纯假人榜单已打开：所有榜单只上榜 bot_ 假人，正在重建...')
    else:
        _reply(src, '§a纯假人榜单已关闭：恢复只上榜真人，正在重建...')


def _cmd_refresh(src: CommandSource):
    changed = tick_once(force=True)
    render(force=True)
    _reply(src, '§a已请求服务端重画实时榜单' if _dp_mode() else '§a已重读统计文件并重画{}'.format('（数值有变化）' if changed else ''))


def register_commands(si: PluginServerInterface):
    si.register_command(
        Literal('!!spms').
        runs(lambda src: [ _reply(src, line) for line in _help_lines() ]).
        then(Literal('status').runs(lambda src: _run_async(lambda: _print_status(src), '读取榜单状态'))).
        then(Literal('debug').runs(lambda src: _print_debug(src))).
        then(Literal('dp').
             runs(lambda src: _cmd_dp(src)).
             then(Literal('rebuild').requires(_need_perm).runs(lambda src: _cmd_dp(src, True)))).
        then(Literal('break').requires(_need_perm).runs(lambda src: _cmd_switch_board(src, BOARD_BREAK))).
        then(Literal('place').requires(_need_perm).runs(lambda src: _cmd_switch_board(src, BOARD_PLACE))).
        then(Literal('both').requires(_need_perm).runs(lambda src: _cmd_switch_board(src, BOARD_BOTH))).
        then(Literal('craft').requires(_need_perm).runs(lambda src: _cmd_switch_board(src, BOARD_CRAFT))).
        then(Literal('drop').requires(_need_perm).runs(lambda src: _cmd_switch_board(src, BOARD_DROP))).
        then(Literal('pick').requires(_need_perm).runs(lambda src: _cmd_switch_board(src, BOARD_PICK))).
        then(Literal('kill').requires(_need_perm).runs(lambda src: _cmd_switch_board(src, BOARD_KILL))).
        then(Literal('custom').requires(_need_perm).runs(lambda src: _cmd_switch_board(src, BOARD_CUSTOM))).
        then(Literal('botboard').requires(_need_perm).
             runs(lambda src: _cmd_botboard(src, None)).
             then(Literal('on').runs(lambda src: _cmd_botboard(src, True))).
             then(Literal('off').runs(lambda src: _cmd_botboard(src, False)))).
        then(Literal('show').requires(_need_perm).runs(lambda src: _cmd_show(src))).
        then(Literal('hide').requires(_need_perm).runs(lambda src: _cmd_hide(src))).
        then(Literal('refresh').requires(_need_perm).runs(lambda src: _cmd_refresh(src)))
    )


# ===========================================================================
# MCDR 事件
# ===========================================================================

def on_load(si: PluginServerInterface, old_module):
    global server, _unloaded, _online, uuid_list, _boards_started
    server = si
    _unloaded = False
    _boards_started = False
    _stop_event.clear()
    _wake_event.clear()
    _start_done.clear()
    with _lock:
        uuid_list = {}
        _states.clear()
        _online = set()
    load_config(si)

    # ⚠ 这几件必须在 on_load 里【同步】做完。
    # MCDR 是在插件加载阶段收集命令树 / 帮助信息的，把 register_command 丢到别的线程里
    # 会注册不上（游戏里打 !!spms 没反应）。
    dp_load_index()
    refresh_uuid_list(si)
    register_commands(si)
    si.register_help_message('!!spms', '简单放置破坏榜：break/place/both/craft/drop/pick/kill/custom 八榜切换')
    _ensure_tick_thread()
    si.logger.info('[spms] 命令已注册：!!spms break / place / both / craft / drop / pick / kill / custom / botboard / show / hide / status / debug / dp / refresh')

    # 只有「慢活」丢到后台线程：datapack 模式要 save-all 并等一会儿才回填，
    # 而 on_load 跑在 MCDR 的任务执行器线程上，在那里阻塞会死锁（参考插件也踩过这个坑）。
    if cfg('auto_show'):
        threading.Thread(target=_deferred_start, name='sidebar_boards-start', daemon=True).start()
    else:
        _start_done.set()


def _server_ready() -> bool:
    """
    MCDR 是【先加载插件，再启动服务端】的，所以 on_load 时服务端往往还没起来。
    这时往 stdin 发 scoreboard 指令会被丢掉（"Server has been terminated"），
    侧边栏就永远建不出来。所以建榜必须等服务端真正就绪。
    is_server_running() 只代表进程活着，is_server_startup() 才是"已经 Done 了能收命令"。
    """
    if server is None:
        return False
    try:
        return bool(server.is_server_running()) and bool(server.is_server_startup())
    except Exception:
        return False


def _deferred_start():
    """
    等服务端就绪后建榜。datapack 模式下这里会有 save-all 等待，所以必须在这个后台线程里做，
    不能放在 on_load（那会阻塞 MCDR 的任务执行器线程，触发 watchdog）。
    """
    try:
        if not cfg('auto_show'):
            return
        deadline = time.monotonic() + 1800.0
        while not _unloaded and time.monotonic() < deadline:
            if _server_ready():
                break
            time.sleep(0.5)
        if _unloaded:
            return
        if not _server_ready():
            server.logger.warning('[spms] 等了 30 分钟服务端还没就绪，先不建榜；'
                                  '服务端起来后会自动补建')
            return
        _start_boards_once()
    except Exception as e:
        server.logger.error('[spms] 初始化榜单失败: {}'.format(e), exc_info=True)
    finally:
        _start_done.set()


def _start_boards_once():
    """只建一次榜（后台线程 / on_server_startup / !!spms show 可能同时想建）"""
    global _boards_started
    with _tick_lock:
        if _boards_started:
            return
        start_boards()
        _boards_started = _obj_ready


def on_unload(si: PluginServerInterface):
    global _unloaded
    _unloaded = True
    stop_boards()
    with _tick_lock:
        try:
            si.execute('scoreboard objectives setdisplay sidebar')
            if _dp_mode():
                # 热重载只暂停函数，保留计分项、初始化标记与尚未消费的事件，防止重复导入。
                si.execute('scoreboard players set #enabled dsb_ctl 0')
                for tag in ('load', 'tick'):
                    dp_write_tag(tag + '.json', json.dumps({'values': []}) + '\n')
            else:
                # poll 模式的展示计分项（dsb_brd）连同占位行一起删掉
                si.execute('scoreboard objectives remove {}'.format(OBJ_BOARD))
        except Exception as e:
            si.logger.error('[spms] 停止数据包失败: {}'.format(e))


def on_info(si: PluginServerInterface, info: Info):
    global _dp_last_error
    if getattr(info, 'is_from_server', False) and not getattr(info, 'is_player', False):
        content = info.content
        if re.search(r'\bSaved the (?:game|world)\b', content):
            _dp_save_done.set()
        if DP_NAME in content and re.search(r'(?:Failed|Couldn.t|Error).*?(?:function|pack)', content, re.IGNORECASE):
            _dp_last_error = content


def on_server_stop(si: PluginServerInterface, return_code: int):
    global _boards_started, _obj_ready, _dp_seeded
    _dp_save_done.set()
    with _tick_lock:
        _boards_started = False
        _obj_ready = False
        _dp_seeded = False


def on_server_startup(si: PluginServerInterface):
    """
    MCDR 先加载插件、再启动服务端。服务端真正就绪（handler 认出 Done）时补建一次榜，
    比 _deferred_start 的轮询更及时，也顺便给它兜底。
    """
    if _unloaded or not cfg('auto_show'):
        return
    _start_done.set()
    if _boards_started:
        return
    _run_async(_start_boards_once, '服务端就绪后建榜')


def on_player_joined(si: PluginServerInterface, player: str, info: Info):
    with _lock:
        _online.add(player)
    # 参考插件也是在玩家进服时刷新 uuid 映射
    try:
        refresh_uuid_list(si)
    except Exception:
        pass
    _wake_event.set()


def on_player_left(si: PluginServerInterface, player: str):
    with _lock:
        _online.discard(player)
