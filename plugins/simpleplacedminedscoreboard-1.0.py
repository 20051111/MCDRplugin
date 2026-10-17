# -*- coding: utf-8 -*-
"""
简单放置破坏榜 (SimplePlacedMinedScoreboard) —— 服务端 Sidebar「破坏总和榜 / 放置总和榜」

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

渲染方式说明（见 README「侧边栏怎么画的」一节）：
    原版一个 display slot 只能挂一个计分项，team 槽位只是「按队伍颜色区分可见性」，
    并不是把多个榜单叠起来。所以「同时显示两个榜单」使用一个 dummy 计分项，
    用假玩家名占行，标题行/数据行都靠 scoreboard players display name 写入文本。
    名次顺序由分数控制，分数列用 numberformat blank 隐藏（需要 MC 1.20.3+）。
    在 Minecraft 1.20.4 中由数据包每 tick 汇总增量并用函数宏更新双榜；无需 RCON。
    stats JSON 仅首次导入历史值，不作为实时数据源。sidebar_layout = "rotate" 可轮播。
"""

PLUGIN_METADATA = {
    # 内部 id 保持不变：改 id 会让 config/数据包索引/计分项槽位全部重来，
    # 世界里的 dsb_* / dsbm* / dsbu* 计分项和历史分数也会变成孤儿。
    'id': 'sidebar_boards',
    'version': '1.3.0',
    # MCDR 的插件名只能是单个字符串（description 才支持多语言），所以中英一起写
    'name': '简单放置破坏榜 (SimplePlacedMinedScoreboard)',
    'description': {
        'en_us': 'Server-tick sidebar boards: total blocks mined / placed, with a total row above each board',
        'zh_cn': '服务端逐 tick 的破坏总和榜 + 放置总和榜（各自上方一行总破坏 / 总放置）',
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

# ===========================================================================
# 常量
# ===========================================================================

# 与参考插件 stats_helper/constants.py 的 AllTargetTag = '#all' 同名同义
ALL_TAG = '#all'

# 统计分类（参考插件里叫 cls，命令行是 minecraft.<cls>:minecraft.<target>）
MINED_CLASS = 'mined'   # 破坏榜用这个
USED_CLASS = 'used'     # 放置榜用这个

# 计分项名：<=16 字符，只能 [A-Za-z0-9_.-]
OBJ_STACKED = 'dsb_rank'    # stacked 布局：一个 dummy 项装两个榜单
OBJ_BREAK = 'dsb_break'     # rotate 布局：破坏榜
OBJ_PLACE = 'dsb_place'     # rotate 布局：放置榜

# 侧边栏最多显示 15 行（原版限制）
SIDEBAR_RENDER_LIMIT = 15

# 假玩家名前缀，用来占行。不能以 '#' 开头（原版规定 '#' 开头的假玩家不显示在侧边栏）
HOLDER_HEAD_BREAK = 'dsb_h1'
HOLDER_HEAD_PLACE = 'dsb_h2'
HOLDER_TOTAL_BREAK = 'dsb_t1'    # 最顶上：总破坏
HOLDER_TOTAL_PLACE = 'dsb_t2'    # 放置榜上方：总放置
HOLDER_BREAK_FMT = 'dsb_b{:02d}'
HOLDER_PLACE_FMT = 'dsb_p{:02d}'

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
    'config_version': 2,
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
    'sidebar_layout': 'stacked',     # stacked | rotate
    'board_top_n': 6,                # 每个榜显示前几名（stacked 下 2 + 2*N 需 <= 15）
    'rotate_interval_seconds': 10,   # 仅 rotate 布局：两个榜轮播间隔
    'hide_number_column': True,      # stacked 布局：用 numberformat blank 隐藏分数列（需 1.20.3+）
    'sidebar_title': '放置/破坏榜',   # 侧边栏最上方的计分项标题（rotate 下是各自标题）
    'break_title': '破坏总和榜',
    'place_title': '放置总和榜',
    'show_rank_number': True,        # 行内是否带 #1 #2 名次
    # 侧边栏「总破坏 / 总放置」两行合计（所有玩家的分数之和），分别排在各自榜单标题上方
    'show_total_row': True,
    'total_break_label': '总破坏',
    'total_place_label': '总放置',
    # 所有统计数字的千分位分隔符；留空 = 不加分隔（默认）。
    # 想恢复逗号就填 ","（也支持 " " / "." 之类）
    'thousands_separator': '',
    'title_color': 'gold',
    'name_color': 'white',
    'value_color': 'aqua',

    # ---- 玩家过滤 ----
    # 规格：排除所有以 bot_ 开头的玩家
    'bot_name_prefixes': ['bot_'],
    # 额外的正则黑名单（参考插件里是 ['^bot.*','Steve','Alex']，这里默认留空，按规格只用 bot_ 前缀）
    'bot_name_patterns': [],

    # ---- 其他 ----
    'auto_show': True,               # 插件加载后自动开榜
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
_last_render: Dict[str, Tuple[int, str]] = {}          # stacked: holder -> (score, component_json)
_last_scores: Dict[str, Dict[str, int]] = {}           # rotate : obj -> {name: value}
_rotate_current = OBJ_BREAK
_active_layout = 'stacked'
_last_rotate_switch = 0.0
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
                 'stamp', 'loaded')

    def __init__(self, name: str, uuid: str):
        self.name = name
        self.uuid = uuid
        self.used: Dict[str, int] = {}        # 上一次的 used 快照（整份，不只交集）
        self.mined: Dict[str, int] = {}       # 上一次的 mined 快照（整份，用于回填准则项）
        self.placed: int = 0                  # 放置总和 = 初始值 + 逐 tick 累计的 used 新增
        self.break_total: int = 0             # 破坏总和 = sum(mined.values())
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
    """[(玩家名, uuid)]，已排除 bot_ 前缀"""
    with _lock:
        items = list(uuid_list.items())
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
DP_OBJ_BREAK = 'dsb_brk'
DP_OBJ_PLACE = 'dsb_plc'
DP_OBJ_TOTAL_BREAK = 'dsb_tbrk'   # 全服破坏合计（侧边栏最顶那一行用）
DP_OBJ_TOTAL_PLACE = 'dsb_tplc'   # 全服放置合计
DP_OBJ_FMT = 'dsb_fmt'            # 千分位格式化用的中间计分项（#fmt / #g1 / #g2 / #g3 / #1000 / #1000000）
DP_OBJ_BASE = 'dsb_B'  # 旧版名称，仅用于兼容诊断
DP_INDEX_FILE = 'datapack_index.json'
DP_SCHEMA = 2
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


def effective_layout() -> str:
    layout = str(cfg('sidebar_layout')).lower()
    if _dp_mode() and _mc_version_tuple() < (1, 20, 3):
        return 'rotate'
    return 'rotate' if layout == 'rotate' else 'stacked'


def rotate_objs() -> Tuple[str, str]:
    return (DP_OBJ_BREAK, DP_OBJ_PLACE) if _dp_mode() else (OBJ_BREAK, OBJ_PLACE)


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


def dp_collect() -> Dict[str, Dict[str, Set[str]]]:
    # 不再由每位玩家的历史交集决定生成哪些函数；完整注册表覆盖首次出现的方块。
    names = set(name for name, _ in known_players()) | set(_online)
    result = {}
    for name in sorted(names):
        if is_bot(name):
            continue
        if not re.fullmatch(r'[A-Za-z0-9_]{1,16}', name):
            raise ValueError('玩家名不能安全用于原版计分板命令: {!r}'.format(name))
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
    load = []
    dummy = [DP_OBJ_BREAK, DP_OBJ_PLACE, DP_OBJ_TOTAL_BREAK, DP_OBJ_TOTAL_PLACE, DP_OBJ_FMT,
             'dsb_init', 'dsb_ctl', 'dsb_work', 'dsb_owner', 'dsb_value']
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

    # 准则分数是“新增计数”，游戏 awardStat 会做加法，并非把历史统计绝对赋值。
    # 只回填汇总值与“曾挖过”标记；不把历史值填进即将再次累计的增量计数器。
    for name in names:
        state = _states.get(name)
        brk = _dp_score(state.break_total if state else 0)
        plc = _dp_score(state.placed if state else 0)
        # init=1 表示玩家已被原生 tick 捕获、尚未导入历史；保留这段先到的在线增量。
        seed = ['execute unless score {} dsb_init matches 1 run scoreboard players set {} {} 0'.format(name, name, DP_OBJ_BREAK),
                'execute unless score {} dsb_init matches 1 run scoreboard players set {} {} 0'.format(name, name, DP_OBJ_PLACE),
                'scoreboard players add {} {} {}'.format(name, DP_OBJ_BREAK, brk),
                'scoreboard players add {} {} {}'.format(name, DP_OBJ_PLACE, plc)]
        for item in used:
            if state and state.mined.get(item, 0) > 0:
                seed.append('scoreboard players set {} dsbh{} 1'.format(name, dp_slot(item)))
        seed.append('scoreboard players set {} dsb_init {}'.format(name, DP_SCHEMA))
        rel = 'seed/' + dp_slug(name)
        dp_write_file(rel + '.mcfunction', '\n'.join(seed) + '\n')
        load.append('execute unless score {} dsb_init matches {} run function {}:{}'.format(name, DP_SCHEMA, DP_NAME, rel))

    new_player = ['scoreboard players set @s {} 0'.format(DP_OBJ_BREAK),
                  'scoreboard players set @s {} 0'.format(DP_OBJ_PLACE),
                  'scoreboard players set @s dsb_init 1',
                  'scoreboard players set #dirty dsb_ctl 1']
    dp_write_file('new_player.mcfunction', '\n'.join(new_player) + '\n')
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

    if effective_layout() == 'stacked':
        update.append('execute if score #visible dsb_ctl matches 1 if score #dirty dsb_ctl matches 1 run function {}:render'.format(DP_NAME))
        load.extend(_dp_write_render(names))
    else:
        dp_write_file('render.mcfunction', '# rotate: 原版计分板自动显示实时数值\n')
        for obj, title in ((DP_OBJ_BREAK, cfg('break_title')), (DP_OBJ_PLACE, cfg('place_title'))):
            load.append('scoreboard objectives modify {} displayname {}'.format(obj, _title_component(title)))
        if _side_visible:
            load.append('scoreboard objectives setdisplay sidebar {}'.format(DP_OBJ_BREAK))
    load += ['scoreboard players set #visible dsb_ctl {}'.format(int(_side_visible)),
             'scoreboard players set #enabled dsb_ctl 1',
             'scoreboard players set #ready dsb_ctl {}'.format(DP_SCHEMA)]
    dp_write_file('load.mcfunction', '\n'.join(load) + '\n')
    dp_write_file('update.mcfunction', '\n'.join(update) + '\n')
    dp_write_file('tick.mcfunction', 'execute if score #enabled dsb_ctl matches 1 run function {}:update\n'.format(DP_NAME))
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


def _dp_write_render(names: List[str]) -> List[str]:
    total_on = bool(cfg('show_total_row'))
    # 侧边栏上限 15 行：合计行各 1 行 + 榜单标题各 1 行 -> 每榜 (15-4)//2 = 5 行
    rows_budget = SIDEBAR_RENDER_LIMIT - (4 if total_on else 2)
    limit = min(max(0, int(cfg('board_top_n'))), rows_budget // 2, len(names))
    head_break_order = 3 + 2 * limit if total_on else 2 * limit + 2
    head_place_order = 1 + limit if total_on else limit + 1
    total_break_order = 4 + 2 * limit
    total_place_order = 2 + limit
    grouped = bool(_dp_grouping())

    def value_lines(source: str, target: str) -> List[str]:
        """把 source（"Alice dsb_brk" / "#total dsb_tbrk"）写进 storage 路径 target。"""
        if not grouped:
            return ['execute store result storage {}:render {} int 1 run scoreboard players get {}'.format(DP_NAME, target, source)]
        return ['scoreboard players operation #fmt {} = {}'.format(DP_OBJ_FMT, source),
                'function {}:fmt/format'.format(DP_NAME),
                'data modify storage {}:render {} set from storage {}:fmt out'.format(DP_NAME, target, DP_NAME)]

    load = ['scoreboard objectives add {} dummy {}'.format(OBJ_STACKED, _title_component(cfg('sidebar_title'))),
            'scoreboard objectives modify {} displayname {}'.format(OBJ_STACKED, _title_component(cfg('sidebar_title')))]
    if cfg('hide_number_column'):
        load.append('scoreboard objectives modify {} numberformat blank'.format(OBJ_STACKED))
    all_holders = [HOLDER_HEAD_BREAK, HOLDER_HEAD_PLACE, HOLDER_TOTAL_BREAK, HOLDER_TOTAL_PLACE] + \
                  [fmt.format(i) for fmt in (HOLDER_BREAK_FMT, HOLDER_PLACE_FMT) for i in range(1, 8)]
    for holder in all_holders:
        load += ['scoreboard players reset {} {}'.format(holder, OBJ_STACKED),
                 'scoreboard players reset {} dsb_owner'.format(holder),
                 'scoreboard players reset {} dsb_value'.format(holder)]
    for holder, order, title in ((HOLDER_HEAD_BREAK, head_break_order, cfg('break_title')),
                                  (HOLDER_HEAD_PLACE, head_place_order, cfg('place_title'))):
        load += ['scoreboard players set {} {} {}'.format(holder, OBJ_STACKED, order),
                 'scoreboard players display name {} {} {}'.format(holder, OBJ_STACKED, _title_component(title))]
    # 布局（自上而下）：总破坏 / 破坏榜（标题+行） / 总放置 / 放置榜（标题+行）
    totals = ((HOLDER_TOTAL_BREAK, total_break_order, cfg('total_break_label'), DP_OBJ_TOTAL_BREAK, 'break'),
              (HOLDER_TOTAL_PLACE, total_place_order, cfg('total_place_label'), DP_OBJ_TOTAL_PLACE, 'place'))
    if total_on:
        for holder, order, label, _obj, _suffix in totals:
            load += ['scoreboard players set {} {} {}'.format(holder, OBJ_STACKED, order),
                     'scoreboard players display name {} {} {}'.format(holder, OBJ_STACKED, _json_component({'text': '', 'extra': [
                         {'text': str(label) + ' ', 'color': cfg('title_color')},
                         {'text': '0', 'color': cfg('value_color')}]}))]
    render = []
    if total_on:
        # 全服合计 = 所有候选玩家 dsb_brk / dsb_plc 之和，每次重画时重新算
        render.append('scoreboard players set #total {} 0'.format(DP_OBJ_TOTAL_BREAK))
        render.append('scoreboard players set #total {} 0'.format(DP_OBJ_TOTAL_PLACE))
        for name in names:
            render.append('scoreboard players operation #total {} += {} {}'.format(DP_OBJ_TOTAL_BREAK, name, DP_OBJ_BREAK))
            render.append('scoreboard players operation #total {} += {} {}'.format(DP_OBJ_TOTAL_PLACE, name, DP_OBJ_PLACE))
    for kind, objective, holder_fmt in (('b', DP_OBJ_BREAK, HOLDER_BREAK_FMT), ('p', DP_OBJ_PLACE, HOLDER_PLACE_FMT)):
        for name in names:
            render.append('scoreboard players operation {} dsb_work = {} {}'.format(name, name, objective))
        for rank in range(1, limit + 1):
            holder = holder_fmt.format(rank)
            render.append('scoreboard players set #best dsb_work {}'.format(SCORE_MIN))
            for name in names:
                render.append('scoreboard players operation #best dsb_work > {} dsb_work'.format(name))
            render.append('scoreboard players set #picked dsb_ctl 0')
            # 按名字升序挑选并列第一，榜外玩家和离线玩家都参与，不截断候选集合。
            for player_id, name in enumerate(names, 1):
                rel = 'render/{}_{}_{}'.format(kind, rank, dp_slug(name))
                render.append('execute if score #picked dsb_ctl matches 0 if score {} dsb_work = #best dsb_work run function {}:{}'.format(name, DP_NAME, rel))
                write_rel = rel + '_write'
                chosen = ['scoreboard players set #picked dsb_ctl 1',
                          'scoreboard players set {} dsb_work {}'.format(name, SCORE_MIN),
                          'execute unless score {} dsb_owner matches {} run function {}:{}'.format(holder, player_id, DP_NAME, write_rel),
                          'execute unless score {} dsb_value = {} {} run function {}:{}'.format(holder, name, objective, DP_NAME, write_rel)]
                dp_write_file(rel + '.mcfunction', '\n'.join(chosen) + '\n')
                write = ['data modify storage {}:render row set value {}'.format(DP_NAME, json.dumps({'name': name}, separators=(',', ':')))]
                write += value_lines('{} {}'.format(name, objective), 'row.value')
                write += ['function {}:render/row_{}{} with storage {}:render row'.format(DP_NAME, kind, rank, DP_NAME),
                          'scoreboard players set {} dsb_owner {}'.format(holder, player_id),
                          'scoreboard players operation {} dsb_value = {} {}'.format(holder, name, objective)]
                dp_write_file(write_rel + '.mcfunction', '\n'.join(write) + '\n')
            # 1.20.4 函数宏把数值写成实际文本，避免未解析的 score 组件在侧边栏显示为空。
            extra = []
            if cfg('show_rank_number'):
                extra.append({'text': '#{} '.format(rank), 'color': RANK_COLORS[min(rank - 1, len(RANK_COLORS) - 1)]})
            extra += [{'text': '$(name) ', 'color': cfg('name_color')},
                      {'text': '$(value)', 'color': cfg('value_color')}]
            order = head_break_order - rank if kind == 'b' else head_place_order - rank
            row = ['scoreboard players set {} {} {}'.format(holder, OBJ_STACKED, order),
                   '$scoreboard players display name {} {} {}'.format(holder, OBJ_STACKED, _json_component({'text': '', 'extra': extra}))]
            dp_write_file('render/row_{}{}.mcfunction'.format(kind, rank), '\n'.join(row) + '\n')
    if total_on:
        # 两个合计行各自显示自己的数字；只在合计真的变了才重写这一行
        for holder, order, label, objective, suffix in totals:
            render.append('execute unless score {} dsb_value = #total {} run function {}:render/total_{}_write'.format(
                holder, objective, DP_NAME, suffix))
            write = value_lines('#total {}'.format(objective), 'total.value')
            write += ['function {}:render/row_total_{} with storage {}:render total'.format(DP_NAME, suffix, DP_NAME),
                      'scoreboard players operation {} dsb_value = #total {}'.format(holder, objective)]
            dp_write_file('render/total_{}_write.mcfunction'.format(suffix), '\n'.join(write) + '\n')
            row = ['scoreboard players set {} {} {}'.format(holder, OBJ_STACKED, order),
                   '$scoreboard players display name {} {} {}'.format(holder, OBJ_STACKED, _json_component({'text': '', 'extra': [
                       {'text': str(label) + ' ', 'color': cfg('title_color')},
                       {'text': '$(value)', 'color': cfg('value_color')}]}))]
            dp_write_file('render/row_total_{}.mcfunction'.format(suffix), '\n'.join(row) + '\n')
    dp_write_file('render.mcfunction', '\n'.join(render) + '\n')
    if _side_visible:
        load += ['function {}:render'.format(DP_NAME), 'scoreboard objectives setdisplay sidebar {}'.format(OBJ_STACKED)]
    return load


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
            '§7布局: §f{}   §7数据包: §f{}'.format(effective_layout(), dp_root()),
            '§7统计/排名/显示: §a服务端逐 tick；无需 RCON、无需周期存档',
            '§7注册方块: §f{}   §7used 准则: §f{}   §7排名玩家: §f{}'.format(len(_dp_created_m), len(_dp_created_u), len(_dp_players)),
            '§7最近错误: §f{}'.format(_dp_last_error or '(无)'),
            '§7可用 /datapack list 核对加载，用 /scoreboard players get #ready dsb_ctl 核对值为 2',
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
            state.baseline = 0
            state.loaded = False
            state.stamp = stamp
            return True
        return False

    used = stat_class_map(stats, USED_CLASS)
    mined = stat_class_map(stats, MINED_CLASS)

    changed = False
    if _dp_mode():
        # 数据包只在首次初始化时导入历史总和；在线增量完全由服务端维护。
        if initial or not state.loaded:
            state.break_total = stat_class_sum(stats, MINED_CLASS)
            state.placed = initial_placed_total(stats, used, mined)
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

def board_rows(limit: Optional[int] = None) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    """返回 (破坏榜, 放置榜)，均按数值降序，同分按玩家名升序"""
    with _lock:
        states = list(_states.values())
    brk = [(s.name, s.break_total) for s in states]
    plc = [(s.name, s.placed) for s in states]
    brk.sort(key=lambda x: (-x[1], x[0]))
    plc.sort(key=lambda x: (-x[1], x[0]))
    top_n = max(0, int(cfg('board_top_n'))) if limit is None else max(0, int(limit))
    return brk[:top_n], plc[:top_n]


# ===========================================================================
# 侧边栏渲染
#
# 关键点：原版一个 display slot 只能挂一个计分项（sidebar.team.<color> 只是按队伍颜色
# 控制可见性，不会把多个榜单堆起来）。所以「同时显示两个总和榜」用一个 dummy 计分项：
#   假玩家名占行 -> scoreboard players set 控制排序
#                -> scoreboard players display name 写入这一行显示的文本
#                -> scoreboard objectives modify <obj> numberformat blank 隐藏分数列
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


def _row_component(rank: Optional[int], name: str, value: int) -> str:
    """名次 + 玩家名 + 数值（数值直接写进行文本里，因为分数列是隐藏的）"""
    extra: List[Dict[str, Any]] = []
    if rank is not None and cfg('show_rank_number'):
        extra.append({'text': '#{} '.format(rank), 'color': RANK_COLORS[min(rank - 1, len(RANK_COLORS) - 1)]})
    extra.append({'text': str(name) + ' ', 'color': cfg('name_color')})
    extra.append({'text': str(value), 'color': cfg('value_color')})
    return _json_component({'text': '', 'extra': extra})


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
    """建计分项。切换布局时要把另一种布局的计分项删掉，避免残留。"""
    global _obj_ready, _rotate_current, _active_layout
    layout = effective_layout()
    _active_layout = layout

    if _dp_mode():
        # 数据包 load 函数负责建项与显示，不能在异步 reload 后又删除它们。
        _rotate_current = rotate_objs()[0]
        _last_rotate_switch_force()
        _obj_ready = True
        return

    if layout == 'rotate':
        _remove_objectives([OBJ_STACKED, OBJ_BREAK, OBJ_PLACE])
        server.execute('scoreboard objectives add {} dummy {}'.format(OBJ_BREAK, _title_component(cfg('break_title'))))
        server.execute('scoreboard objectives add {} dummy {}'.format(OBJ_PLACE, _title_component(cfg('place_title'))))
        _rotate_current = OBJ_BREAK
        _last_rotate_switch_force()
    else:
        _remove_objectives([OBJ_BREAK, OBJ_PLACE, OBJ_STACKED])
        server.execute('scoreboard objectives add {} dummy {}'.format(OBJ_STACKED, _title_component(cfg('sidebar_title'))))
        if cfg('hide_number_column'):
            # 隐藏分数列（1.20.3+）。否则名次排序用的分数会显示出来。
            server.execute('scoreboard objectives modify {} numberformat blank'.format(OBJ_STACKED))

    _obj_ready = True
    if _side_visible:
        show_sidebar()


def _last_rotate_switch_force():
    global _last_rotate_switch
    _last_rotate_switch = time.monotonic()


def show_sidebar():
    global _side_visible
    _side_visible = True
    if not _obj_ready:
        return
    if _dp_mode():
        server.execute('scoreboard players set #visible dsb_ctl 1')
        server.execute('function {}:render'.format(DP_NAME))
    if effective_layout() == 'rotate':
        server.execute('scoreboard objectives setdisplay sidebar {}'.format(_rotate_current))
    else:
        server.execute('scoreboard objectives setdisplay sidebar {}'.format(OBJ_STACKED))


def hide_sidebar():
    global _side_visible
    _side_visible = False
    if _dp_mode():
        server.execute('scoreboard players set #visible dsb_ctl 0')
    server.execute('scoreboard objectives setdisplay sidebar')


def render(force: bool = False):
    """按当前布局渲染；只有真正变化的行才发指令（防止刷屏 / 无谓的指令洪流）"""
    global _stat_renders
    if server is None or not _obj_ready:
        return
    if not _side_visible:
        return
    if _dp_mode():
        if force:
            server.execute('function {}:render'.format(DP_NAME))
        return
    if effective_layout() == 'rotate':
        render_rotate(force=force)
    else:
        render_stacked(force=force)
    _stat_renders += 1


def render_stacked(force: bool = False):
    global _last_render
    brk, plc = board_rows()

    # 视觉顺序：破坏榜标题 -> 破坏榜数据行 -> 放置榜标题 -> 放置榜数据行
    desired: List[Tuple[str, int, str]] = []          # (holder, score, component)
    desired.append((HOLDER_HEAD_BREAK, 0, _title_component(cfg('break_title'))))
    for i, (name, value) in enumerate(brk):
        desired.append((HOLDER_BREAK_FMT.format(i + 1), 0, _row_component(i + 1, name, value)))
    desired.append((HOLDER_HEAD_PLACE, 0, _title_component(cfg('place_title'))))
    for i, (name, value) in enumerate(plc):
        desired.append((HOLDER_PLACE_FMT.format(i + 1), 0, _row_component(i + 1, name, value)))

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
        server.execute('scoreboard players reset {} {}'.format(holder, OBJ_STACKED))
    for holder in to_write:
        score, component = plan[holder]
        server.execute('scoreboard players set {} {} {}'.format(holder, OBJ_STACKED, score))
        server.execute('scoreboard players display name {} {} {}'.format(holder, OBJ_STACKED, component))

    with _lock:
        _last_render = plan

    if to_remove or to_write:
        _log('渲染 stacked：写 {} 行，清 {} 行（破坏榜 {} 人 / 放置榜 {} 人）'.format(
            len(to_write), len(to_remove), len(brk), len(plc)))


def render_rotate(force: bool = False):
    """两个真实计分项，用真实玩家名，分数列直接显示数值；按 rotate_interval_seconds 轮播"""
    global _last_scores
    if _dp_mode():
        # 数据包模式：dsb_brk / dsb_plc 的分数由服务端每 tick 自己维护，插件不写分数
        return
    obj_break, obj_place = rotate_objs()
    # rotate 下不需要排序分数，直接用原版“取分数最高的 15 个”的行为
    brk, plc = board_rows(limit=SIDEBAR_RENDER_LIMIT)
    all_rows = {}
    for name, value in brk:
        all_rows.setdefault(name, {})[obj_break] = value
    for name, value in plc:
        all_rows.setdefault(name, {})[obj_place] = value

    with _lock:
        previous = {obj: dict(m) for obj, m in _last_scores.items()}

    for obj in (obj_break, obj_place):
        prev = previous.get(obj, {})
        for name, per_obj in all_rows.items():
            value = per_obj.get(obj)
            if value is None:
                continue
            if force or prev.get(name) != value:
                server.execute('scoreboard players set {} {} {}'.format(name, obj, value))

    with _lock:
        _last_scores = {obj_break: {}, obj_place: {}}
        for name, per_obj in all_rows.items():
            for obj, value in per_obj.items():
                _last_scores[obj][name] = value


def rotate_tick():
    """rotate 布局的轮播"""
    global _rotate_current
    if not _side_visible or not _obj_ready:
        return
    interval = max(1.0, float(cfg('rotate_interval_seconds')))
    if time.monotonic() - _last_rotate_switch < interval:
        return
    _last_rotate_switch_force()
    obj_break, obj_place = rotate_objs()
    _rotate_current = obj_place if _rotate_current == obj_break else obj_break
    server.execute('scoreboard objectives setdisplay sidebar {}'.format(_rotate_current))


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
        if effective_layout() == 'rotate':
            rotate_tick()
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

    if effective_layout() == 'rotate':
        rotate_tick()
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
    _side_visible = True
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
    if effective_layout() != 'rotate':
        if 2 + 2 * top_n > SIDEBAR_RENDER_LIMIT:
            server.logger.warning(
                '[spms] board_top_n={} 会产生 {} 行，超过侧边栏 {} 行的上限，'
                '已将实时双榜限制到每榜最多 {} 行。'.format(
                    top_n, 2 + 2 * top_n, SIDEBAR_RENDER_LIMIT, (SIDEBAR_RENDER_LIMIT - 2) // 2))
    server.logger.info('[spms] 已启动：模式 {}，布局 {}；统计目录 {}；识别玩家 {} 名（已排除 bot_ 前缀）'.format(
        cfg('realtime_mode'), effective_layout(), stats_dir(), len(known_players())))


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


def _print_status(src: CommandSource):
    if _dp_mode():
        if dp_can_read():
            dp_read_totals()
        for line in dp_diagnose():
            _reply(src, line)
        if not dp_can_read():
            _reply(src, '§7实时排名见侧边栏；未开启 RCON 时不把本地历史快照当作实时值显示。')
            return
        brk, plc = board_rows()
        for title, rows in ((cfg('break_title'), brk), (cfg('place_title'), plc)):
            _reply(src, '§6---- {} ----'.format(title))
            for line in _status_rows(rows):
                _reply(src, line)
        return
    brk, plc = board_rows()
    with _lock:
        n_states = len(_states)
        n_online = len(_online)
        n_uuid = len(uuid_list)
    lines = [
        '§6======== 简单放置破坏榜 状态 ========',
        '§7实时方式: §f{}   §7布局: §f{}   §7侧边栏: {}'.format(
            cfg('realtime_mode'), effective_layout(), '§a显示中' if _side_visible else '§c已隐藏'),
        '§7统计目录: §f{}'.format(stats_dir()),
        '§7已识别玩家: §f{}   §7已排除 bot_: §f{}   §7在线: §f{}'.format(
            n_uuid, n_uuid - n_states, n_online),
        '§7逐 tick: §f{}ms   §7扫描次数: §f{}   §7读取次数: §f{}   §7渲染次数: §f{}'.format(
            cfg('tick_interval_ms'), _stat_scans, _stat_reads, _stat_renders),
        '§7主动保存间隔: §f{}s{}'.format(
            cfg('save_all_interval_seconds'),
            '' if cfg('save_all_interval_seconds') else ' §8(关闭：数据只能等原版自动保存，约 5 分钟)'),
    ]
    lines.append('§6---- {} ----'.format(cfg('break_title')))
    lines.extend(_status_rows(brk))
    lines.append('§6---- {} ----'.format(cfg('place_title')))
    lines.extend(_status_rows(plc))
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


HELP_LINES = [
    '§6======== 简单放置破坏榜 ========',
    '§7侧边栏同时显示：§e破坏总和榜§7（sum minecraft:mined）§7+ §e放置总和榜§7（各自 used ∩ mined）',
    '§7!!spms §fstatus §7- 查看榜单与运行状态',
    '§7!!spms §fdebug §7- 诊断路径 / 文件数 / RCON / 每个玩家的口径',
    '§7!!spms §fdp §7- 数据包模式诊断（模式、pack_format、RCON、生成的文件）',
    '§7!!spms §fdp rebuild §7- 重建实时数据包（保留已累计分数）',
    '§7!!spms §fshow §7- 显示侧边栏        §7!!spms §fhide §7- 隐藏侧边栏（不删分数）',
    '§7!!spms §frefresh §7- 立即重画（实时模式由服务端自动更新）',
    '§8需要权限等级 2 才能 show/hide/refresh/dp rebuild。',
]


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
    _reply(src, '§a侧边栏已显示')


def _cmd_hide(src: CommandSource):
    hide_sidebar()
    _reply(src, '§a侧边栏已隐藏（计分项和分数都保留）')


def _cmd_refresh(src: CommandSource):
    changed = tick_once(force=True)
    render(force=True)
    _reply(src, '§a已请求服务端重画实时榜单' if _dp_mode() else '§a已重读统计文件并重画{}'.format('（数值有变化）' if changed else ''))


def register_commands(si: PluginServerInterface):
    si.register_command(
        Literal('!!spms').
        runs(lambda src: [ _reply(src, line) for line in HELP_LINES ]).
        then(Literal('status').runs(lambda src: _run_async(lambda: _print_status(src), '读取榜单状态'))).
        then(Literal('debug').runs(lambda src: _print_debug(src))).
        then(Literal('dp').
             runs(lambda src: _cmd_dp(src)).
             then(Literal('rebuild').requires(_need_perm).runs(lambda src: _cmd_dp(src, True)))).
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
    si.register_help_message('!!spms', '简单放置破坏榜：破坏/放置总和双榜')
    _ensure_tick_thread()
    si.logger.info('[spms] 命令已注册：!!spms status / debug / dp / show / hide / refresh')

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
                for obj in (OBJ_STACKED, OBJ_BREAK, OBJ_PLACE):
                    si.execute('scoreboard objectives remove {}'.format(obj))
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
