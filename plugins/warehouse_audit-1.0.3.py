# -*- coding: utf-8 -*-
"""
仓库审计日志 (WarehouseAuditLog)
================================

监控指定坐标范围内仓库容器（箱子/木桶等）的交互事件，记录
玩家 ID、时间戳、物品 ID、数量及坐标到 SQLite，异步队列写入避免卡服，
并提供 !!wh export 指令导出 CSV 用于分析。

单文件 MCDReforged 插件，仅依赖 Python 标准库 + MCDR API。

数据流 (默认配置: 只记取出, 不记玩家ID)::

    玩家取出物品 ──► 轮询线程 (RCON 只读: data get block)
                        │  diff (只保留取出)
                        ▼
                   有界队列 (queue.Queue)
                        ▼
                   写库线程 (SQLite WAL, 批量 executemany)
                        ▼
                   !!wh export ──► CSV (utf-8-sig, Excel 友好)

环境要求:
    - MCDReforged >= 2.1.0
    - 服务端 RCON 已启用 (MCDR config.yml: rcon.enable: true)
    - 监控区域所在维度与容器类型可在 config/warehouse_audit/warehouse_audit.json 配置

已知限制 (轮询机制的固有权衡):
    - 同一轮询周期内 "取出又放回" 的净零变化不会记录
    - 默认只记录取出 (record_deposits=false), 不记录玩家 ID (record_player_id=false);
      需要完整审计时可在配置中开启
    - 区域文件枚举模式最多滞后一个世界自动保存周期
"""

PLUGIN_METADATA = {
	'id': 'warehouse_audit',
	'version': '1.0.3',
	'name': '仓库审计日志 (WarehouseAuditLog)',
	'description': {
		'en_us': 'Audit warehouse container interactions (player/time/item/count/coords) into SQLite with async batched writes, export CSV via !!wh export',
		'zh_cn': '审计仓库容器交互事件（玩家/时间/物品/数量/坐标）写入 SQLite，异步队列批量写入不卡服，!!wh export 导出 CSV',
	},
	'author': 'TPW',
	'link': 'https://github.com/MCDReforged',
	'dependencies': {
		'mcdreforged': '>=2.1.0',
	},
}

import csv
import copy
import datetime
import os
import queue
import re
import shlex
import sqlite3
import struct
import threading
import time
import traceback
import zlib
from typing import Any, Dict, List, Optional, Set, Tuple

from mcdreforged.api.all import *
from mcdreforged.api.command import *
from mcdreforged.api.rtext import *

# ===========================================================================
# 常量
# ===========================================================================

COMMAND_PREFIX = '!!wh'

#: `data get block X Y Z <path>` 成功时的输出 (注意坐标之间是逗号+空格)
#: 兼容 1.13~1.20.4 的 "X, Y, Z has the following block data: ...";
#: 新版本若改措辞, 回退到宽松匹配 (坐标 + 冒号 + 任意内容)
BLOCK_DATA_RE = re.compile(
	r'^(-?\d+), (-?\d+), (-?\d+) has the following block data: (.*)$',
	re.DOTALL
)
#: 宽松兜底: 新版本改措辞时匹配 "坐标 + 任意分隔 + 数据" (取行内最后一个冒号分隔)
BLOCK_DATA_RE_LOOSE = re.compile(
	r'^(-?\d+),\s*(-?\d+),\s*(-?\d+)\b[^:]*:\s*(.*)$',
	re.DOTALL
)
#: `execute as @a run data get entity @s Pos` 的每玩家输出 (多名玩家拼接在同一字符串中)
PLAYER_POS_RE = re.compile(
	r'([A-Za-z0-9_]{1,16}) has the following entity data: '
	r'\[(-?[\d.]+(?:[eE][-+]?\d+)?)d, (-?[\d.]+(?:[eE][-+]?\d+)?)d, (-?[\d.]+(?:[eE][-+]?\d+)?)d\]'
)
#: 简单物品条目 (1.20.4 旧格式, 无嵌套 tag) 的正则快提取:
#: {Slot: 0b, id: "minecraft:diamond", Count: 5b}  (键顺序不限, 1.20.5+ 的小写键也兼容)
FAST_ITEMS_RE = re.compile(
	r'\{\s*Slot:\s*(\d+)b?\s*,\s*id:\s*"([a-z0-9_:]+)"\s*,\s*Count:\s*(\d+)b?\s*\}'
)
#: `execute as @a run data get entity @s Dimension` 的每玩家输出
PLAYER_DIM_RE = re.compile(
	r'([A-Za-z0-9_]{1,16}) has the following entity data: "([a-z0-9_:.-]+)"'
)

ERR_NOT_BLOCK_ENTITY = 'not a block entity'
ERR_NOT_LOADED = 'not loaded'
ERR_PATH_NOT_FOUND = 'Found no elements matching'

#: 容器读取状态
READ_OK = 'ok'            # 读取成功 (含空容器)
READ_ABSENT = 'absent'    # 该位置不是方块实体 (容器被拆除)
READ_SKIP = 'skip'        # 区块未加载 / 命令失败 / 响应异常, 本轮跳过

DIMENSION_REGION_SUBDIR = {
	'minecraft:overworld': '',
	'minecraft:the_nether': 'DIM-1',
	'minecraft:the_end': 'DIM1',
}

#: 维度别名 -> 标准 ID (指令参数用)
DIMENSION_ALIASES = {
	'overworld': 'minecraft:overworld',
	'minecraft:overworld': 'minecraft:overworld',
	'主世界': 'minecraft:overworld',
	'the_nether': 'minecraft:the_nether',
	'nether': 'minecraft:the_nether',
	'minecraft:the_nether': 'minecraft:the_nether',
	'地狱': 'minecraft:the_nether',
	'the_end': 'minecraft:the_end',
	'end': 'minecraft:the_end',
	'minecraft:the_end': 'minecraft:the_end',
	'末地': 'minecraft:the_end',
}

WORLD_COORD_LIMIT = 30_000_000

DEFAULT_CONFIG = {
	'enabled': True,
	'regions': [
		{
			'name': '主仓库',
			'dimension': 'minecraft:overworld',
			'from': [-10, 99, -10],
			'to': [10, 101, 10],
		},
	],
	'container_blocks': [
		'minecraft:chest', 'minecraft:trapped_chest', 'minecraft:barrel',
		# 潜影盒 (无色 + 16 色)
		'minecraft:shulker_box',
		'minecraft:white_shulker_box', 'minecraft:orange_shulker_box',
		'minecraft:magenta_shulker_box', 'minecraft:light_blue_shulker_box',
		'minecraft:yellow_shulker_box', 'minecraft:lime_shulker_box',
		'minecraft:pink_shulker_box', 'minecraft:gray_shulker_box',
		'minecraft:light_gray_shulker_box', 'minecraft:cyan_shulker_box',
		'minecraft:purple_shulker_box', 'minecraft:blue_shulker_box',
		'minecraft:brown_shulker_box', 'minecraft:green_shulker_box',
		'minecraft:red_shulker_box', 'minecraft:black_shulker_box',
	],
	'poll_interval_seconds': 1.0,
	'rescan_interval_seconds': 300.0,
	'enumeration_method': 'auto',          # auto | rcon_scan | region_file
	'rcon_scan_max_blocks': 10000,         # auto 模式下超过该体积改用区域文件解析
	'save_before_rescan': False,           # 区域文件解析前是否 /save-all flush (有阻塞风险)
	'attribution_radius': 8.0,
	'world_directory': '',                 # 留空则自动推断
	'database_file': 'warehouse_audit.db',
	'export_directory': 'export',
	'queue_max_size': 100000,
	'batch_write_size': 200,
	'batch_write_interval_seconds': 0.5,
	'prune_after_misses': 600,             # 连续 N 次读取不到容器后从轮询列表移除
	'record_deposits': False,              # False = 只记录取出 (存入不记录)
	'record_player_id': True,              # True = 记录玩家 ID (查询玩家位置做归因)
	'record_dimension': False,             # False = 不记录维度信息
	'anonymous_mode': False,               # True = 匿名模式: 数据输出用数字编号代替玩家 ID
	'lock_containers': True,               # True = 锁定容器列表: 区域框定后不再自动新增容器坐标 (临时容器不统计)
	'merge_window_seconds': 1.0,           # 同一容器相同物品的多次取出在此时长内合并为一条 (数量相加); 0 = 不合并
}


def csv_columns(record_player: bool, record_dimension: bool) -> List[str]:
	"""按配置生成 CSV 列 (默认最小化: 不记玩家/维度/槽位)。"""
	cols = ['时间']
	if record_dimension:
		cols.append('维度')
	cols += ['X', 'Y', 'Z', '容器类型', '物品ID', '变化量', '变化前', '变化后', '方向']
	if record_player:
		cols += ['玩家', '玩家距离', '多人可疑']
	cols.append('内容物')
	return cols

CSV_HEADERS = [
	'时间', '玩家', '维度', 'X', 'Y', 'Z', '容器类型', '槽位', '物品ID',
	'变化量', '变化前', '变化后', '方向', '玩家距离', '多人可疑',
]

DB_SCHEMA = '''
CREATE TABLE IF NOT EXISTS container_changes (
	id              INTEGER PRIMARY KEY AUTOINCREMENT,
	ts              TEXT    NOT NULL,
	ts_unix         REAL    NOT NULL,
	region          TEXT    NOT NULL DEFAULT '',
	dimension       TEXT    NOT NULL,
	x               INTEGER NOT NULL,
	y               INTEGER NOT NULL,
	z               INTEGER NOT NULL,
	block           TEXT    NOT NULL,
	slot            INTEGER NOT NULL,             -- 恒为 -1 (不记录槽位, 旧数据保留原值)
	item_id         TEXT    NOT NULL,
	delta           INTEGER NOT NULL,             -- +存入 / -取出
	before_count    INTEGER NOT NULL,
	after_count     INTEGER NOT NULL,
	player          TEXT,                         -- NULL = 未记录玩家
	player_distance REAL,                         -- 归因玩家距容器中心距离
	ambiguous       INTEGER NOT NULL DEFAULT 0,   -- 1 = 半径内多人
	contents        TEXT                          -- 被取出/存入物品的内嵌容器内容概要
);
CREATE INDEX IF NOT EXISTS idx_cc_ts ON container_changes(ts_unix);
CREATE INDEX IF NOT EXISTS idx_cc_player ON container_changes(player);
CREATE INDEX IF NOT EXISTS idx_cc_pos ON container_changes(x, y, z);
CREATE INDEX IF NOT EXISTS idx_cc_item ON container_changes(item_id);
'''

INSERT_SQL = (
	'INSERT INTO container_changes '
	'(ts, ts_unix, region, dimension, x, y, z, block, slot, item_id, '
	' delta, before_count, after_count, player, player_distance, ambiguous, contents) '
	'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
)


# ===========================================================================
# SNBT 解析器 (Minecraft 字符串化 NBT)
# ===========================================================================

class SnbtError(ValueError):
	"""SNBT 解析失败"""


class _SnbtParser:
	"""递归下降 SNBT 解析器。

	支持: 复合标签 {k: v, ...} (键可带引号或不带)、列表 [...]、
	双引号/单引号字符串 (含转义)、带后缀数字 (b/s/L/f/d)、科学计数法。
	"""

	def __init__(self, text: str):
		self.text = text
		self.pos = 0
		self.length = len(text)

	# -- 基础工具 -----------------------------------------------------------

	def _error(self, msg: str) -> SnbtError:
		snippet = self.text[max(0, self.pos - 20):self.pos + 20]
		return SnbtError('{} at pos {} near {!r}'.format(msg, self.pos, snippet))

	def _skip_ws(self) -> None:
		while self.pos < self.length and self.text[self.pos] in ' \t\r\n':
			self.pos += 1

	def _peek(self) -> str:
		self._skip_ws()
		if self.pos >= self.length:
			return ''
		return self.text[self.pos]

	# -- 入口 ---------------------------------------------------------------

	def parse(self) -> Any:
		self._skip_ws()
		if self.pos >= self.length:
			raise SnbtError('empty input')
		value = self._parse_value()
		self._skip_ws()
		if self.pos != self.length:
			raise self._error('trailing data')
		return value

	# -- 值 -----------------------------------------------------------------

	def _parse_value(self) -> Any:
		self._skip_ws()
		if self.pos >= self.length:
			raise self._error('unexpected end of input')
		ch = self.text[self.pos]
		if ch == '{':
			return self._parse_compound()
		if ch == '[':
			return self._parse_list()
		if ch == '"' or ch == "'":
			return self._parse_quoted_string(ch)
		return self._parse_unquoted()

	def _parse_compound(self) -> Dict[str, Any]:
		assert self.text[self.pos] == '{'
		self.pos += 1
		result: Dict[str, Any] = {}
		if self._peek() == '}':
			self.pos += 1
			return result
		while True:
			self._skip_ws()
			if self.pos >= self.length:
				raise self._error('unterminated compound')
			ch = self.text[self.pos]
			if ch == '"' or ch == "'":
				key = self._parse_quoted_string(ch)
			else:
				key = self._parse_unquoted(stop_chars=':,} \t\r\n')
			if not isinstance(key, str):
				raise self._error('invalid compound key')
			self._skip_ws()
			if self.pos >= self.length or self.text[self.pos] != ':':
				raise self._error("expected ':' after compound key")
			self.pos += 1
			result[key] = self._parse_value()
			nxt = self._peek()
			if nxt == ',':
				self.pos += 1
				continue
			if nxt == '}':
				self.pos += 1
				return result
			raise self._error("expected ',' or '}' in compound")

	def _parse_list(self) -> List[Any]:
		assert self.text[self.pos] == '['
		self.pos += 1
		result: List[Any] = []
		if self._peek() == ']':
			self.pos += 1
			return result
		while True:
			result.append(self._parse_value())
			nxt = self._peek()
			if nxt == ',':
				self.pos += 1
				continue
			if nxt == ']':
				self.pos += 1
				return result
			raise self._error("expected ',' or ']' in list")

	def _parse_quoted_string(self, quote: str) -> str:
		assert self.text[self.pos] == quote
		self.pos += 1
		chars: List[str] = []
		while self.pos < self.length:
			ch = self.text[self.pos]
			if ch == '\\':
				if self.pos + 1 >= self.length:
					raise self._error('dangling escape')
				esc = self.text[self.pos + 1]
				mapping = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f',
						   '0': '\0', '\\': '\\', '"': '"', "'": "'"}
				chars.append(mapping.get(esc, esc))
				self.pos += 2
				continue
			if ch == quote:
				self.pos += 1
				return ''.join(chars)
			chars.append(ch)
			self.pos += 1
		raise self._error('unterminated string')

	def _parse_unquoted(self, stop_chars: str = ',}] \t\r\n') -> Any:
		"""解析无引号 token: 字符串 (方块 id / 键名) 或数字。"""
		start = self.pos
		while self.pos < self.length and self.text[self.pos] not in stop_chars:
			self.pos += 1
		token = self.text[start:self.pos].strip()
		if not token:
			raise self._error('empty token')
		m = re.fullmatch(
			r'([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([bBsSlLfFdD])?',
			token
		)
		if m and not token[0].isalpha():
			num, suffix = m.group(1), m.group(2)
			if suffix in ('f', 'F', 'd', 'D') or '.' in num or 'e' in num or 'E' in num:
				return float(num)
			return int(num)
		return token


def parse_snbt(text: str) -> Any:
	"""解析 SNBT 字符串为 Python 值。失败抛出 SnbtError。"""
	return _SnbtParser(text).parse()


# ===========================================================================
# 二进制 NBT 解析器 + 区域文件 (.mca) 遍历
# ===========================================================================

_TAG_END = 0
_TAG_BYTE = 1
_TAG_SHORT = 2
_TAG_INT = 3
_TAG_LONG = 4
_TAG_FLOAT = 5
_TAG_DOUBLE = 6
_TAG_BYTE_ARRAY = 7
_TAG_STRING = 8
_TAG_LIST = 9
_TAG_COMPOUND = 10
_TAG_INT_ARRAY = 11
_TAG_LONG_ARRAY = 12


class _BinaryNbtReader:
	"""最小二进制 NBT 读取器 (大端, 区域文件区块格式)。"""

	def __init__(self, data: bytes):
		self.data = data
		self.pos = 0

	def _take(self, n: int) -> bytes:
		if self.pos + n > len(self.data):
			raise SnbtError('NBT data truncated (need {} more bytes)'.format(n))
		chunk = self.data[self.pos:self.pos + n]
		self.pos += n
		return chunk

	def _ubyte(self) -> int:
		return self._take(1)[0]

	def _string(self) -> str:
		(n,) = struct.unpack('>H', self._take(2))
		return self._take(n).decode('utf-8', 'replace')

	def _payload(self, tag_type: int) -> Any:
		if tag_type == _TAG_BYTE:
			return struct.unpack('>b', self._take(1))[0]
		if tag_type == _TAG_SHORT:
			return struct.unpack('>h', self._take(2))[0]
		if tag_type == _TAG_INT:
			return struct.unpack('>i', self._take(4))[0]
		if tag_type == _TAG_LONG:
			return struct.unpack('>q', self._take(8))[0]
		if tag_type == _TAG_FLOAT:
			return struct.unpack('>f', self._take(4))[0]
		if tag_type == _TAG_DOUBLE:
			return struct.unpack('>d', self._take(8))[0]
		if tag_type == _TAG_BYTE_ARRAY:
			(n,) = struct.unpack('>i', self._take(4))
			return list(self._take(n))
		if tag_type == _TAG_STRING:
			return self._string()
		if tag_type == _TAG_LIST:
			item_type = self._ubyte()
			(n,) = struct.unpack('>i', self._take(4))
			return [self._payload(item_type) for _ in range(n)]
		if tag_type == _TAG_COMPOUND:
			out: Dict[str, Any] = {}
			while True:
				t = self._ubyte()
				if t == _TAG_END:
					return out
				name = self._string()
				out[name] = self._payload(t)
		if tag_type == _TAG_INT_ARRAY:
			(n,) = struct.unpack('>i', self._take(4))
			return list(struct.unpack('>{}i'.format(n), self._take(4 * n)))
		if tag_type == _TAG_LONG_ARRAY:
			(n,) = struct.unpack('>i', self._take(4))
			return list(struct.unpack('>{}q'.format(n), self._take(8 * n)))
		raise SnbtError('unknown NBT tag type {}'.format(tag_type))

	def read_root(self) -> Dict[str, Any]:
		if self._ubyte() != _TAG_COMPOUND:
			raise SnbtError('chunk root is not a compound tag')
		self._string()  # root name (empty)
		return self._payload(_TAG_COMPOUND)


def _region_coords_from_name(path: str) -> Tuple[Optional[int], Optional[int]]:
	m = re.search(r'r\.(-?\d+)\.(-?\d+)\.mca$', os.path.basename(path))
	if not m:
		return None, None
	return int(m.group(1)), int(m.group(2))


def iter_region_file_chunks(path: str):
	"""遍历区域文件, yield (chunk_x, chunk_z, nbt_root)。损坏区块跳过。"""
	with open(path, 'rb') as f:
		header = f.read(8192)
		if len(header) < 8192:
			return
		region_x, region_z = _region_coords_from_name(path)
		if region_x is None:
			return
		for i in range(1024):
			offset = struct.unpack('>I', b'\x00' + header[i * 4:i * 4 + 3])[0]
			if offset == 0:
				continue
			f.seek(offset * 4096)
			head = f.read(5)
			if len(head) < 5:
				continue
			(length,) = struct.unpack('>I', head[:4])
			compression = head[4]
			if length <= 1 or length > 1024 * 1024:
				continue
			data = f.read(length - 1)
			try:
				if compression == 1:
					raw = zlib.decompress(data, 16 + 15)  # gzip
				elif compression == 2:
					raw = zlib.decompress(data)
				else:
					continue
			except zlib.error:
				continue
			try:
				root = _BinaryNbtReader(raw).read_root()
			except SnbtError:
				continue
			chunk_x = region_x * 32 + (i & 31)
			chunk_z = region_z * 32 + (i >> 5)
			yield chunk_x, chunk_z, root


# ===========================================================================
# 服务器响应解析辅助
# ===========================================================================

def parse_block_items_response(response: Optional[str], pos: Tuple[int, int, int]) -> Tuple[str, Optional[Dict[int, Tuple[str, int, Optional[str]]]]]:
	"""解析 `data get block X Y Z Items` 的 RCON 响应。

	返回 (status, items):
	    (READ_OK, {slot: (item_id, count, contents)})   读取成功 (空容器为 {})
	        contents = 内嵌容器内容概要 (潜影盒/箱子物品), 如 'observer x1728; packed_ice x576';
        非容器物品或空盒为 None
	    (READ_ABSENT, {})                     该位置不是方块实体 (容器不存在)
	    (READ_SKIP, None)                     本轮应跳过 (未加载/失败/坐标不匹配)
	"""
	if response is None:
		return READ_SKIP, None
	text = response.strip()
	if not text:
		return READ_SKIP, None
	if ERR_NOT_LOADED in text:
		return READ_SKIP, None
	if ERR_NOT_BLOCK_ENTITY in text:
		return READ_ABSENT, {}
	if ERR_PATH_NOT_FOUND in text:
		# 该方块实体没有 Items 标签 (如空潜影盒) -> 视为空容器
		return READ_OK, {}
	m = BLOCK_DATA_RE.match(text) or BLOCK_DATA_RE_LOOSE.match(text)
	if not m:
		return READ_SKIP, None
	if (int(m.group(1)), int(m.group(2)), int(m.group(3))) != pos:
		return READ_SKIP, None
	payload = m.group(4)
	# 性能优化: 简单响应 (1.20.4 旧格式, 无嵌套 tag/components) 用正则快速提取,
	# 比递归下降 SNBT 解析快约 10 倍; 含嵌套结构时回退全量解析。
	if 'tag:' not in payload and 'components' not in payload and '"' in payload:
		fast = FAST_ITEMS_RE.findall(payload)
		if fast:
			result: Dict[int, Tuple[str, int, Optional[str]]] = {}
			for slot_s, item_id, count_s in fast:
				try:
					result[int(slot_s)] = (item_id, int(count_s), None)
				except ValueError:
					continue
			return READ_OK, result
	try:
		items = parse_snbt(payload)
	except SnbtError:
		return READ_SKIP, None
	if not isinstance(items, list):
		return READ_SKIP, None
	result: Dict[int, Tuple[str, int, Optional[str]]] = {}
	for entry in items:
		if not isinstance(entry, dict):
			continue
		# 1.20.4: id / 1.20.5+: item (组件化)
		item_id = entry.get('id') or entry.get('item')
		if not isinstance(item_id, str):
			continue
		try:
			slot = int(entry.get('Slot', entry.get('slot', 0)))
			count = int(entry.get('Count', entry.get('count', 0)))
		except (TypeError, ValueError):
			continue
		result[slot] = (item_id, count, extract_item_contents(entry))
	return READ_OK, result


def extract_item_contents(entry: Dict[str, Any]) -> Optional[str]:
	"""提取物品的内嵌容器内容概要 (潜影盒/箱子等), 按数量降序聚合。

	兼容 1.20.4 及更早的 tag.BlockEntityTag.Items 与 1.20.5+ 的
	components['minecraft:container' / 'minecraft:bundle_contents'] 两种格式。
	只展开一层 (嵌套盒中的盒不再递归)。
	"""
	nested = None
	tag = entry.get('tag')
	if isinstance(tag, dict):
		bet = tag.get('BlockEntityTag')
		if isinstance(bet, dict):
			nested = bet.get('Items')
	if nested is None:
		comps = entry.get('components')
		if isinstance(comps, dict):
			for key in ('minecraft:container', 'minecraft:bundle_contents'):
				if isinstance(comps.get(key), list):
					nested = comps[key]
					break
	if not isinstance(nested, list) or not nested:
		return None
	agg: Dict[str, int] = {}
	for sub in nested:
		if not isinstance(sub, dict):
			continue
		sub_id = sub.get('id')
		if not isinstance(sub_id, str):
			sub_item = sub.get('item')
			sub_id = sub_item if isinstance(sub_item, str) else None
		if not sub_id:
			continue
		try:
			sub_count = int(sub.get('Count', sub.get('count', 1)))
		except (TypeError, ValueError):
			sub_count = 1
		agg[sub_id] = agg.get(sub_id, 0) + sub_count
	if not agg:
		return None
	parts = ['{} x{}'.format(k, v) for k, v in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))]
	return '; '.join(parts)[:500]


def parse_player_positions(response: Optional[str]) -> Dict[str, Tuple[float, float, float]]:
	"""解析 `execute as @a run data get entity @s Pos` 响应 (多名玩家拼接)。"""
	result: Dict[str, Tuple[float, float, float]] = {}
	if not response:
		return result
	for name, xs, ys, zs in PLAYER_POS_RE.findall(response):
		try:
			result[name] = (float(xs), float(ys), float(zs))
		except ValueError:
			continue
	return result


def parse_player_dimensions(response: Optional[str]) -> Dict[str, str]:
	"""解析 `execute as @a run data get entity @s Dimension` 响应。"""
	result: Dict[str, str] = {}
	if not response:
		return result
	for name, dim in PLAYER_DIM_RE.findall(response):
		result[name] = dim
	return result


def split_command_args(args: str) -> List[str]:
	"""类 shlex 分词, 但保留 Windows 反斜杠路径 (posix=False + 去成对引号)。"""
	tokens = shlex.split(args or '', posix=False)
	cleaned = []
	for token in tokens:
		if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
			token = token[1:-1]
		cleaned.append(token)
	return cleaned


# ===========================================================================
# 主插件类
# ===========================================================================

class WarehouseAudit:
	"""插件核心: 配置、线程、状态。"""

	def __init__(self, server: PluginServerInterface):
		self.server = server
		self.config: Dict[str, Any] = dict(DEFAULT_CONFIG)
		self.data_folder = os.path.abspath(server.get_data_folder())
		self.db_path = os.path.join(self.data_folder, self.config['database_file'])
		self.export_dir = os.path.join(self.data_folder, self.config['export_directory'])
		self.world_dir: Optional[str] = None

		# 共享状态 (poller 写, 指令读; 用锁保护)
		self._state_lock = threading.Lock()
		self.containers: Dict[Tuple[int, int, int], str] = {}     # pos -> block id
		# 快照: pos -> {slot: (item_id, count, contents)}
		self.snapshots: Dict[Tuple[int, int, int], Dict[int, Tuple[str, int, Optional[str]]]] = {}
		self.miss_counts: Dict[Tuple[int, int, int], int] = {}
		self.players: Dict[str, Tuple[Optional[str], float, float, float]] = {}
		self.region_of: Dict[Tuple[int, int, int], str] = {}      # pos -> region name
		self._rescan_lock = threading.Lock()
		# 已确认的非容器坐标缓存 (扫描性能优化)
		self._verified_absent: Set[Tuple[int, int, int]] = set()
		# 响应哈希缓存 (1.20.5+ 大响应性能优化: 哈希未变跳过解析)
		self._resp_hashes: Dict[Tuple[int, int, int], int] = {}
		self._resp_hash_lock = threading.Lock()
		# 容器列表锁定 (lock_containers=true 时首次枚举后密封)
		self._containers_sealed = False
		# 匿名编号映射: player name -> 编号 (anon mode)
		self._anon_ids: Dict[str, str] = {}
		self._anon_counter = 0

		# 运行统计
		self.stats = {
			'poll_cycles': 0,
			'last_cycle_ms': 0.0,
			'last_poll_ts': 0.0,
			'last_rescan_ts': 0.0,
			'last_rescan_method': '-',
			'last_rescan_count': 0,
			'records_written': 0,
			'records_dropped': 0,
			'started_ts': time.time(),
			'last_error': '',
		}

		# 异步写库
		self.record_queue: 'queue.Queue' = queue.Queue()
		self._stop_event = threading.Event()
		self._poller_thread: Optional[threading.Thread] = None
		self._writer_thread: Optional[threading.Thread] = None
		self._writer_conn: Optional[sqlite3.Connection] = None
		# 合并缓冲: key -> (record, first_ts, last_ts), 轮询线程为主, rebaseline 亦会访问
		self._pending_merges: Dict[Tuple, Tuple[Tuple, float, float]] = {}
		self._merge_lock = threading.Lock()

		# 区域文件解析缓存: path -> (mtime, size, {(cx,cz): root})
		self._region_file_cache: Dict[str, Tuple[float, int, Dict[Tuple[int, int], Dict[str, Any]]]] = {}

	# ------------------------------------------------------------------ 配置

	def load_config(self) -> None:
		self.config = self.server.load_config_simple(
			'warehouse_audit.json', default_config=copy.deepcopy(DEFAULT_CONFIG)
		)
		self.db_path = os.path.join(self.data_folder, str(self.config['database_file']))
		self.export_dir = os.path.join(self.data_folder, str(self.config['export_directory']))
		self.world_dir = self._resolve_world_dir()

	def _resolve_world_dir(self) -> Optional[str]:
		configured = str(self.config.get('world_directory') or '').strip()
		if configured:
			return configured if os.path.isdir(configured) else None
		try:
			mcdr_config = self.server.get_mcdr_config() or {}
		except Exception:
			mcdr_config = {}
		work_dir = str(mcdr_config.get('working_directory') or 'server')
		instance_dir = os.path.dirname(os.path.dirname(self.data_folder))
		base = work_dir if os.path.isabs(work_dir) else os.path.join(instance_dir, work_dir)
		level_name = 'world'
		try:
			with open(os.path.join(base, 'server.properties'), 'r', encoding='utf-8', errors='replace') as f:
				for line in f:
					if line.strip().startswith('level-name'):
						level_name = line.split('=', 1)[1].strip() or 'world'
						break
		except OSError:
			pass
		for candidate in (os.path.join(base, level_name), os.path.join(base, 'world'),
						  os.path.join(instance_dir, 'world')):
			if os.path.isdir(os.path.join(candidate, 'region')):
				return candidate
		return None

	# -------------------------------------------------------------- 生命周期

	def start(self) -> None:
		os.makedirs(self.data_folder, exist_ok=True)
		self._writer_thread = threading.Thread(
			target=self._writer_loop, name='WarehouseAudit-Writer', daemon=True)
		self._writer_thread.start()
		self._poller_thread = threading.Thread(
			target=self._poll_loop, name='WarehouseAudit-Poller', daemon=True)
		self._poller_thread.start()

	def stop(self) -> None:
		self._stop_event.set()
		for thread in (self._poller_thread, self._writer_thread):
			if thread is not None and thread.is_alive():
				thread.join(timeout=5.0)
		if self._writer_conn is not None:
			try:
				self._writer_conn.close()
			except sqlite3.Error:
				pass
			self._writer_conn = None

	# ------------------------------------------------------------------ 轮询

	def _rcon_query(self, cmd: str) -> Optional[str]:
		"""带护栏的 rcon_query: 查询前后再校验 RCON 状态, 异常返回 None。

		服务端关闭瞬间 MCDR 的 RCON 连接对象可能仍处于"已连接"状态,
		此时查询会触发 MCDR 内部 3 次重试 (Bad file descriptor 日志)。
		这里在查询前后各做一次状态检查, 尽量避免在服务端死亡时触发查询。
		"""
		if self._stop_event.is_set():
			return None
		try:
			if not self.server.is_rcon_running():
				return None
			return self.server.rcon_query(cmd)
		except Exception:
			return None

	def _poll_loop(self) -> None:
		"""轮询线程主循环: 枚举 -> 读玩家 -> 读容器 -> diff -> 合并 -> 入队。"""
		server = self.server
		backoff = 1.0
		try:
			while not self._stop_event.is_set():
				try:
					if not server.is_server_running() or not server.is_rcon_running():
						# 服务器断开: 立即写出合并缓冲中的记录, 避免滞留内存
						# (服务器恢复后下一周期自动重新基线, 见 on_server_startup)
						self._flush_all_pending()
						backoff = min(backoff * 2, 10.0)
						self._stop_event.wait(backoff)
						continue
					backoff = 1.0
					cycle_start = time.perf_counter()
					if bool(self.config.get('enabled', True)):
						self._maybe_rescan()
						self._read_players()
						self._read_containers_and_diff()
						with self._state_lock:
							self.stats['poll_cycles'] += 1
							cycle_ms = (time.perf_counter() - cycle_start) * 1000.0
							self.stats['last_cycle_ms'] = cycle_ms
							self.stats['last_poll_ts'] = time.time()
					interval = max(0.1, float(self.config.get('poll_interval_seconds', 1.0)))
					# 自适应降频 (时间预算): 单周期耗时超过轮询间隔的 60% 时,
					# 自动拉长间隔到 周期耗时/0.6 (上限 10s), 保证不卡 RCON/服务端;
					# 周期变快后逐步回落到配置间隔
					with self._state_lock:
						cycle_ms = self.stats['last_cycle_ms']
					budget = interval * 1000.0 * 0.6
					if cycle_ms > budget:
						interval = min(10.0, max(interval, (cycle_ms / 1000.0) / 0.6))
					self._stop_event.wait(interval)
				except Exception:
					err = traceback.format_exc()
					with self._state_lock:
						self.stats['last_error'] = err.strip().splitlines()[-1] if err else 'unknown'
					server.logger.error('WarehouseAudit poll loop error:\n{}'.format(err))
					self._stop_event.wait(2.0)
		finally:
			# 退出前把合并缓冲中的记录全部写入队列, 避免丢失
			try:
				self._flush_all_pending()
			except Exception:
				pass

	def _read_players(self) -> None:
		if not bool(self.config.get('record_player_id', False)):
			with self._state_lock:
				self.players = {}
			return
		positions = parse_player_positions(
			self._rcon_query('execute as @a run data get entity @s Pos'))
		if positions is None:
			# RCON 断开 (服务端死亡瞬间): 跳过本轮
			return
		dimensions = parse_player_dimensions(
			self._rcon_query('execute as @a run data get entity @s Dimension'))
		players: Dict[str, Tuple[Optional[str], float, float, float]] = {}
		for name, (x, y, z) in positions.items():
			players[name] = (dimensions.get(name), x, y, z)
		with self._state_lock:
			self.players = players

	def _read_containers_and_diff(self) -> None:
		now = datetime.datetime.now().astimezone()
		now_iso = now.isoformat(timespec='milliseconds')
		now_unix = time.time()
		with self._state_lock:
			containers = dict(self.containers)
			players = dict(self.players)
			old_snapshots = dict(self.snapshots)
			region_of = dict(self.region_of)
		new_snapshots: Dict[Tuple[int, int, int], Dict[int, Tuple[str, int]]] = {}
		records: List[Tuple] = []
		prune_threshold = int(self.config.get('prune_after_misses', 600))
		# 多线程解析: RCON 拉取串行 (单 socket), SNBT 解析并行 (CPU 密集)
		# 解析结果按容器顺序收集, 保证处理顺序稳定
		parse_results: Dict[Tuple[int, int, int], object] = {}
		parse_lock = threading.Lock()
		parse_error = {'abort': False}
		def fetch_and_parse(pos, block_id):
			if self._stop_event.is_set() or parse_error['abort']:
				return
			x, y, z = pos
			resp = self._rcon_query('data get block {} {} {} Items'.format(x, y, z))
			if resp is None:
				# RCON 断开 (服务端死亡瞬间): 通知中止
				parse_error['abort'] = True
				return
			# 性能优化 (1.20.5+ 大响应): 响应哈希与上次相同 -> 内容未变,
			# 直接复用旧快照, 跳过 SNBT 全量解析 (1.21.x 组件化响应可达几十 KB)
			with self._resp_hash_lock:
				last_hash = self._resp_hashes.get(pos)
			this_hash = hash(resp)
			if this_hash == last_hash:
				with parse_lock:
					parse_results[pos] = ('unchanged', None)
				return
			try:
				status, items = parse_block_items_response(resp, pos)
			except Exception:
				status, items = READ_SKIP, None
			with self._resp_hash_lock:
				self._resp_hashes[pos] = this_hash
			with parse_lock:
				parse_results[pos] = (status, items)

		# 拉取+解析在多个工作线程并行 (RCON 内部有锁串行化, 解析并行)
		from concurrent.futures import ThreadPoolExecutor
		workers = max(2, min(4, (os.cpu_count() or 2)))
		with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='WarehouseAudit-Parse') as pool:
			futures = [pool.submit(fetch_and_parse, pos, bid) for pos, bid in containers.items()]
			for f in futures:
				f.result()
		if parse_error['abort']:
			# RCON 断开: 本轮中止 (快照不变, 下轮重新基线)
			return
		for pos, block_id in containers.items():
			result = parse_results.get(pos)
			if result is None:
				continue
			status, items = result
			if status == 'unchanged':
				# 响应哈希未变 -> 内容未变: 复用旧快照, 跳过 diff (1.21.x 性能优化)
				if pos in old_snapshots:
					new_snapshots[pos] = old_snapshots[pos]
					self.miss_counts[pos] = 0
				continue
			if status == READ_SKIP:
				# 未加载或读取失败: 保留旧快照 (未加载区块内不可能发生变化)
				if pos in old_snapshots:
					new_snapshots[pos] = old_snapshots[pos]
				continue
			if items is None:
				items = {}
			new_snapshots[pos] = items
			old_items = old_snapshots.get(pos)
			if old_items is None:
				# 首次读取: 建立基线, 不产生记录
				continue
			if status == READ_ABSENT:
				# 容器不存在: 累计未命中, 达阈值后由 rescan/修剪逻辑移除
				self.miss_counts[pos] = self.miss_counts.get(pos, 0) + 1
			else:
				self.miss_counts[pos] = 0
			if items == old_items:
				continue
			region_name = region_of.get(pos, '')
			dimension = self._dimension_of(pos)
			player, distance, ambiguous = self._attribute_player(pos, players, dimension)
			records.extend(self._diff_container(
				pos, block_id, region_name, dimension, old_items, items,
				now_iso, now_unix, player, distance, ambiguous,
				bool(self.config.get('record_deposits', False)),
				bool(self.config.get('record_dimension', False))))
		with self._state_lock:
			self.snapshots = new_snapshots
			for pos in list(self.miss_counts):
				if self.miss_counts.get(pos, 0) >= prune_threshold:
					self.containers.pop(pos, None)
					self.snapshots.pop(pos, None)
					self.miss_counts.pop(pos, None)
		# 1 秒内同一容器相同物品(同方向)的多次取出合并为一条, 数量相加
		merge_window = float(self.config.get('merge_window_seconds', 1.0))
		for record in records:
			self._merge_record(record, merge_window)
		self._flush_expired_pending(now_unix, merge_window)

	def _merge_record(self, record: Tuple, merge_window: float) -> None:
		"""合并缓冲: key = (物品, X, Y, Z, 是否存入)。

		以组合内首条记录的时间为基准, merge_window 内的后续记录合并
		(delta 相加, after_count 取最新, before_count/时间保持首次)。
		"""
		if merge_window <= 0:
			self._enqueue(record)
			return
		item_id, x, y, z = record[9], record[4], record[5], record[6]
		key = (item_id, x, y, z, record[10] > 0)
		now = record[1]
		with self._merge_lock:
			pending = self._pending_merges.get(key)
			if pending is not None and now - pending[1] <= merge_window:
				old = pending[0]
				merged = (
					old[0], old[1], old[2], old[3], old[4], old[5], old[6], old[7],
					old[8], old[9],
					old[10] + record[10],   # delta 相加
					old[11],                # before_count 保持首次
					record[12],             # after_count 取最新
					old[13], old[14], old[15],
					old[16],                # contents 保持首次
				)
				self._pending_merges[key] = (merged, pending[1], now)
			else:
				if pending is not None:
					self._enqueue(pending[0])
				self._pending_merges[key] = (record, now, now)

	def _flush_expired_pending(self, now: float, merge_window: float) -> None:
		"""把超过合并窗口的挂起记录写入队列。"""
		if merge_window <= 0:
			return
		with self._merge_lock:
			for key in list(self._pending_merges):
				entry = self._pending_merges[key]
				if now - entry[1] > merge_window:
					self._enqueue(entry[0])
					del self._pending_merges[key]

	def _flush_all_pending(self) -> None:
		"""立即写入全部挂起记录 (服务器断开/插件停止时调用, 避免丢失)。"""
		with self._merge_lock:
			for key in list(self._pending_merges):
				self._enqueue(self._pending_merges.pop(key)[0])

	def rebaseline(self) -> None:
		"""服务器重启后重新建立基线 (丢弃旧快照, 写出挂起记录)。

		用途: 回档后世界内容与旧快照不一致, 若继续 diff 会把
		"回档消失的物品"当成取出记录 (幽灵记录)。重新基线后
		首次读取只建立基线不产生记录。

		代价: 崩溃前不到一个轮询周期内未被记录的取出不会被补记
		    (服务端关闭期间不可能发生交互, 该窗口仅限崩溃前 <1s)。
		"""
		self._flush_all_pending()
		with self._state_lock:
			self.snapshots = {}
		self.server.logger.info('WarehouseAudit: server restarted, snapshots re-baselined')

	def _diff_container(self, pos, block_id, region_name, dimension, old_items, new_items,
						now_iso, now_unix, player, distance, ambiguous,
						record_deposits: bool = True, record_dimension: bool = True) -> List[Tuple]:
		"""对比容器快照, 产生变化记录 (不记录具体槽位, slot 字段恒为 -1)。

		快照槽位格式: (item_id, count, contents) —— contents 为内嵌容器内容概要。
		记录中的 contents 取自被取出/存入物品的内容概要 (合并时取首条)。

		record_deposits=False 时只记录取出 (delta<0), 存入 (delta>0) 不记录。
		record_dimension=False 时 dimension 字段写空串 (不统计维度)。
		物品被替换时: 旧物品取出总是记录, 新物品存入受 record_deposits 控制。
		"""
		dim_value = dimension if record_dimension else ''
		records: List[Tuple] = []
		x, y, z = pos
		ambiguous_flag = 1 if ambiguous else 0
		for slot in sorted(set(old_items) | set(new_items)):
			old_entry = old_items.get(slot)
			new_entry = new_items.get(slot)
			old_item = old_entry[0] if old_entry else None
			old_count = old_entry[1] if old_entry else 0
			old_contents = old_entry[2] if (old_entry and len(old_entry) > 2) else None
			new_item = new_entry[0] if new_entry else None
			new_count = new_entry[1] if new_entry else 0
			new_contents = new_entry[2] if (new_entry and len(new_entry) > 2) else None
			if old_item == new_item and old_count == new_count:
				continue
			if old_item == new_item:
				# 同物品数量变化
				delta = new_count - old_count
				if delta > 0 and not record_deposits:
					continue
				records.append((
					now_iso, now_unix, region_name, dim_value, x, y, z,
					block_id, -1, old_item, delta, old_count, new_count,
					player, distance, ambiguous_flag,
					old_contents if delta < 0 else new_contents,
				))
			else:
				# 物品被替换: 分别记录取出与存入
				if old_item is not None:
					records.append((
						now_iso, now_unix, region_name, dim_value, x, y, z,
						block_id, -1, old_item, -old_count, old_count, 0,
						player, distance, ambiguous_flag, old_contents,
					))
				if new_item is not None and record_deposits:
					records.append((
						now_iso, now_unix, region_name, dim_value, x, y, z,
						block_id, -1, new_item, new_count, 0, new_count,
						player, distance, ambiguous_flag, new_contents,
					))
		return records

	def _dimension_of(self, pos: Tuple[int, int, int]) -> str:
		for region in self._regions():
			if self._in_bounds(pos, region):
				return str(region.get('dimension', 'minecraft:overworld'))
		return 'minecraft:overworld'

	def _attribute_player(self, pos, players, dimension):
		"""同维度内距容器中心最近的玩家; 返回 (name|None, distance|None, ambiguous)。"""
		cx, cy, cz = pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5
		radius = float(self.config.get('attribution_radius', 8.0))
		best_name: Optional[str] = None
		best_dist = float('inf')
		near_count = 0
		for name, (dim, px, py, pz) in players.items():
			if dim is not None and dim != dimension:
				continue
			dist = ((px - cx) ** 2 + (py - cy) ** 2 + (pz - cz) ** 2) ** 0.5
			if dist <= radius:
				near_count += 1
				if dist < best_dist:
					best_dist = dist
					best_name = name
		if best_name is None:
			return None, None, False
		display = self._anonymize(best_name)
		return display, round(best_dist, 2), near_count >= 2

	def _anonymize(self, name: str) -> str:
		"""匿名模式: 用数字编号 (P1, P2...) 代替玩家 ID; 关闭时返回原名。

		编号按玩家首次归因顺序分配, 同一玩家始终对应同一编号 (一一对应)。
		"""
		if not bool(self.config.get('anonymous_mode', False)):
			return name
		with self._state_lock:
			if name not in self._anon_ids:
				self._anon_counter += 1
				self._anon_ids[name] = 'P{}'.format(self._anon_counter)
			return self._anon_ids[name]

	# ------------------------------------------------------------------ 队列

	def _enqueue(self, record: Tuple) -> None:
		max_size = int(self.config.get('queue_max_size', 100000))
		if self.record_queue.qsize() >= max_size:
			try:
				self.record_queue.get_nowait()
				with self._state_lock:
					self.stats['records_dropped'] += 1
			except queue.Empty:
				pass
		self.record_queue.put(record)

	# ------------------------------------------------------------------ 写库

	def _writer_loop(self) -> None:
		batch_interval = max(0.05, float(self.config.get('batch_write_interval_seconds', 0.5)))
		batch_size = max(1, int(self.config.get('batch_write_size', 200)))
		while not (self._stop_event.is_set() and self.record_queue.empty()):
			batch: List[Tuple] = []
			try:
				batch.append(self.record_queue.get(timeout=batch_interval))
				while len(batch) < batch_size:
					try:
						batch.append(self.record_queue.get_nowait())
					except queue.Empty:
						break
			except queue.Empty:
				continue
			if not batch:
				continue
			try:
				self._write_batch(batch)
				with self._state_lock:
					self.stats['records_written'] += len(batch)
			except sqlite3.Error as e:
				self.server.logger.error(
					'WarehouseAudit: failed to write {} records: {}'.format(len(batch), e))
				time.sleep(1.0)
				try:
					self._write_batch(batch)
					with self._state_lock:
						self.stats['records_written'] += len(batch)
				except sqlite3.Error as e2:
					self.server.logger.error(
						'WarehouseAudit: retry failed, dropping {} records: {}'.format(len(batch), e2))

	def _write_batch(self, batch: List[Tuple]) -> None:
		if self._writer_conn is None:
			os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
			conn = sqlite3.connect(self.db_path, timeout=10.0)
			conn.execute('PRAGMA journal_mode=WAL')
			conn.execute('PRAGMA synchronous=NORMAL')
			conn.executescript(DB_SCHEMA)
			self._migrate_schema(conn)
			self._writer_conn = conn
		self._writer_conn.executemany(INSERT_SQL, batch)
		self._writer_conn.commit()

	def ensure_db_schema(self) -> None:
		"""加载时即完成旧库迁移 (补 contents 列), 避免指令查询时报无此列。"""
		if not os.path.isfile(self.db_path):
			return
		try:
			conn = sqlite3.connect(self.db_path, timeout=10.0)
			try:
				self._migrate_schema(conn)
			finally:
				conn.close()
		except sqlite3.Error as e:
			self.server.logger.warning('WarehouseAudit: db schema migration failed: {}'.format(e))

	@staticmethod
	def _migrate_schema(conn: sqlite3.Connection) -> None:
		"""旧库迁移: 补充后续版本新增的列 (如 contents)。"""
		cols = [row[1] for row in conn.execute('PRAGMA table_info(container_changes)')]
		if cols and 'contents' not in cols:
			conn.execute('ALTER TABLE container_changes ADD COLUMN contents TEXT')
			conn.commit()

	def _ro_connect(self) -> sqlite3.Connection:
		uri = 'file:{}?mode=ro'.format(self.db_path.replace('\\', '/').replace('?', '%3f'))
		return sqlite3.connect(uri, uri=True, timeout=10.0)

	# ------------------------------------------------------------------ 枚举

	def _regions(self) -> List[Dict[str, Any]]:
		regions = self.config.get('regions') or []
		return [r for r in regions if isinstance(r, dict)]

	@staticmethod
	def _normalize_bounds(region: Dict[str, Any]) -> Optional[Tuple[Tuple[int, int, int], Tuple[int, int, int]]]:
		try:
			frm = [int(v) for v in region['from']]
			to = [int(v) for v in region['to']]
		except (KeyError, TypeError, ValueError):
			return None
		if len(frm) != 3 or len(to) != 3:
			return None
		lo = tuple(min(a, b) for a, b in zip(frm, to))
		hi = tuple(max(a, b) for a, b in zip(frm, to))
		return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])

	@staticmethod
	def _in_bounds(pos: Tuple[int, int, int], region: Dict[str, Any]) -> bool:
		bounds = WarehouseAudit._normalize_bounds(region)
		if bounds is None:
			return False
		lo, hi = bounds
		return all(lo[i] <= pos[i] <= hi[i] for i in range(3))

	def _maybe_rescan(self) -> None:
		interval = float(self.config.get('rescan_interval_seconds', 300))
		with self._state_lock:
			last = self.stats['last_rescan_ts']
		if time.time() - last >= interval:
			self.rescan_containers()

	def _containers_locked(self) -> bool:
		"""容器列表是否已锁定 (lock_containers=true 且已枚举过至少一次)。"""
		if not bool(self.config.get('lock_containers', False)):
			return False
		with self._state_lock:
			return self._containers_sealed

	def rescan_containers(self, source: Optional[CommandSource] = None) -> int:
		"""重新枚举容器。返回容器总数。

		lock_containers=true 时: 首次枚举后锁定列表, 之后的周期性 rescan
		直接跳过 (不再新增容器坐标), 手动 !!wh rescan 可强制解锁重扫。
		"""
		if self._containers_locked() and source is None:
			# 周期性 rescan 在锁定模式下跳过
			with self._state_lock:
				self.stats['last_rescan_ts'] = time.time()
				self.stats['last_rescan_method'] = 'locked'
			return len(self.containers)
		if not self._rescan_lock.acquire(blocking=False):
			if source is not None:
				source.reply('§7已有枚举任务在进行中§r')
			return -1
		try:
			count = self._rescan_containers_impl(source)
			# 大区域防护: 枚举失败 (返回 -1) 或发现 0 容器时不密封,
			# 允许下轮重试 —— 避免大区域首次枚举超时/失败后锁死空列表
			if count > 0 and bool(self.config.get('lock_containers', False)):
				with self._state_lock:
					self._containers_sealed = True
			return count
		finally:
			self._rescan_lock.release()

	def _rescan_containers_impl(self, source: Optional[CommandSource]) -> int:
		found: Dict[Tuple[int, int, int], str] = {}
		method_used = 'none'
		container_ids = set(str(b) for b in self.config.get('container_blocks', []))
		regions = self._regions()
		if not regions or not container_ids:
			with self._state_lock:
				self.stats['last_rescan_ts'] = time.time()
				self.stats['last_rescan_method'] = 'no-region'
				self.stats['last_rescan_count'] = 0
			return 0

		method = str(self.config.get('enumeration_method', 'auto')).lower()
		total_volume = 0
		for region in regions:
			bounds = self._normalize_bounds(region)
			if bounds:
				lo, hi = bounds
				total_volume += (hi[0] - lo[0] + 1) * (hi[1] - lo[1] + 1) * (hi[2] - lo[2] + 1)
		use_rcon = method == 'rcon_scan' or (
			method == 'auto' and total_volume <= int(self.config.get('rcon_scan_max_blocks', 10000)))
		# 大区域防护: RCON 逐块扫描超预算 (30 万块 = 60 秒, 占用 MCDR rcon 锁)
		# -> 自动切换区域文件解析 (不占 RCON, 不依赖区块加载)
		if use_rcon and total_volume > 50000:
			self.server.logger.warning(
				'WarehouseAudit: region volume {} too large for RCON scan, '
				'falling back to region-file parse'.format(total_volume))
			use_rcon = False

		if use_rcon and self.server.is_rcon_running():
			rcon_found, verified_absent, unloaded = self._rcon_scan(regions, container_ids)
			found.update(rcon_found)
			method_used = 'rcon_scan'
			# 性能优化: 缓存已确认的非容器坐标, 下次扫描跳过 (不再逐块查询非容器方块)
			if not self._containers_locked():
				with self._state_lock:
					self._verified_absent = verified_absent
			if unloaded > 0:
				# 有区块未加载: 用区域文件补齐 (但尊重 RCON 已确认不存在的位置)
				file_found = self._region_file_scan(regions, container_ids)
				for pos, block_id in file_found.items():
					if pos not in verified_absent:
						found.setdefault(pos, block_id)
				method_used = 'rcon_scan+region_file'
		else:
			found = self._region_file_scan(regions, container_ids)
			method_used = 'region_file'

		with self._state_lock:
			previous_count = len(self.containers)
			if not found and previous_count > 0:
				# 扫描一无所获但之前有容器: 可能是世界目录错误/IO 故障, 保留旧列表
				self.server.logger.warning(
					'WarehouseAudit: rescan found 0 containers but had {} before; keeping old list'.format(previous_count))
			elif not found and previous_count == 0:
				# 大区域防护: 枚举失败 (0 容器) - 不密封, 允许下轮重试
				# (world_dir 推断失败/RCON 超时/region file 读取失败都会走到这里)
				self.server.logger.warning(
					'WarehouseAudit: rescan found 0 containers in {} block(s); '
					'will retry (check world_directory config if this persists)'.format(total_volume))
			else:
				self.containers = dict(found)
				self.region_of = {}
				for region in regions:
					name = str(region.get('name', ''))
					for pos in self.containers:
						if self._in_bounds(pos, region):
							self.region_of[pos] = name
				for pos in list(self.snapshots):
					if pos not in self.containers:
						self.snapshots.pop(pos, None)
						self.miss_counts.pop(pos, None)
			self.stats['last_rescan_ts'] = time.time()
			self.stats['last_rescan_method'] = method_used
			self.stats['last_rescan_count'] = len(self.containers)
			count = len(self.containers)

		self.server.logger.info(
			'WarehouseAudit: container rescan done via {}, {} container(s) known'.format(method_used, count))
		if source is not None:
			source.reply('§a容器枚举完成§r (方式: {}, 共 {} 个)'.format(method_used, count))
		return count

	def _rcon_scan(self, regions, container_ids):
		"""逐块 RCON 扫描。返回 (found, verified_absent, unloaded_count)。

		性能优化: 已缓存的非容器坐标 (上次扫描确认) 直接跳过, 不再逐块查询。
		"""
		found: Dict[Tuple[int, int, int], str] = {}
		verified_absent: Set[Tuple[int, int, int]] = set()
		unloaded = 0
		query = self._rcon_query
		with self._state_lock:
			cached_absent = set(getattr(self, '_verified_absent', set()) or set())
		for region in regions:
			bounds = self._normalize_bounds(region)
			if not bounds:
				continue
			lo, hi = bounds
			for x in range(lo[0], hi[0] + 1):
				for y in range(lo[1], hi[1] + 1):
					for z in range(lo[2], hi[2] + 1):
						if self._stop_event.is_set() or not self.server.is_rcon_running():
							# 停止或 RCON 断开: 中止扫描 (避免 Bad file descriptor 刷屏)
							return found, verified_absent, unloaded
						pos = (x, y, z)
						if pos in cached_absent:
							# 已确认的非容器方块: 跳过 (性能优化)
							verified_absent.add(pos)
							continue
						resp = query('data get block {} {} {} id'.format(x, y, z))
						if resp is None:
							unloaded += 1
							continue
						text = resp.strip()
						if ERR_NOT_LOADED in text:
							unloaded += 1
							continue
						if ERR_NOT_BLOCK_ENTITY in text:
							verified_absent.add(pos)
							continue
						m = BLOCK_DATA_RE.match(text) or BLOCK_DATA_RE_LOOSE.match(text)
						if not m or (int(m.group(1)), int(m.group(2)), int(m.group(3))) != pos:
							continue
						try:
							block_id = parse_snbt(m.group(4))
						except SnbtError:
							continue
						if isinstance(block_id, str) and block_id in container_ids:
							found[pos] = block_id
						else:
							verified_absent.add(pos)
		return found, verified_absent, unloaded

	def _region_file_scan(self, regions, container_ids) -> Dict[Tuple[int, int, int], str]:
		"""解析世界区域文件中的 block_entities。不依赖区块加载状态。"""
		found: Dict[Tuple[int, int, int], str] = {}
		if not self.world_dir:
			self.server.logger.warning(
				'WarehouseAudit: world directory not resolved, region-file enumeration unavailable')
			return found
		if bool(self.config.get('save_before_rescan', False)) and self.server.is_rcon_running():
			self.server.rcon_query('save-all flush')
		for region in regions:
			bounds = self._normalize_bounds(region)
			if not bounds:
				continue
			lo, hi = bounds
			dimension = str(region.get('dimension', 'minecraft:overworld'))
			subdir = DIMENSION_REGION_SUBDIR.get(dimension)
			if subdir is None:
				self.server.logger.warning(
					'WarehouseAudit: unknown dimension {!r}, skipped'.format(dimension))
				continue
			region_dir = os.path.join(self.world_dir, subdir, 'region') if subdir \
				else os.path.join(self.world_dir, 'region')
			if not os.path.isdir(region_dir):
				continue
			cx1, cz1 = lo[0] >> 4, lo[2] >> 4
			cx2, cz2 = hi[0] >> 4, hi[2] >> 4
			rx1, rz1 = cx1 >> 5, cz1 >> 5
			rx2, rz2 = cx2 >> 5, cz2 >> 5
			for rx in range(rx1, rx2 + 1):
				for rz in range(rz1, rz2 + 1):
					path = os.path.join(region_dir, 'r.{}.{}.mca'.format(rx, rz))
					if not os.path.isfile(path):
						continue
					for chunk_x, chunk_z, root in self._iter_region_file_cached(path):
						if not (cx1 <= chunk_x <= cx2 and cz1 <= chunk_z <= cz2):
							continue
						# 方块实体: 1.13~1.20.x 在区块根节点; 同时兼容存放在 sections 内的格式
						be_lists = [root.get('block_entities') or []]
						for section in root.get('sections') or []:
							if isinstance(section, dict) and section.get('block_entities'):
								be_lists.append(section['block_entities'])
						for be_list in be_lists:
							for be in be_list:
								if not isinstance(be, dict):
									continue
								block_id = be.get('id')
								if not isinstance(block_id, str) or block_id not in container_ids:
									continue
								try:
									bx, by, bz = int(be['x']), int(be['y']), int(be['z'])
								except (KeyError, TypeError, ValueError):
									continue
								if lo[0] <= bx <= hi[0] and lo[1] <= by <= hi[1] and lo[2] <= bz <= hi[2]:
									found[(bx, by, bz)] = block_id
		return found

	def _iter_region_file_cached(self, path: str):
		"""带 (mtime,size) 缓存的区域文件遍历。"""
		try:
			st = os.stat(path)
			sig = (st.st_mtime, st.st_size)
		except OSError:
			return
		cached = self._region_file_cache.get(path)
		if cached is not None and cached[0] == sig[0] and cached[1] == sig[1]:
			results = cached[2]
		else:
			results = {}
			try:
				for chunk_x, chunk_z, root in iter_region_file_chunks(path):
					results[(chunk_x, chunk_z)] = root
			except OSError as e:
				self.server.logger.warning(
					'WarehouseAudit: cannot read region file {}: {}'.format(path, e))
				return
			self._region_file_cache[path] = (sig[0], sig[1], results)
		for (chunk_x, chunk_z), root in results.items():
			yield chunk_x, chunk_z, root

	# ------------------------------------------------------------------ 导出

	def export_csv(self, args: str, source: CommandSource) -> None:
		try:
			options = self._parse_export_args(args)
		except ValueError as e:
			source.reply('§c参数错误: {}§r'.format(e))
			source.reply('§7用法: !!wh export [--player 名字] [--days 天数] [--from 日期] [--to 日期] '
						'[--item 物品ID] [--container X,Y,Z] [--limit 行数] [--out 路径]§r')
			return
		if not os.path.isfile(self.db_path):
			source.reply('§c数据库文件尚不存在: {}§r'.format(self.db_path))
			return
		os.makedirs(self.export_dir, exist_ok=True)
		out_path = options['out'] or os.path.join(
			self.export_dir,
			'warehouse_audit_{}.csv'.format(datetime.datetime.now().strftime('%Y%m%d_%H%M%S')))
		try:
			count = self._write_csv(options, out_path)
		except Exception as e:
			self.server.logger.error('WarehouseAudit export failed:\n{}'.format(traceback.format_exc()))
			source.reply('§c导出失败: {}§r'.format(e))
			return
		source.reply('§a导出完成§r: {} 条记录 -> §b{}§r'.format(count, out_path))
		if span_from and span_to:
			source.reply('§7统计时间跨度: {} ~ {}§r'.format(span_from, span_to))
		try:
			info = self.server.get_server_information()
			if info and info.version:
				source.reply('§7服务端版本: Minecraft {}§r'.format(info.version))
		except Exception:
			pass

	@staticmethod
	def _parse_export_args(args: str) -> Dict[str, Any]:
		tokens = split_command_args(args)
		options: Dict[str, Any] = {
			'player': None, 'from_ts': None, 'to_ts': None, 'item': None,
			'container': None, 'limit': 500000, 'out': None,
		}

		def parse_date(text: str, end_of_day: bool) -> float:
			for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
				try:
					dt = datetime.datetime.strptime(text, fmt)
				except ValueError:
					continue
				if end_of_day and fmt == '%Y-%m-%d':
					dt = dt.replace(hour=23, minute=59, second=59)
				return dt.timestamp()
			raise ValueError('无法解析日期 {!r}, 请使用 YYYY-MM-DD[ HH:MM:SS]'.format(text))

		i = 0
		while i < len(tokens):
			token = tokens[i]
			if token in ('--player', '-p') and i + 1 < len(tokens):
				options['player'] = tokens[i + 1]
				i += 2
			elif token == '--item' and i + 1 < len(tokens):
				options['item'] = tokens[i + 1]
				i += 2
			elif token == '--container' and i + 1 < len(tokens):
				parts = tokens[i + 1].split(',')
				if len(parts) != 3:
					raise ValueError('--container 需要 X,Y,Z 三个整数')
				options['container'] = tuple(int(float(p)) for p in parts)
				i += 2
			elif token == '--days' and i + 1 < len(tokens):
				options['from_ts'] = time.time() - float(tokens[i + 1]) * 86400.0
				i += 2
			elif token == '--from' and i + 1 < len(tokens):
				options['from_ts'] = parse_date(tokens[i + 1], False)
				i += 2
			elif token == '--to' and i + 1 < len(tokens):
				options['to_ts'] = parse_date(tokens[i + 1], True)
				i += 2
			elif token == '--limit' and i + 1 < len(tokens):
				options['limit'] = max(1, int(tokens[i + 1]))
				i += 2
			elif token == '--out' and i + 1 < len(tokens):
				options['out'] = tokens[i + 1]
				i += 2
			else:
				raise ValueError('未知参数 {!r}'.format(token))
		return options

	def _write_csv(self, options: Dict[str, Any], out_path: str) -> Tuple[int, Optional[str], Optional[str]]:
		"""写出 CSV (带备注行), 返回 (行数, 最早时间, 最晚时间)。"""
		conditions: List[str] = []
		params: List[Any] = []
		if options['player']:
			conditions.append('player = ?')
			params.append(options['player'])
		if options['item']:
			conditions.append('item_id = ?')
			params.append(options['item'])
		if options['from_ts'] is not None:
			conditions.append('ts_unix >= ?')
			params.append(options['from_ts'])
		if options['to_ts'] is not None:
			conditions.append('ts_unix <= ?')
			params.append(options['to_ts'])
		if options['container']:
			conditions.append('x = ? AND y = ? AND z = ?')
			params.extend(options['container'])
		where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
		select_cols = ('ts, player, dimension, x, y, z, block, item_id, delta, '
					   'before_count, after_count, player_distance, ambiguous, contents ')
		sql = ('SELECT ' + select_cols +
			   'FROM container_changes{} ORDER BY id LIMIT ?').format(where)
		params.append(int(options['limit']))
		record_player = bool(self.config.get('record_player_id', False))
		record_dimension = bool(self.config.get('record_dimension', False))
		headers = csv_columns(record_player, record_dimension)
		conn = self._ro_connect()
		try:
			# 统计时间跨度 (与导出相同的筛选条件)
			span_sql = 'SELECT MIN(ts), MAX(ts), COUNT(*) FROM container_changes' + where
			span_row = conn.execute(span_sql, params[:-1]).fetchone()
			span_from, span_to, matched = (span_row if span_row else (None, None, 0))
			cursor = conn.execute(sql, params)
			count = 0
			with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
				writer = csv.writer(f)
				# 备注行 (以 # 开头, 数据工具可用 comment='#' 跳过)
				for remark in self._export_remarks(span_from, span_to, matched, int(options['limit'])):
					f.write(remark + '\r\n')
				writer.writerow(headers)
				while True:
					rows = cursor.fetchmany(1000)
					if not rows:
						break
					for row in rows:
						writer.writerow(self._format_csv_row(row, record_player, record_dimension))
						count += 1
			return count, span_from, span_to
		finally:
			conn.close()

	def _export_remarks(self, span_from, span_to, matched: int, limit: int) -> List[str]:
		"""CSV 顶部备注行: 统计时间跨度 / 服务端版本 / 导出时间 / 行数。"""
		remarks = []
		if matched and span_from and span_to:
			remarks.append('# 备注: 统计时间跨度 {} ~ {}'.format(span_from, span_to))
		else:
			remarks.append('# 备注: 统计时间跨度 (无匹配记录)')
		version = None
		try:
			info = self.server.get_server_information()
			version = info.version if info else None
		except Exception:
			version = None
		remarks.append('# 备注: 服务端版本 Minecraft {}'.format(version or '未知'))
		remarks.append('# 备注: 匹配记录 {} 条 (导出上限 {})'.format(matched, limit))
		remarks.append('# 备注: 导出时间 {}'.format(
			datetime.datetime.now().astimezone().isoformat(timespec='seconds')))
		return remarks

	@staticmethod
	def _format_csv_row(row, record_player: bool, record_dimension: bool) -> List[Any]:
		(ts, player, dimension, x, y, z, block, item_id, delta,
		 before, after, distance, ambiguous, contents) = row
		out = [ts]
		if record_dimension:
			out.append(dimension)
		out += [x, y, z, block, item_id, delta, before, after,
				'存入' if delta > 0 else '取出']
		if record_player:
			out += [player if player else '',
					'{:.1f}'.format(distance) if distance is not None else '',
					'是' if ambiguous else '']
		out.append(contents if contents else '')
		return out

	# ------------------------------------------------------------------ 状态

	def status_lines(self) -> List[str]:
		with self._state_lock:
			containers = len(self.containers)
			stats = dict(self.stats)
			queue_depth = self.record_queue.qsize()
		db_size = 0
		if os.path.isfile(self.db_path):
			db_size = os.path.getsize(self.db_path)
		db_rows = self._count_rows()
		lines = [
			'§7===== 仓库审计日志 状态 =====§r',
			'运行: §e{}§r | 轮询周期: §e{}§r'.format(
				'开启' if self.config.get('enabled', True) else '暂停',
				'{:.2f}s'.format(self.config.get('poll_interval_seconds', 1.0))),
			'记录范围: §e{}§r | 记录存入: §e{}§r | 记录玩家ID: §e{}§r | 匿名模式: §e{}§r | 合并窗口: §e{}§r'.format(
				'只记取出' if not self.config.get('record_deposits', False) else '取出+存入',
				'开' if self.config.get('record_deposits', False) else '关',
				'开' if self.config.get('record_player_id', False) else '关',
				'开 (编号 P1/P2...)' if self.config.get('anonymous_mode', False) else '关',
				'{:.1f}s'.format(float(self.config.get('merge_window_seconds', 1.0)))
				if float(self.config.get('merge_window_seconds', 1.0)) > 0 else '关'),
			'容器锁定: §e{}§r | 非容器缓存: §e{}§r 个坐标'.format(
				'已锁定' if self._containers_locked() else '关',
				len(getattr(self, '_verified_absent', set()) or set())),
			'已知容器: §e{}§r 个 | 队列深度: §e{}§r | 已写入: §e{}§r 条 | 丢弃: §e{}§r 条'.format(
				containers, queue_depth, stats['records_written'], stats['records_dropped']),
			'数据库: §e{}§r ({} 行, {:.1f} KB)'.format(self.db_path, db_rows, db_size / 1024.0),
			'最近枚举: §e{}§r ({} 个容器, {})'.format(
				self._fmt_ts(stats['last_rescan_ts']), stats['last_rescan_count'], stats['last_rescan_method']),
			'最近轮询: §e{}§r (耗时 {:.1f} ms)'.format(
				self._fmt_ts(stats['last_poll_ts']), stats['last_cycle_ms']),
			'世界目录: §e{}§r'.format(self.world_dir or '§c未解析§r'),
			'RCON: §e{}§r'.format('就绪' if self.server.is_rcon_running() else '§c未连接§r'),
		]
		if stats['last_error']:
			lines.append('最近错误: §c{}§r'.format(stats['last_error']))
		return lines

	@staticmethod
	def _fmt_ts(ts: float) -> str:
		if not ts:
			return '尚未执行'
		return datetime.datetime.fromtimestamp(ts).strftime('%H:%M:%S')

	def _count_rows(self) -> int:
		try:
			conn = self._ro_connect()
			try:
				return conn.execute('SELECT COUNT(*) FROM container_changes').fetchone()[0]
			finally:
				conn.close()
		except sqlite3.Error:
			return -1

	def container_list_lines(self) -> List[str]:
		with self._state_lock:
			containers = dict(self.containers)
		if not containers:
			return ['§e尚未枚举到任何容器§r']
		lines = ['§7共 {} 个容器:§r'.format(len(containers))]
		for pos in sorted(containers):
			lines.append('  §b{}§r §7{}§r'.format(', '.join(str(v) for v in pos), containers[pos]))
		return lines

	def recent_records_lines(self, limit: int = 10) -> List[str]:
		"""最近 N 条变化记录 (倒序), 用于游戏内查看。"""
		if not os.path.isfile(self.db_path):
			return ['§c数据库文件尚不存在§r']
		try:
			conn = self._ro_connect()
			try:
				cursor = conn.execute(
					'SELECT ts, player, dimension, x, y, z, block, item_id, delta, contents '
					'FROM container_changes ORDER BY id DESC LIMIT ?', (int(limit),))
				rows = cursor.fetchall()
			finally:
				conn.close()
		except sqlite3.Error as e:
			return ['§c查询失败: {}§r'.format(e)]
		if not rows:
			return ['§e暂无记录§r']
		record_player = bool(self.config.get('record_player_id', False))
		lines = ['§7===== 最近 {} 条仓库记录 (最新在上) =====§r'.format(len(rows))]
		for (ts, player, dimension, x, y, z, block, item_id, delta, contents) in rows:
			# 兼容 ISO (T 分隔) 与 '日期 时间' 两种格式, 只显示时分秒
			if 'T' in ts:
				time_part = ts.split('T')[-1]
			elif ' ' in ts:
				time_part = ts.split(' ')[-1]
			else:
				time_part = ts
			time_part = time_part[:8]
			block_short = str(block).replace('minecraft:', '')
			item_short = str(item_id).replace('minecraft:', '')
			color = '§c' if delta < 0 else '§a'
			parts = [
				'§7[{}]§r'.format(time_part),
				'§b({}, {}, {})§r'.format(x, y, z),
				'{}{:+d}§r'.format(color, delta),
				'§f{}§r'.format(item_short),
			]
			if record_player and player:
				parts.append('§e{}§r'.format(player))
			if contents:
				short = contents if len(contents) <= 60 else contents[:57] + '...'
				parts.append('§7内容: {}§r'.format(short))
			lines.append(' '.join(parts))
		return lines


# ===========================================================================
# 插件入口
# ===========================================================================

server_inst: Optional[PluginServerInterface] = None
audit: Optional[WarehouseAudit] = None


def on_load(server: PluginServerInterface, prev_module) -> None:
	global server_inst, audit
	server_inst = server
	audit = WarehouseAudit(server)
	audit.load_config()
	audit.ensure_db_schema()
	if not audit.world_dir:
		server.logger.warning(
			'WarehouseAudit: world directory not resolved; region-file enumeration disabled. '
			'Set "world_directory" in config/warehouse_audit/warehouse_audit.json if needed')
	server.logger.info(
		'WarehouseAudit v{} loaded | regions={} | containers_tracked={} | db={}'.format(
			server.get_self_metadata().version,
			len(audit._regions()),
			','.join(str(b) for b in audit.config.get('container_blocks', [])),
			audit.db_path))
	audit.start()
	# 重要: MCDR 只会自动调用 on_load/on_unload 等事件监听函数,
	# 不会自动调用模块级 register(); 必须在此显式注册指令
	register(server)
	# 初始枚举: 等待服务器与 RCON 就绪后在后台线程执行
	# 注意: 必须先捕获本实例引用, 否则插件卸载后全局 audit=None 会让线程崩溃
	audit_ref = audit

	def initial_scan():
		while audit_ref is not None and not audit_ref._stop_event.is_set():
			if server.is_server_running() and server.is_rcon_running():
				try:
					audit_ref.rescan_containers()
				except Exception:
					server.logger.error('WarehouseAudit initial scan failed:\n{}'.format(traceback.format_exc()))
				return
			time.sleep(1.0)

	threading.Thread(target=initial_scan, name='WarehouseAudit-InitScan', daemon=True).start()


def on_unload(server: PluginServerInterface) -> None:
	global audit
	if audit is not None:
		audit.stop()
		server.logger.info('WarehouseAudit stopped')
		audit = None


def on_server_startup(server: PluginServerInterface) -> None:
	"""服务器重启: 重新建立基线, 避免回档产生幽灵记录。"""
	if audit is not None:
		try:
			audit.rebaseline()
		except Exception:
			server.logger.error('WarehouseAudit rebaseline failed:\n{}'.format(traceback.format_exc()))


# ---------------------------------------------------------------- 指令处理

def _require_perm(level: int):
	def check(source: CommandSource) -> bool:
		return source.has_permission(level)
	return check


def show_help(source: CommandSource) -> None:
	source.reply('§7===== 仓库审计日志 (WarehouseAudit) =====§r')
	source.reply('§f{} help§r §7- 显示本帮助§r'.format(COMMAND_PREFIX))
	source.reply('§f{} status§r §7- 运行状态§r'.format(COMMAND_PREFIX))
	source.reply('§f{} list§r §7- 列出已知容器坐标§r'.format(COMMAND_PREFIX))
	source.reply('§f{} recent [条数]§r §7- 查看最近 N 条记录 (默认 10 条, 最多 100)§r'.format(COMMAND_PREFIX))
	source.reply('§f{} export [选项]§r §7- 导出 CSV (默认 export/ 目录)§r'.format(COMMAND_PREFIX))
	source.reply('§7  选项: --player 名字 | --days 天数 | --from 日期 | --to 日期 | '
				'--item 物品ID | --container X,Y,Z | --limit 行数 | --out 路径§r')
	source.reply('§f{} rescan§r §7- 立即重新枚举容器§r'.format(COMMAND_PREFIX))
	source.reply('§f{} on / off§r §7- 恢复/暂停轮询§r'.format(COMMAND_PREFIX))
	source.reply('§f{} region§r §7- 查看监控区域§r'.format(COMMAND_PREFIX))
	source.reply('§f{} region set <名称> <维度> <x1> <y1> <z1> <x2> <y2> <z2>§r §7- 添加/更新监控区域§r'.format(COMMAND_PREFIX))
	source.reply('§7  维度: overworld / the_nether / the_end; 例: !!wh region set 主仓库 overworld -10 99 -10 10 101 10§r')
	source.reply('§f{} region del <名称> / clear§r §7- 删除/清空监控区域§r'.format(COMMAND_PREFIX))


def show_status(source: CommandSource) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	for line in audit.status_lines():
		source.reply(line)


def show_list(source: CommandSource) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	for line in audit.container_list_lines():
		source.reply(line)


def show_recent(source: CommandSource, context: Optional[dict] = None) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	limit = 10
	if context and context.get('count') is not None:
		try:
			limit = max(1, min(int(context['count']), 100))
		except (TypeError, ValueError):
			limit = 10
	for line in audit.recent_records_lines(limit):
		source.reply(line)


def do_export(source: CommandSource, context: dict) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	audit.export_csv(context.get('args', ''), source)


def do_rescan(source: CommandSource) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	threading.Thread(
		target=lambda: audit.rescan_containers(source), name='WarehouseAudit-Rescan',
		daemon=True).start()
	source.reply('§7正在重新枚举容器...§r')


def set_enabled(source: CommandSource, value: bool) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	audit.config['enabled'] = value
	audit.server.save_config_simple(audit.config, file_name='warehouse_audit.json')
	source.reply('§a轮询已{}§r'.format('恢复' if value else '暂停'))


# ------------------------------------------------- 区域配置 (!!wh region)

def show_regions(source: CommandSource) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	regions = audit.config.get('regions') or []
	if not regions:
		source.reply('§e当前没有监控区域§r')
		source.reply('§7用法: !!wh region set <名称> <维度> <x1> <y1> <z1> <x2> <y2> <z2>§r')
		return
	source.reply('§7===== 监控区域 (共 {} 个) =====§r'.format(len(regions)))
	for region in regions:
		bounds = WarehouseAudit._normalize_bounds(region)
		if bounds is None:
			source.reply('§c{} §7(坐标配置无效)§r'.format(region.get('name')))
			continue
		lo, hi = bounds
		volume = (hi[0] - lo[0] + 1) * (hi[1] - lo[1] + 1) * (hi[2] - lo[2] + 1)
		source.reply('§b{}§r §7{}§r ({}) ~ ({}) §7共 {} 方块§r'.format(
			region.get('name', ''), region.get('dimension', ''),
			', '.join(str(v) for v in lo), ', '.join(str(v) for v in hi), volume))


def region_set(source: CommandSource, context: dict) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	name = str(context.get('name', '')).strip()
	dim_raw = str(context.get('dim', ''))
	dimension = DIMENSION_ALIASES.get(dim_raw.lower())
	if not name:
		source.reply('§c区域名称不能为空§r')
		return
	if dimension is None:
		source.reply('§c未知维度 {!r}§r §7(可用: overworld / the_nether / the_end)§r'.format(dim_raw))
		return
	try:
		coords = [int(context[k]) for k in ('x1', 'y1', 'z1', 'x2', 'y2', 'z2')]
	except (KeyError, TypeError, ValueError):
		source.reply('§c坐标必须是整数§r')
		return
	if any(abs(c) > WORLD_COORD_LIMIT for c in coords):
		source.reply('§c坐标超出世界范围 (±{})§r'.format(WORLD_COORD_LIMIT))
		return
	lo = [min(coords[0], coords[3]), min(coords[1], coords[4]), min(coords[2], coords[5])]
	hi = [max(coords[0], coords[3]), max(coords[1], coords[4]), max(coords[2], coords[5])]
	volume = (hi[0] - lo[0] + 1) * (hi[1] - lo[1] + 1) * (hi[2] - lo[2] + 1)
	new_region = {'name': name, 'dimension': dimension, 'from': lo, 'to': hi}
	# 防御性拷贝, 避免与模块级默认配置共享嵌套列表
	regions = [dict(r) if isinstance(r, dict) else r
			   for r in (audit.config.get('regions') or [])]
	replaced = False
	for i, region in enumerate(regions):
		if isinstance(region, dict) and region.get('name') == name:
			regions[i] = new_region
			replaced = True
			break
	if not replaced:
		regions.append(new_region)
	audit.config['regions'] = regions
	audit.server.save_config_simple(audit.config, file_name='warehouse_audit.json')
	source.reply('§a区域已{}§r: §b{}§r {} ({}) ~ ({}) §7共 {} 方块§r'.format(
		'更新' if replaced else '添加', name, dimension,
		', '.join(str(v) for v in lo), ', '.join(str(v) for v in hi), volume))
	if volume > 50000:
		source.reply('§e注意: 区域较大, 枚举可能需要较长时间§r')
	# 立即重新枚举生效
	threading.Thread(target=lambda: audit.rescan_containers(source),
					 name='WarehouseAudit-Rescan', daemon=True).start()


def region_del(source: CommandSource, context: dict) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	name = str(context.get('name', '')).strip()
	# 防御性拷贝, 避免与模块级默认配置共享嵌套列表
	regions = [r for r in (audit.config.get('regions') or [])]
	remaining = [r for r in regions if not (isinstance(r, dict) and r.get('name') == name)]
	if len(remaining) == len(regions):
		source.reply('§c找不到名为 {!r} 的区域§r'.format(name))
		return
	audit.config['regions'] = remaining
	audit.server.save_config_simple(audit.config, file_name='warehouse_audit.json')
	source.reply('§a已删除区域§r §b{}§r §7(剩余 {} 个)§r'.format(name, len(remaining)))
	threading.Thread(target=lambda: audit.rescan_containers(source),
					 name='WarehouseAudit-Rescan', daemon=True).start()


def region_clear(source: CommandSource) -> None:
	if audit is None:
		source.reply('§c插件未初始化§r')
		return
	count = len(audit.config.get('regions') or [])
	audit.config['regions'] = []
	audit.server.save_config_simple(audit.config, file_name='warehouse_audit.json')
	source.reply('§a已清空全部 {} 个监控区域§r'.format(count))
	threading.Thread(target=lambda: audit.rescan_containers(source),
					 name='WarehouseAudit-Rescan', daemon=True).start()


def register(server: PluginServerInterface) -> None:
	root = Literal(COMMAND_PREFIX)
	root.runs(show_help)
	root.then(Literal('help').runs(show_help))
	root.then(Literal('status').runs(show_status))
	root.then(Literal('list').runs(show_list))
	recent_node = Literal('recent').runs(show_recent)
	recent_node.then(Integer('count').at_min(1).at_max(100).runs(show_recent))
	root.then(recent_node)
	export_node = Literal('export').requires(_require_perm(2), lambda: '权限不足')
	export_node.runs(lambda src: do_export(src, {'args': ''}))
	export_node.then(GreedyText('args').runs(do_export))
	root.then(export_node)
	root.then(
		Literal('rescan')
		.requires(_require_perm(2), lambda: '权限不足')
		.runs(do_rescan)
	)
	root.then(
		Literal('on')
		.requires(_require_perm(2), lambda: '权限不足')
		.runs(lambda src: set_enabled(src, True))
	)
	root.then(
		Literal('off')
		.requires(_require_perm(2), lambda: '权限不足')
		.runs(lambda src: set_enabled(src, False))
	)
	region_node = Literal('region').requires(_require_perm(2), lambda: '权限不足')
	region_node.runs(show_regions)
	# 注意: MCDR 指令树中连续参数必须链式嵌套 (每个参数是前一个的子节点),
	# 回调挂在最后一个节点上
	region_node.then(
		Literal('set').then(
			Text('name').then(
				Text('dim').then(
					Integer('x1').then(
						Integer('y1').then(
							Integer('z1').then(
								Integer('x2').then(
									Integer('y2').then(
										Integer('z2').runs(region_set)
									)
								)
							)
						)
					)
				)
			)
		)
	)
	region_node.then(Literal('del').then(Text('name').runs(region_del)))
	region_node.then(Literal('clear').runs(region_clear))
	root.then(region_node)
	server.register_command(root)
	server.register_help_message(COMMAND_PREFIX, '仓库容器交互审计日志 (SQLite + CSV 导出)')
