#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mc_upscale.py — Minecraft 16x 资源包 → 32x 批量放大工具
=========================================================

设计目标
--------
1. 把 16x 材质包整体放大为 32x（默认 2x，可用 --scale 调整），保持原目录结构；
2. 使用可配置的超分模型（Real-ESRGAN / Scale2x / 最近邻 / Lanczos）；
3. 后处理：调色板量化（≤N 色）、超分伪影清除、硬边缘保留（不涂抹）；
4. GUI / 字体类贴图单独走一套参数，避免文字糊化；
5. 断点续跑（基于源文件 SHA-256 + 参数指纹的状态文件）；
6. 输出完整处理日志与分类统计报告；
7. 可选：与"参考风格包"对比，量化评估哪些类别需要人工重绘。

依赖
----
    基础（Scale2x / 最近邻 / Lanczos，无需 GPU）:
        pip install numpy pillow

    Real-ESRGAN（可选，CPU 也能跑，只是慢）:
        pip install torch torchvision
        # 权重下载到 weights/ 目录，或用 --weights 指定 .pth

用法示例
--------
    # 最快、零依赖、零伪影的 2x 转换（默认）
    python mc_upscale.py -i 26.3.zip -r "Faithful 32x - 26.3.zip"

    # Real-ESRGAN 放大（需要先装 torch 并下载权重）
    python mc_upscale.py -i 26.3.zip -m realesrgan-x4plus --tile 256

    # 断点续跑 / 只重新打包
    python mc_upscale.py -i 26.3.zip --zip-only

作者备注：Scale2x 内核为本文件内的 numpy 向量化实现，
参考 Eric Johnston 的 Scale2x / AdvMAME2x 公开算法规则。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import multiprocessing as mp
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # 材质包里有 288x1800 之类的大图，禁用保护

LOG = logging.getLogger("mc_upscale")

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

PACK_FORMATS: Dict[str, int] = {
    "1.20": 15, "1.20.1": 15,
    "1.20.2": 18,
    "1.20.3": 22, "1.20.4": 22,
    "1.20.5": 32, "1.20.6": 32,
    "1.21": 34, "1.21.1": 34,
    "1.21.2": 42, "1.21.3": 42,
    "1.21.4": 46,
    "1.21.5": 55,
    "1.21.6": 57,
    "1.21.7": 63,
    "1.21.8": 64,
}

# 只打包这些内容，源 zip 里常混入 JRE / class / META-INF 等安装残留
# （实际使用见 iter_pack_files 的黑名单逻辑，这里保留供参考）
COPY_ROOT_WHITELIST = {
    "pack.png", "pack.mcmeta", "pack.txt",
    "LICENSE", "LICENSE.txt", "LICENSE.md",
    "README.md", "README.txt", "readme.txt",
    "respackopts.json5", "respackopts.json",
}

# 分类 → 处理档案
PIXEL_CATS = {
    "block", "item", "entity", "particle", "painting",
    "trims", "mob_effect", "environment", "map",
}
# GUI / HUD 类：含文字、细线、固定布局，任何插值都会破坏，强制最近邻
GUI_CATS = {"gui", "font", "heart", "jei", "emi", "reipe", "recipebook",
            "hud", "icons", "widget", "overlay"}
# 渐变查找表：Lanczos，不能量化（否则出色带）
LUT_CATS = {"colormap", "palettes"}
# 平滑叠加层：暗角/附魔光/扭曲，不是像素画
SMOOTH_CATS = {"misc"}

# 各类别的人工重绘风险评估（数据 + 经验规则）
CATEGORY_ADVICE: Dict[str, str] = {
    "gui": "GUI 面板/图标/进度条，含大量细线与文字。已用最近邻避免糊化，但 16x 放大后边缘仍呈锯齿，建议人工重绘或直接采用参考包的同名文件。",
    "font": "字形图集（ascii.png 等）。任何插值都会破坏字形轮廓，必须人工重绘或使用参考包字体。",
    "misc": "暗角/附魔光/恶心扭曲/南瓜模糊等平滑渐变叠加层。不是像素画，量化会产生色带，已改用 Lanczos；如需完美效果建议人工重绘。",
    "colormap": "生物群系着色查找表（草地/树叶 tint）。Lanczos 放大足够，但 16x 采样精度会限制渐变细腻度，可人工重绘。",
    "palettes": "调色板条（1px 高）。Lanczos 放大即可，务必不要量化。",
    "environment": "太阳/月亮/雨雪。大块纯色 + 硬边，Scale2x 效果良好；月亮相位图建议人工对齐。",
    "entity": "生物/盔甲/玩家皮肤。细节密度高，Scale2x 可用但观感偏『硬』；这是最值得人工重绘或换用参考包的类别。",
    "trims": "盔甲纹饰（64x32 细密图案）。Scale2x 可用但纹样容易被抹，是较值得人工重绘的一类。",
    "painting": "画作（16x16~32x32 名画）。色阶丰富，已启用量化误差保护自动保留原色；Scale2x 放大可用。",
    "particle": "粒子贴图（8x8/5x5 极小）。放大 2x 后仍只有 16x/10x，游戏中观感提升有限，可接受。",
    "mob_effect": "状态效果图标（18x18）。Scale2x 可用。",
    "map": "地图图标（8x8，极小）。往返 PSNR 偏低是尺寸太小导致的相对效应（一个像素角占整图比例大），实际观感影响很小，人工重绘收益低。",
    "block": "方块贴图。Scale2x + 调色板量化效果最好的一类，通常无需人工重绘。",
    "item": "物品贴图。同 block，通常无需人工重绘。",
    "blocks": "Mod 的方块贴图目录（部分 Mod 用 blocks/ 而非 block/）。同 block，效果良好。",
    "items": "Mod 的物品贴图目录（部分 Mod 用 items/ 而非 item/）。同 item，效果良好。",
    "model": "实体模型贴图（64x64）。Scale2x 可用。",
    "models": "Mod 的模型贴图目录（64x32）。Scale2x 可用。",
    "heart": "血量/护盾 HUD 图标（9x9）。已用最近邻保持硬边；9x9 放大到 18x18 后尺寸依旧很小，人工重绘收益有限。",
    "other": "无法按路径分类的贴图（如 Respckopts 的 extend_helper/ 图集）。已按内容判定走像素画或平滑渐变处理。",
    "textures_root": "直接放在 textures/ 下的 HUD 图集（health/absorption/half_heart 等）。已按内容判定处理。",
    "jei": "JEI 物品管理器 GUI。含细线与文字，已用最近邻；如需完美观感建议用参考包或人工重绘。",
    "root": "pack.png 等根目录文件。最近邻放大即可。",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def setup_logging(log_path: Optional[Path], verbose: bool) -> None:
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def parse_pack_format(spec: str) -> Tuple[int, Optional[int]]:
    """'15' -> (15, None); '84-97' -> (84, 97); 'auto' -> (-1, None)"""
    spec = (spec or "auto").strip().lower()
    if spec in ("auto", ""):
        return (-1, None)
    m = re.match(r"^(\d+)\s*(?:-|\.\.|:)\s*(\d+)$", spec)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))
    if spec.isdigit():
        return (int(spec), None)
    raise argparse.ArgumentTypeError(f"无法解析 pack_format: {spec!r}（示例：15 / 84-97 / auto）")


# --------------------------------------------------------------------------- #
# 图像底层操作
# --------------------------------------------------------------------------- #

def to_rgba(img: Image.Image) -> Image.Image:
    return img.convert("RGBA") if img.mode != "RGBA" else img


def pack_rgba(arr: np.ndarray) -> np.ndarray:
    """HxWx4 uint8 -> HxW int64（把 RGBA 打包成单个整数，便于逐像素精确比较）"""
    a = arr.astype(np.int64)
    return (a[..., 0] << 24) | (a[..., 1] << 16) | (a[..., 2] << 8) | a[..., 3]


def unpack_rgba(packed: np.ndarray) -> np.ndarray:
    """HxW int64 -> HxWx4 uint8"""
    p = np.asarray(packed, dtype=np.int64)
    out = np.empty(p.shape + (4,), dtype=np.uint8)
    out[..., 0] = (p >> 24) & 0xFF
    out[..., 1] = (p >> 16) & 0xFF
    out[..., 2] = (p >> 8) & 0xFF
    out[..., 3] = p & 0xFF
    return out


def scale2x_array(rgba: np.ndarray) -> np.ndarray:
    """
    Scale2x / AdvMAME2x（Eric Johnston 公开算法）的 numpy 向量化实现。

    对每个源像素 E，取其 3x3 邻域::

        A B C
        D E F
        G H I

    输出 2x2 块，规则（仅在"真实斜边"上做圆角，绝不混合颜色）::

        E0 = (D == B and D != A and B != E) ? D : E     # 左上
        E1 = (B == F and B != C and B != E) ? B : E     # 右上
        E2 = (D == H and D != G and D != E) ? D : E     # 左下
        E3 = (F == H and F != I and F != E) ? F : E     # 右下

    因为条件里两侧邻色必然相等，取哪一侧结果相同。该算法只做"选色"，
    不产生任何插值色，因此硬边缘与 alpha 边界 100% 保留。
    """
    p = pack_rgba(rgba)
    h, w = p.shape
    # 用 -1 作为越界哨兵：与任何真实颜色都不同，边界像素不会发生圆角
    pad = np.full((h + 2, w + 2), -1, dtype=np.int64)
    pad[1:-1, 1:-1] = p

    A = pad[0:-2, 0:-2]
    B = pad[0:-2, 1:-1]
    C = pad[0:-2, 2:]
    D = pad[1:-1, 0:-2]
    E = pad[1:-1, 1:-1]
    F = pad[1:-1, 2:]
    G = pad[2:, 0:-2]
    H = pad[2:, 1:-1]
    I = pad[2:, 2:]

    e0 = np.where((D == B) & (D != A) & (B != E), D, E)
    e1 = np.where((B == F) & (B != C) & (B != E), B, E)
    e2 = np.where((D == H) & (D != G) & (D != E), D, E)
    e3 = np.where((F == H) & (F != I) & (F != E), F, E)

    out = np.empty((h * 2, w * 2), dtype=np.int64)
    out[0::2, 0::2] = e0   # 每个源像素对应输出 2x2 块的左上
    out[0::2, 1::2] = e1   # 右上
    out[1::2, 0::2] = e2   # 左下
    out[1::2, 1::2] = e3   # 右下
    return unpack_rgba(out)


def scale2x_image(img: Image.Image, passes: int = 1) -> Image.Image:
    """重复应用 Scale2x；passes=1 → 2x，passes=2 → 4x，以此类推。"""
    rgba = np.asarray(to_rgba(img), dtype=np.uint8)
    for _ in range(max(1, int(passes))):
        rgba = scale2x_array(rgba)
    return Image.fromarray(rgba, "RGBA")


def despeckle_array(rgba: np.ndarray, passes: int = 1, tol: int = 10) -> np.ndarray:
    """
    清除超分模型的孤立噪点（椒盐状伪影）。

    只处理"与上下左右四个邻居都明显不同"的像素，用 3x3 中值替换。
    因为条件是"四邻全异"，正常边缘像素不会被触碰 —— 边缘保持型去噪。
    只改 RGB，不动 alpha。
    """
    if passes <= 0:
        return rgba
    a = rgba.copy()
    for _ in range(int(passes)):
        c = a[..., :3].astype(np.int16)
        pc = np.pad(c, ((1, 1), (1, 1), (0, 0)), mode="edge")
        up, dn = pc[:-2, 1:-1], pc[2:, 1:-1]
        lf, rt = pc[1:-1, :-2], pc[1:-1, 2:]
        iso = (
            (np.abs(c - up).sum(-1) > tol)
            & (np.abs(c - dn).sum(-1) > tol)
            & (np.abs(c - lf).sum(-1) > tol)
            & (np.abs(c - rt).sum(-1) > tol)
        )
        iso &= a[..., 3] > 0  # 不碰全透明像素
        if not iso.any():
            break
        # 3x3 中值
        stack = np.stack(
            [pc[0:-2, 0:-2], pc[0:-2, 1:-1], pc[0:-2, 2:],
             pc[1:-1, 0:-2], c,              pc[1:-1, 2:],
             pc[2:, 0:-2],   pc[2:, 1:-1],   pc[2:, 2:]],
            axis=0,
        )
        med = np.median(stack, axis=0)
        a[iso, :3] = np.clip(med[iso] + 0.5, 0, 255).astype(np.uint8)
    return a


def harden_alpha(rgba: np.ndarray, thresh: int = 128) -> np.ndarray:
    """把 alpha 通道二值化，得到干净的硬边（可选后处理）。"""
    a = rgba.copy()
    a[..., 3] = np.where(a[..., 3] >= thresh, 255, 0).astype(np.uint8)
    return a


def fill_transparent(rgba: np.ndarray, iterations: int = 6) -> np.ndarray:
    """
    把全透明像素的 RGB 用邻近不透明像素"填"起来（多轮廉价膨胀）。

    神经网络模型会把透明区当成黑色，从而在可见边缘产生黑晕。
    先把颜色推进透明区，推理完再贴回原始 alpha，即可消除黑晕。
    只改 RGB，alpha 原样返回。
    """
    a = rgba.copy()
    rgb = a[..., :3].copy()
    src_mask = a[..., 3] > 0
    need = a[..., 3] == 0
    if not need.any():
        return a
    for _ in range(int(iterations)):
        if not need.any():
            break
        pc = np.pad(rgb, ((1, 1), (1, 1), (0, 0)), mode="edge")
        pm = np.pad(src_mask.astype(np.uint8), 1, mode="constant", constant_values=0)
        has_src = (pm[:-2, 1:-1] | pm[2:, 1:-1] | pm[1:-1, :-2] | pm[1:-1, 2:]) > 0
        cand = np.maximum.reduce([pc[:-2, 1:-1], pc[2:, 1:-1],
                                  pc[1:-1, :-2], pc[1:-1, 2:]])
        take = need & has_src
        rgb[take] = cand[take]
        src_mask |= take
        need &= ~take
    a[..., :3] = rgb
    return a


def count_colors(rgba: np.ndarray) -> int:
    return int(len(np.unique(pack_rgba(rgba).ravel())))


def quantize_palette(img: Image.Image, colors: int, dither: bool = False,
                     max_error: Optional[int] = 8) -> Image.Image:
    """
    把 RGB 量化到 ≤ colors 色，**alpha 通道原样保留**（不参与量化，硬边不丢）。

    实现要点：
      1. 色数已经在预算内 → 原样返回，不做任何量化（约 98.5% 的 Minecraft 贴图属于此类）；
      2. 否则用 MEDIANCUT 量化 RGB，再贴回原始 alpha。
         Pillow 的 FASTOCTREE 在 RGBA 上是有损的（实测 4 色图会被压成 3 色并发生色偏），
         LIBIMAGEQUANT 在官方 Windows wheel 里未编译进来，所以只用 MEDIANCUT；
      3. **误差保护**：量化后若可见像素上的最大 RGB 误差超过 max_error，
         说明这张图色阶太丰富（典型如 vanilla 画作、火焰、海晶灯），
         强行量化会出现色带，此时保留原始颜色。传 0 可关闭该保护；
      4. 量化前把全透明像素的 RGB 统一成哨兵值，使它们在量化后落到同一个索引，
         这样还能顺带输出带 tRNS 的 P 模式 PNG，体积更小。
    """
    if colors <= 0:
        return img
    rgba = to_rgba(img)
    a = np.asarray(rgba, dtype=np.uint8)
    colors = int(min(colors, 256))
    if count_colors(a) <= colors:
        return rgba  # 已在预算内，量化是纯无操作

    dm = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    work = a.copy()
    transparent = work[..., 3] == 0
    if transparent.any():
        work[transparent, :3] = 0  # 哨兵色，让透明像素聚集到同一索引

    q = Image.fromarray(work[..., :3], "RGB").quantize(
        colors=colors, method=Image.Quantize.MEDIANCUT, dither=dm)
    q_rgba = q.convert("RGBA")

    # 误差保护
    if max_error and max_error > 0:
        qa = np.asarray(q_rgba, dtype=np.int16)
        vis = a[..., 3] > 0
        if vis.any():
            err = int(np.abs(qa[..., :3][vis] - a[..., :3][vis].astype(np.int16)).max())
            if err > max_error:
                LOG.debug("量化误差 %d > %d，保留原始颜色（防止色带）", err, max_error)
                return rgba

    # 尝试保留为 P 模式 + tRNS（更小）；失败则退回 RGBA
    if transparent.any():
        idx = np.asarray(q)
        t_idx = np.unique(idx[transparent])
        if len(t_idx) == 1:
            try:
                q.info["transparency"] = int(t_idx[0])
                return q.convert("RGBA")
            except Exception:
                pass
    out = q_rgba
    out.putalpha(rgba.getchannel("A"))
    return out


# --------------------------------------------------------------------------- #
# 超分模型
# --------------------------------------------------------------------------- #

class BaseUpscaler:
    name = "base"

    def upscale(self, img: Image.Image, out_w: int, out_h: int) -> Image.Image:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


class NearestUpscaler(BaseUpscaler):
    name = "nearest"

    def upscale(self, img: Image.Image, out_w: int, out_h: int) -> Image.Image:
        return to_rgba(img).resize((out_w, out_h), Image.Resampling.NEAREST)


class LanczosUpscaler(BaseUpscaler):
    name = "lanczos"

    def upscale(self, img: Image.Image, out_w: int, out_h: int) -> Image.Image:
        return to_rgba(img).resize((out_w, out_h), Image.Resampling.LANCZOS)


class BoxUpscaler(BaseUpscaler):
    """先 Lanczos 放大到中间尺寸再 BOX 降采样，边缘比纯 Lanczos 硬"""
    name = "box"

    def upscale(self, img: Image.Image, out_w: int, out_h: int) -> Image.Image:
        src = to_rgba(img)
        sw, sh = src.size
        if sw == 0 or sh == 0:
            return src.resize((out_w, out_h), Image.Resampling.NEAREST)
        mid_w, mid_h = out_w * 2, out_h * 2
        big = src.resize((mid_w, mid_h), Image.Resampling.LANCZOS)
        return big.resize((out_w, out_h), Image.Resampling.BOX)


class Scale2xUpscaler(BaseUpscaler):
    name = "scale2x"

    def upscale(self, img: Image.Image, out_w: int, out_h: int) -> Image.Image:
        src = to_rgba(img)
        sw, sh = src.size
        if sw <= 0 or sh <= 0:
            return src.resize((max(1, out_w), max(1, out_h)), Image.Resampling.NEAREST)
        fw = out_w / sw
        fh = out_h / sh
        if fw != fh or fw < 1 or abs(fw - round(fw)) > 1e-9:
            # 非整数等比：退回 Lanczos（并给出警告一次）
            if not getattr(self, "_warned", False):
                LOG.warning("Scale2x 需要整数等比放大，%dx%d→%dx%d 回退 Lanczos",
                            sw, sh, out_w, out_h)
                self._warned = True
            return src.resize((out_w, out_h), Image.Resampling.LANCZOS)
        f = int(round(fw))
        # Scale2x 只支持 2 的幂；其它整数倍先 NEAREST 到最近的 2 的幂尺寸
        mult, passes = 1, 0
        while mult * 2 <= f:
            mult *= 2
            passes += 1
        if mult != f:
            LOG.warning("Scale2x 仅支持 2 的幂倍率，%dx 将按 %dx 处理后 NEAREST 缩放", f, mult)
            src = src.resize((sw * mult, sh * mult), Image.Resampling.NEAREST)
        out = scale2x_image(src, passes)
        return out.resize((out_w, out_h), Image.Resampling.NEAREST)


# ---- Real-ESRGAN（纯 PyTorch，免 basicsr / gfpgan / facexlib） ------------- #

RRDB_WEIGHTS = {
    "realesrgan": "RealESRGAN_x4plus.pth",
    "realesrgan-x4plus": "RealESRGAN_x4plus.pth",
    "realesrnet-x4plus": "RealESRNet_x4plus.pth",
    "realesrgan-x2plus": "RealESRGAN_x2plus.pth",
}
SRVGG_WEIGHTS = {
    "realesrgan-anime-6b": "RealESRGAN_x4plus_anime_6B.pth",
}
RRDB_ARCH = {
    "realesrgan": dict(scale=4, num_feat=64, num_block=23, num_grow_ch=32),
    "realesrgan-x4plus": dict(scale=4, num_feat=64, num_block=23, num_grow_ch=32),
    "realesrnet-x4plus": dict(scale=4, num_feat=64, num_block=23, num_grow_ch=32),
    # 注意：RealESRGAN_x2plus.pth 实测为 num_in_ch=12 的特殊模型（需要额外的
    # 多尺度下采样输入），不是普通 3 通道放大器，加载时会明确报错。
    "realesrgan-x2plus": dict(scale=2, num_feat=64, num_block=6, num_grow_ch=32),
}
WEIGHT_URLS = {
    "RealESRGAN_x4plus.pth": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
    "RealESRNet_x4plus.pth": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRNet_x4plus.pth",
    "RealESRGAN_x2plus.pth": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
    "RealESRGAN_x4plus_anime_6B.pth": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
}

NEURAL_MODELS = set(RRDB_WEIGHTS) | set(SRVGG_WEIGHTS)


def _canon_key(k: str) -> str:
    """键名归一化：去掉下划线并小写，使 conv_hr / HRconv / convHR 视为同一个键。"""
    return k.replace("_", "").replace("-", "").lower()


# 官方不同版本 RRDBNet 的层名差异（归一化后的形式）
_KEY_ALIASES = {
    "convbody": "trunk_conv",     # RealESRGAN_x2plus 用 conv_body
    "convup1": "upconv1",
    "convup2": "upconv2",
    "convup3": "upconv3",
    "convhr": "HRconv",
    "hrconv": "HRconv",
}


def _alias_key(k: str) -> str:
    """
    对权重键名做别名归一化，逐段替换模块名：
        conv_body.weight -> trunk_conv.weight
        conv_up1.weight  -> upconv1.weight
        conv_hr.weight   -> HRconv.weight
    不在别名表里的段原样保留。
    """
    parts = k.split(".")
    param = ""
    if parts and parts[-1] in ("weight", "bias", "running_mean", "running_var",
                               "num_batches_tracked"):
        param = parts[-1]
        parts = parts[:-1]
    segs = [_KEY_ALIASES.get(_canon_key(s), s) for s in parts]
    return ".".join(segs) + (("." + param) if param else "")


def _detect_rrdb_arch(state: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """
    从 RRDB 权重里反推架构参数。返回 None 表示不是 RRDB 风格的权重。

    实测 RealESRGAN_x2plus.pth 的真实架构是
    num_in_ch=12, num_block=23, num_grow_ch=32, scale=2，且层名为
    conv_body / conv_up1 —— 与网上流传的 "num_in_ch=3, num_block=6" 说法不符。
    所以不硬编码，一律从权重读。
    """
    if not isinstance(state, dict) or not state:
        return None
    cf = state.get("conv_first.weight")
    if cf is None or not hasattr(cf, "shape") or len(cf.shape) != 4:
        return None
    try:
        num_in_ch, num_feat = int(cf.shape[1]), int(cf.shape[0])
    except Exception:
        return None
    cl = state.get("conv_last.weight")
    num_out_ch = int(cl.shape[0]) if cl is not None and len(cl.shape) == 4 else 3

    body_idx = set()
    grow = 32
    for k, v in state.items():
        m = re.match(r"body\.(\d+)\.", k)
        if m:
            body_idx.add(int(m.group(1)))
            mm = re.match(r"body\.\d+\.rdb1\.conv1\.weight$", k)
            if mm and hasattr(v, "shape"):
                grow = int(v.shape[0])
    if not body_idx:
        return None
    return {
        "num_in_ch": num_in_ch,
        "num_out_ch": num_out_ch,
        "num_feat": num_feat,
        "num_block": len(body_idx),
        "num_grow_ch": grow,
        "scale": 0,  # 0 = 权重里看不出倍率，稍后由预设或前向传播确定
    }


def _torch():
    try:
        import torch  # noqa: F401
        return torch
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "未安装 PyTorch。请执行:  pip install torch  （CPU 版约 200MB）"
        ) from exc


def _build_rrdb(torch, nn, F, arch: Dict[str, int]):
    class ResidualDenseBlock(nn.Module):
        def __init__(self, num_feat=64, num_grow_ch=32):
            super().__init__()
            self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
            self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
            return x5 * 0.2 + x

    class RRDB(nn.Module):
        def __init__(self, num_feat, num_grow_ch=32):
            super().__init__()
            self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

        def forward(self, x):
            out = self.rdb3(self.rdb2(self.rdb1(x)))
            return out * 0.2 + x

    class RRDBNet(nn.Module):
        def __init__(self, num_in_ch=3, num_out_ch=3, scale=4,
                     num_feat=64, num_block=23, num_grow_ch=32):
            super().__init__()
            self.scale = scale
            self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
            self.body = nn.ModuleList([RRDB(num_feat, num_grow_ch) for _ in range(num_block)])
            self.trunk_conv = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.upconv1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.upconv2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            # upconv3 只在 scale=8 时使用；官方 x4plus 的权重里也没有 conv_up3，
            # 所以按需创建，否则 load_state_dict 会报缺失键。
            if scale == 8:
                self.upconv3 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.HRconv = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        def forward(self, x):
            feat = self.conv_first(x)
            trunk = feat
            for b in self.body:
                trunk = b(trunk)
            trunk = self.trunk_conv(trunk)
            feat = feat + trunk
            if self.scale == 2:
                feat = self.lrelu(self.upconv1(F.interpolate(feat, scale_factor=2, mode="nearest")))
            elif self.scale == 3:
                feat = self.lrelu(self.upconv1(F.interpolate(feat, scale_factor=3, mode="nearest")))
            elif self.scale == 4:
                feat = self.lrelu(self.upconv1(F.interpolate(feat, scale_factor=2, mode="nearest")))
                feat = self.lrelu(self.upconv2(F.interpolate(feat, scale_factor=2, mode="nearest")))
            elif self.scale == 8:
                feat = self.lrelu(self.upconv1(F.interpolate(feat, scale_factor=2, mode="nearest")))
                feat = self.lrelu(self.upconv2(F.interpolate(feat, scale_factor=2, mode="nearest")))
                feat = self.lrelu(self.upconv3(F.interpolate(feat, scale_factor=2, mode="nearest")))
            else:
                raise ValueError(f"RRDBNet 不支持 scale={self.scale}")
            return self.conv_last(self.lrelu(self.HRconv(feat)))

    return RRDBNet(**arch)


def _build_srvgg(torch, nn, F, arch: Dict[str, int]):
    class SRVGGNetCompact(nn.Module):
        def __init__(self, num_in_ch=3, num_out_ch=3, num_feat=64,
                     num_conv=16, upscale=4):
            super().__init__()
            self.scale = upscale
            body: List[nn.Module] = [nn.Conv2d(num_in_ch, num_feat, 3, 1, 1),
                                     nn.PReLU(num_parameters=num_feat)]
            for _ in range(num_conv):
                body += [nn.Conv2d(num_feat, num_feat, 3, 1, 1),
                         nn.PReLU(num_parameters=num_feat)]
            body += [nn.Conv2d(num_feat, num_out_ch * (upscale ** 2), 3, 1, 1)]
            self.body = nn.Sequential(*body)
            self.upsampler = nn.PixelShuffle(upscale)

        def forward(self, x):
            out = self.upsampler(self.body(x))
            base = F.interpolate(x, scale_factor=self.scale, mode="nearest")
            return out + base

    return SRVGGNetCompact(**arch)


class RealESRGANUpscaler(BaseUpscaler):
    """
    纯 PyTorch 的 Real-ESRGAN 推理器。

    为什么不用 `pip install realesrgan`：那个包强依赖 basicsr / gfpgan /
    facexlib / opencv，在 Python 3.12+ 上基本装不起来。这里只依赖 torch，
    直接加载官方 .pth 权重，行为与官方推理一致。
    """

    def __init__(self, model_name: str, weights: Optional[str], device: str,
                 tile: int = 0, fp32: bool = False,
                 rrdb_overrides: Optional[Dict[str, int]] = None):
        import torch
        import torch.nn as nn
        import torch.nn.functional as F

        self.torch = torch
        self.tile = int(tile or 0)
        self.fp32 = fp32
        self.model_name = model_name

        path = self._resolve_weights(weights)
        LOG.info("加载权重 %s (%s)", path, human(Path(path).stat().st_size))
        obj = torch.load(path, map_location="cpu", weights_only=False)
        state = obj
        if isinstance(obj, dict):
            for key in ("params_ema", "params", "state_dict", "model"):
                if key in obj and isinstance(obj[key], dict):
                    state = obj[key]
                    break

        # ---- 架构：优先从权重反推，其次用预设，最后用 CLI 覆盖 ------------ #
        detected = _detect_rrdb_arch(state)
        if detected is not None:
            arch = detected
            LOG.info("从权重推断出架构: %s", arch)
            if model_name in RRDB_ARCH:
                preset = RRDB_ARCH[model_name]
                diff = {k: (preset.get(k), v) for k, v in arch.items()
                        if preset.get(k) not in (None, v)}
                if diff:
                    LOG.warning("权重与 %s 的预设架构不一致（预设 vs 实际）: %s；以权重为准",
                                model_name, diff)
        elif model_name in RRDB_ARCH:
            arch = dict(RRDB_ARCH[model_name])
            LOG.info("无法从权重推断架构，使用 %s 预设: %s", model_name, arch)
        else:
            raise ValueError(f"未知模型 {model_name}，且无法从权重推断架构")

        # 倍率：预设优先；权重看不出时用预设；都没有则按 4
        if not arch.get("scale"):
            arch["scale"] = RRDB_ARCH.get(model_name, {}).get("scale", 4)

        if rrdb_overrides:
            arch.update({k: v for k, v in rrdb_overrides.items() if v is not None})
            LOG.info("应用 CLI 架构覆盖: %s", arch)

        if arch.get("num_in_ch", 3) != 3:
            raise RuntimeError(
                f"权重 {Path(path).name} 的 conv_first 需要 {arch['num_in_ch']} 个输入通道，"
                f"不是标准的 3 通道 RGB 放大器（例如 RealESRGAN_x2plus 需要额外的多尺度"
                f"下采样输入），本脚本不支持。请改用 RealESRGAN_x4plus.pth 或 "
                f"RealESRGAN_x4plus_anime_6B.pth。"
            )

        if model_name in RRDB_WEIGHTS or detected is not None:
            net = _build_rrdb(torch, nn, F, arch)
        else:
            net = _build_srvgg(torch, nn, F, dict(num_feat=64, num_conv=16, upscale=4))

        cleaned = {}
        for k, v in state.items():
            nk = k
            for pref in ("module.", "model."):
                if nk.startswith(pref):
                    nk = nk[len(pref):]
            cleaned[nk] = v
        # 官方不同版本/不同模型的权重键名有差异（例如 conv_hr / HRconv、
        # conv_body / trunk_conv、conv_up1 / upconv1）。先做别名归一化，再匹配。
        canon = {_canon_key(k): k for k in net.state_dict().keys()}
        remapped, unmatched = {}, []
        for k, v in cleaned.items():
            ak = _alias_key(k)
            mk = canon.get(_canon_key(ak))
            if mk is None:
                unmatched.append(k)
                remapped[k] = v
            else:
                remapped[mk] = v
        if unmatched:
            LOG.warning("权重里有 %d 个键无法对应到架构（将被忽略）: %s",
                        len(unmatched), unmatched[:6])
        try:
            net.load_state_dict(remapped, strict=True)
        except RuntimeError as exc:
            # 权重与架构不完全一致时给出可操作的提示，而不是一句 KeyError
            LOG.warning("权重与架构键名不完全匹配，尝试宽松加载: %s",
                        str(exc).splitlines()[0])
            missing, unexpected = net.load_state_dict(remapped, strict=False)
            if unexpected:
                LOG.warning("权重中未被使用的键: %s", list(unexpected)[:8])
            if missing:
                raise RuntimeError(
                    f"权重缺少关键层 {list(missing)[:8]}。"
                    f"可用 --rrdb-num-block / --rrdb-num-feat / --rrdb-scale 覆盖架构参数。"
                ) from exc

        dev = device
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(dev)
        net.eval().to(self.device)
        self.half = (not fp32) and self.device.type == "cuda"
        if self.half:
            net.half()
        self.net = net

        # 用一次前向传播实测真实倍率（比任何预设都可靠）
        self.native_scale = self._probe_scale(net, arch.get("num_in_ch", 3))
        if self.native_scale != arch.get("scale"):
            LOG.warning("架构设定的倍率 x%d 与前向传播实测的 x%d 不一致，以实测为准；"
                        "如结果异常可用 --rrdb-scale 指定",
                        arch.get("scale", 0), self.native_scale)
        LOG.info("Real-ESRGAN 就绪: model=%s device=%s 实测倍率=x%d precision=%s tile=%d",
                 model_name, self.device, self.native_scale,
                 "fp32" if fp32 else ("fp16" if self.half else "fp32"), self.tile)

    def _probe_scale(self, net, num_in_ch: int) -> int:
        """跑一个 8x8 假输入，测出模型真实的放大倍率。"""
        torch = self.torch
        try:
            with torch.no_grad():
                x = torch.zeros(1, num_in_ch, 8, 8, device=self.device)
                if self.half:
                    x = x.half()
                y = net(x)
            return max(1, int(y.shape[-1] // 8))
        except Exception as exc:
            LOG.warning("无法实测倍率（%s），按预设 scale=%d 处理",
                        exc, getattr(net, "scale", 4))
            return int(getattr(net, "scale", 4))

    # -- 权重解析 ---------------------------------------------------------- #
    @staticmethod
    def _resolve_weights(weights: Optional[str]) -> Path:
        """
        按优先级查找权重文件：
          1. --weights 指定的路径
          2. 环境变量 MC_UPSCALE_WEIGHTS
          3. 脚本同级 weights/ 目录
          4. 当前工作目录 weights/
          5. ~/.cache/mc-upscale/weights/
        """
        script_dir = Path(__file__).resolve().parent
        cands: List[Path] = []
        if weights:
            cands.append(Path(weights))
        env = os.environ.get("MC_UPSCALE_WEIGHTS")
        if env:
            cands.append(Path(env))
        cands += [script_dir / "weights" / f for f in WEIGHT_URLS]
        cands += [Path.cwd() / "weights" / f for f in WEIGHT_URLS]
        home = Path.home() / ".cache" / "mc-upscale" / "weights"
        cands += [home / f for f in WEIGHT_URLS]
        for c in cands:
            if c.is_file() and c.stat().st_size > 1024:
                return c
        raise FileNotFoundError(
            "找不到 Real-ESRGAN 权重文件。请下载后放到以下任一位置：\n"
            f"  {script_dir / 'weights'}\\  （脚本同级目录，推荐）\n"
            f"  {Path.cwd() / 'weights'}\\  （当前工作目录）\n"
            f"  {home}\\  （用户缓存目录）\n"
            "或用 --weights <路径> 直接指定。下载地址：\n" +
            "\n".join(f"  {u}" for u in WEIGHT_URLS.values())
        )

    # -- 推理 -------------------------------------------------------------- #
    def _infer(self, img_np: np.ndarray) -> np.ndarray:
        """img_np: HxWx3 uint8 → 返回 native_scale 倍放大的 HxWx3 uint8"""
        torch = self.torch

        @torch.no_grad()
        def run(t):
            if self.tile and max(t.shape[-2:]) > self.tile:
                return self._tiled(t)
            return self.net(t)

        x = torch.from_numpy(np.ascontiguousarray(img_np).astype(np.float32) / 255.0)
        x = x.permute(2, 0, 1).unsqueeze(0).to(self.device)
        if self.half:
            x = x.half()
        y = run(x).float().clamp(0, 1)
        out = y.squeeze(0).permute(1, 2, 0).cpu().numpy()
        return (out * 255.0 + 0.5).astype(np.uint8)

    def _tiled(self, t):
        """分块推理，避免大图爆显存（16x 贴图其实用不到，保留给大图）。"""
        torch = self.torch
        b, c, h, w = t.shape
        ts = self.tile
        oh, ow = h * self.native_scale, w * self.native_scale
        out = torch.zeros((b, c, oh, ow), dtype=torch.float32, device=t.device)
        step = max(1, ts - 16)
        for y in range(0, h, step):
            for x in range(0, w, step):
                ye, xe = min(y + ts, h), min(x + ts, w)
                ys, xs = max(0, ye - ts), max(0, xe - ts)
                patch = t[..., ys:ye, xs:xe]
                with torch.no_grad():
                    r = self.net(patch).float().clamp(0, 1)
                out[..., ys * self.native_scale:ye * self.native_scale,
                    xs * self.native_scale:xe * self.native_scale] = r
        return out

    def upscale(self, img: Image.Image, out_w: int, out_h: int) -> Image.Image:
        src = to_rgba(img)
        sw, sh = src.size
        if sw <= 0 or sh <= 0:
            return src.resize((max(1, out_w), max(1, out_h)), Image.Resampling.NEAREST)
        rgba = np.asarray(src, dtype=np.uint8)

        # 1) 透明区填色，避免黑晕
        work = fill_transparent(rgba, iterations=3)
        rgb = work[..., :3]

        # 2) 神经网络推理（原生倍数）
        big = self._infer(rgb)

        # 3) 缩放到目标尺寸：x4 模型要 2x 时，用 BOX 平均而非 Lanczos，边缘更硬
        big_img = Image.fromarray(big, "RGB")
        if big_img.size != (out_w, out_h):
            if big_img.width >= out_w and big_img.height >= out_h:
                big_img = big_img.resize((out_w, out_h), Image.Resampling.BOX)
            else:
                big_img = big_img.resize((out_w, out_h), Image.Resampling.LANCZOS)

        # 4) alpha 用最近邻放大，保证硬边
        alpha = src.getchannel("A").resize((out_w, out_h), Image.Resampling.NEAREST)
        out = big_img.convert("RGBA")
        out.putalpha(alpha)
        return out


def build_upscaler(name: str, weights: Optional[str] = None, device: str = "auto",
                   tile: int = 0, fp32: bool = False,
                   rrdb_overrides: Optional[Dict[str, int]] = None) -> BaseUpscaler:
    name = (name or "auto").lower()
    table = {
        "nearest": NearestUpscaler,
        "scale2x": Scale2xUpscaler,
        "lanczos": LanczosUpscaler,
        "box": BoxUpscaler,
    }
    if name in table:
        return table[name]()
    if name in NEURAL_MODELS:
        return RealESRGANUpscaler(name, weights, device, tile, fp32, rrdb_overrides)
    raise ValueError(
        f"未知模型 {name!r}；可选: {sorted(table)} + {sorted(NEURAL_MODELS)} + auto"
    )


def resolve_default_model(requested: str, has_torch: bool) -> str:
    """
    `auto` 的解析策略。

    实测（见 README「模型怎么选」一节）：对 Minecraft 像素画，Scale2x 的往返 PSNR
    明显高于 Real-ESRGAN（stone 45.1 vs 29.0 dB），且 ESRGAN 会把像素画抹糊、
    在平坦贴图上产生横向条带。因此即使装了 torch，默认也用 Scale2x，
    只在用户显式指定 --model realesrgan-* 时才走神经网络。
    """
    if requested != "auto":
        return requested
    return "scale2x"


# --------------------------------------------------------------------------- #
# 处理档案（按类别路由）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    quantize: int
    dither: bool
    despeckle: int
    harden_alpha: bool
    max_error: int = 8
    desc: str = ""


def default_profiles(model: str, colors: int, despeckle: int,
                     gui_model: str, lut_model: str, dither: bool,
                     harden_alpha: bool, max_error: int = 8) -> Dict[str, Profile]:
    neural = model in NEURAL_MODELS
    return {
        # 方块/物品/实体等像素画：边缘定向放大 + 调色板量化
        "pixel": Profile("pixel", model, colors, dither,
                         despeckle if neural else 0, harden_alpha, max_error,
                         "像素画：边缘定向放大 + 调色板量化（保留硬边）"),
        # GUI/字体：最近邻，绝不插值，避免文字糊化
        "gui": Profile("gui", gui_model, 0, False, 0, False, 0,
                       "GUI/字体：最近邻放大，文字保持锐利"),
        # 渐变查找表：Lanczos，不量化（否则出色带）
        "lut": Profile("lut", lut_model, 0, False, 0, False, 0,
                       "渐变查找表：Lanczos 放大，不量化"),
        # 平滑叠加层（暗角/附魔光）：Lanczos，不量化
        "smooth": Profile("smooth", lut_model, 0, False, 0, False, 0,
                          "平滑叠加层：Lanczos 放大，不量化"),
        # 根目录文件（pack.png）
        "root": Profile("root", "nearest", 0, False, 0, False, 0,
                        "根目录文件：最近邻放大"),
        # 无法按路径分类的（如 Respckopts 的 extend_helper/*.png）：
        # 运行时按内容判定，默认按像素画处理
        "other": Profile("other", model, colors, dither,
                         despeckle if neural else 0, harden_alpha, max_error,
                         "未分类贴图：按内容判定，默认按像素画处理"),
    }


def classify(rel: str) -> Tuple[str, str]:
    """
    返回 (category, profile_name)。

    命名空间无关：只要路径里出现 `.../textures/<类别>/...` 就按 <类别> 路由，
    因此同时支持
        assets/minecraft/textures/block/...
        assets/create/textures/block/...          （Mod 整合包，39 个命名空间）
        format_48/assets/minecraft/textures/gui/... （overlays 版本覆盖目录）
        older_version/1.18/assets/.../textures/...
    没有 textures 段的（如 Respckopts 的 extend_helper/material.png）归为 other。
    """
    parts = rel.replace("\\", "/").split("/")
    cat = None
    for i, p in enumerate(parts):
        if p == "textures" and i + 1 < len(parts) and parts[i + 1]:
            cat = parts[i + 1]
            # 去掉 .png 后缀（textures/icons.png 这类直接放在 textures/ 下的文件）
            if cat.lower().endswith((".png", ".jpg", ".jpeg")):
                cat = "textures_root"
            break
    if cat is None:
        return "other", "other"
    if cat in GUI_CATS:
        return cat, "gui"
    if cat in LUT_CATS:
        return cat, "lut"
    if cat in SMOOTH_CATS:
        return cat, "smooth"
    return cat, "pixel"


def pixel_art_score(rgba: np.ndarray) -> float:
    """
    0~1 的"像素画程度"：相邻像素同色比例。
    像素画（硬边、大块同色）通常 0.4~0.95；平滑渐变/照片通常 < 0.15。
    """
    if rgba.size == 0:
        return 0.0
    c = pack_rgba(rgba)
    if c.shape[0] < 2 or c.shape[1] < 2:
        return 1.0
    r = (c[:-1, :] == c[1:, :]).mean() + (c[:, :-1] == c[:, 1:]).mean()
    return float(r / 2)


def looks_like_pixel_art(rgba: np.ndarray) -> bool:
    """色数少或同色邻接比例高 => 像素画，走 Scale2x；否则按平滑渐变走 Lanczos。"""
    n = count_colors(rgba)
    total = max(1, rgba.shape[0] * rgba.shape[1])
    return n <= 1024 or n <= total * 0.5 or pixel_art_score(rgba) > 0.35


# --------------------------------------------------------------------------- #
# 阶段一：解压 / 暂存
# --------------------------------------------------------------------------- #

def iter_pack_files(zip_path: Path) -> Iterable[Tuple[str, int]]:
    """
    产出资源包里需要保留的 (路径, 原始大小)。

    采用**黑名单**而非白名单：除了已知的安装残留（内置 JRE / 签名 / macOS 垃圾），
    其余全部保留。这是因为现代资源包大量使用 assets/ 之外的目录：

      - pack.mcmeta 的 overlays.entries[].directory 指向的版本覆盖目录
        （如 format_27/ format_42/ ...）
      - Respckopts 的 older_version/<版本>/ 与 customization/<选项>/
      - Respckopts 的 extend_helper/ 图集
      - 根目录的 respackopts.json5 / LICENSE / README

    早前的白名单（只保留 assets/** + 少数根文件）会把这些全部静默丢掉，
    导致覆盖层失效、选项丢失。被剔除的路径会打日志，便于核对。
    """
    # 顶层垃圾目录：内置 Java 运行时与签名
    junk_tops = {"com", "net", "data", "META-INF", "__MACOSX", "jre", "runtime"}
    # 顶层垃圾文件
    junk_roots = {"version.json", "flightrecorder-config.jfc", ".mcassetsroot",
                  ".DS_Store", "Thumbs.db", "desktop.ini"}
    excluded: List[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            base = name.rsplit("/", 1)[-1]
            top = name.split("/", 1)[0]
            if not base or base.startswith("._") or base == ".DS_Store":
                continue
            if top in junk_tops or ("/" not in name and base in junk_roots):
                excluded.append(name)
                continue
            yield name, info.file_size
    if excluded:
        LOG.info("已剔除 %d 个非资源包文件（安装残留）: %s%s",
                 len(excluded), ", ".join(sorted(excluded)[:6]),
                 " ..." if len(excluded) > 6 else "")


def stage_source(input_path: Path, src_dir: Path, signature: str) -> Dict[str, str]:
    """
    把 zip 里属于资源包的文件解压到 src_dir，返回 {相对路径: sha256}。
    已暂存过且签名一致时直接读 manifest，实现秒级续跑。
    """
    manifest_path = src_dir / ".manifest.json"
    if (src_dir / ".staged_ok").exists() and manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if data.get("signature") == signature:
                LOG.info("源包已暂存，跳过解压（%d 个文件）", len(data.get("files", {})))
                return data["files"]
        except Exception:
            pass

    LOG.info("解压源包 → %s", src_dir)
    if src_dir.exists():
        shutil.rmtree(src_dir, ignore_errors=True)
    src_dir.mkdir(parents=True, exist_ok=True)

    files: Dict[str, str] = {}
    with zipfile.ZipFile(input_path) as zf:
        members = list(iter_pack_files(zip_path=input_path))
        for name, _size in members:
            dest = src_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as fsrc, dest.open("wb") as fdst:
                shutil.copyfileobj(fsrc, fdst, 1 << 20)
            files[name] = sha256_file(dest)
    atomic_write_json(manifest_path, {"signature": signature, "files": files})
    (src_dir / ".staged_ok").write_text(signature, encoding="utf-8")
    LOG.info("解压完成：%d 个文件，%s", len(files),
             human(sum((src_dir / n).stat().st_size for n in files)))
    return files


def zip_signature(path: Path) -> str:
    st = path.stat()
    return f"{path.name}|{st.st_size}|{int(st.st_mtime)}"


# --------------------------------------------------------------------------- #
# 阶段二：处理单张贴图
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    rel: str
    category: str
    profile: str
    src_sha: str
    width: int
    height: int


@dataclass
class Result:
    rel: str
    status: str            # ok | fail | skip
    category: str = ""
    profile: str = ""
    detail: str = ""
    ms: float = 0.0
    in_size: Tuple[int, int] = (0, 0)
    out_size: Tuple[int, int] = (0, 0)
    colors_in: int = 0
    colors_out: int = 0
    bytes_out: int = 0


# 每个 worker 进程持有一份模型实例
_W: Dict[str, Any] = {}


def _worker_init(cfg: Dict[str, Any]) -> None:
    _W["cfg"] = cfg
    _W["upscalers"] = {}


def _get_upscaler(profile_name: str, cfg: Dict[str, Any]) -> BaseUpscaler:
    key = f"{profile_name}:{cfg['profiles'][profile_name].model}"
    cache = _W.setdefault("upscalers", {})
    if key not in cache:
        p = cfg["profiles"][profile_name]
        cache[key] = build_upscaler(
            p.model, cfg["weights"], cfg["device"],
            cfg["tile"], cfg["fp32"], cfg["rrdb"],
        )
    return cache[key]


def _process_one(job: Job, cfg: Dict[str, Any], src_dir: Path, out_dir: Path) -> Result:
    t0 = time.perf_counter()
    prof: Profile = cfg["profiles"][job.profile]
    src_path = src_dir / job.rel
    dst_path = out_dir / job.rel
    try:
        with Image.open(src_path) as im:
            im.load()
            src = to_rgba(im)
        sw, sh = src.size
        out_w, out_h = sw * cfg["scale"], sh * cfg["scale"]

        arr = np.asarray(src, dtype=np.uint8)
        colors_in = count_colors(arr)

        up = _get_upscaler(job.profile, cfg)
        out = up.upscale(src, out_w, out_h)
        if out.mode != "RGBA":
            out = out.convert("RGBA")

        a = np.asarray(out, dtype=np.uint8)
        if prof.despeckle:
            a = despeckle_array(a, passes=prof.despeckle)
        if prof.harden_alpha:
            a = harden_alpha(a)
        if prof.quantize:
            out = quantize_palette(Image.fromarray(a, "RGBA"), prof.quantize,
                                   prof.dither, prof.max_error)
            a = np.asarray(out, dtype=np.uint8)
        out = Image.fromarray(a, "RGBA")

        colors_out = count_colors(a)

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst_path, format="PNG", optimize=True)
        return Result(
            rel=job.rel, status="ok", category=job.category, profile=job.profile,
            ms=(time.perf_counter() - t0) * 1000, in_size=(sw, sh), out_size=(out_w, out_h),
            colors_in=colors_in, colors_out=colors_out,
            bytes_out=dst_path.stat().st_size,
        )
    except Exception as exc:
        return Result(rel=job.rel, status="fail", category=job.category,
                      profile=job.profile, detail=f"{type(exc).__name__}: {exc}",
                      ms=(time.perf_counter() - t0) * 1000)


def _worker_entry(args: Tuple[Job, Dict[str, Any], str, str]) -> Result:
    job, cfg, src_dir, out_dir = args
    return _process_one(job, cfg, Path(src_dir), Path(out_dir))


# --------------------------------------------------------------------------- #
# 状态 / 断点续跑
# --------------------------------------------------------------------------- #

class State:
    def __init__(self, path: Path, params_hash: str):
        self.path = path
        self.params_hash = params_hash
        self.data: Dict[str, Any] = {"version": 3, "params_hash": params_hash, "files": {}}
        if path.is_file():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                if old.get("params_hash") == params_hash:
                    self.data = old
            except Exception:
                pass

    def is_done(self, rel: str, sha: str) -> bool:
        e = self.data["files"].get(rel)
        return bool(e) and e.get("sha") == sha and e.get("status") == "ok"

    def mark(self, rel: str, sha: str, res: Result) -> None:
        self.data["files"][rel] = {
            "sha": sha,
            "status": res.status,
            "category": res.category,
            "profile": res.profile,
            "detail": res.detail,
            "ms": round(res.ms, 2),
            "in_size": (f"{res.in_size[0]}x{res.in_size[1]}"
                        if res.in_size[0] else ""),
            "out_size": (f"{res.out_size[0]}x{res.out_size[1]}"
                         if res.out_size[0] else ""),
        }

    def save(self) -> None:
        atomic_write_json(self.path, self.data)


# --------------------------------------------------------------------------- #
# 阶段三：打包
# --------------------------------------------------------------------------- #

def build_zip(pack_dir: Path, out_zip: Path, compresslevel: int = 6) -> Tuple[int, int]:
    files = sorted(p for p in pack_dir.rglob("*") if p.is_file())
    total = 0
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_zip.with_suffix(out_zip.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as zf:
        for p in files:
            rel = p.relative_to(pack_dir).as_posix()
            if rel.startswith(".") or p.name.startswith("."):
                continue
            zf.write(p, rel)
            total += p.stat().st_size
    os.replace(tmp, out_zip)
    return len(files), total


def write_pack_mcmeta(pack_dir: Path, description: str,
                      min_f: Optional[int], max_f: Optional[int]) -> Path:
    pack: Dict[str, Any] = {"description": description}
    if min_f is not None and max_f is not None and min_f != max_f:
        pack["min_format"] = min_f
        pack["max_format"] = max_f
    elif min_f is not None:
        pack["pack_format"] = min_f
    path = pack_dir / "pack.mcmeta"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pack": pack}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def _as_int(v: Any) -> Optional[int]:
    """pack.mcmeta 里的格式字段可能是 int、[int]、{"min":..} 或字符串，统一转 int。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    if isinstance(v, (list, tuple)) and v:
        ints = [_as_int(x) for x in v]
        ints = [x for x in ints if x is not None]
        return min(ints) if ints else None
    if isinstance(v, dict):
        ints = [_as_int(x) for x in v.values()]
        ints = [x for x in ints if x is not None]
        return min(ints) if ints else None
    return None


def detect_pack_format(zip_path: Path) -> Tuple[Optional[int], Optional[int]]:
    """
    读取资源包（或参考包）的 pack.mcmeta，返回 (min_format, max_format)。

    兼容各种写法：
        {"pack_format": 42}
        {"min_format": 16, "max_format": 97}
        {"min_format": [16], "max_format": [999]}      （ Respckopts 风格）
        {"supported_formats": [16, 999]}
    取不到时返回 (None, None)。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if "pack.mcmeta" not in zf.namelist():
                return None, None
            meta = json.loads(zf.read("pack.mcmeta").decode("utf-8-sig"))
            pack = meta.get("pack", meta) or {}
            lo = _as_int(pack.get("min_format"))
            hi = _as_int(pack.get("max_format"))
            sf = pack.get("supported_formats")
            if sf is not None:
                sflo, sfhi = _as_int(sf), _as_int(list(reversed(sf)) if isinstance(sf, list) else sf)
                if lo is None:
                    lo = sflo
                if hi is None:
                    hi = sfhi if sfhi is not None else sflo
            pf = _as_int(pack.get("pack_format"))
            if lo is None and pf is not None:
                lo = hi = pf
            if lo is not None and hi is None:
                hi = lo
            if hi is not None and lo is None:
                lo = hi
            return lo, hi
    except Exception as exc:
        LOG.warning("读取 %s 的 pack.mcmeta 失败: %s", zip_path.name, exc)
    return None, None


# --------------------------------------------------------------------------- #
# 参考包对比（质量评估）
# --------------------------------------------------------------------------- #

def _edge_energy(rgba: np.ndarray) -> float:
    """
    平均"可见边缘强度"：只在两侧都可见的像素边上统计 |ΔRGB|，并按边数归一化。

    这样跨图可比，且完全不受透明区里无意义的 RGB 噪声影响。
    """
    a = (rgba[..., 3] > 0).astype(np.float32)
    g = rgba[..., :3].astype(np.float32).mean(-1)
    gx = np.abs(np.diff(g, axis=1)) * a[:, :-1] * a[:, 1:]
    gy = np.abs(np.diff(g, axis=0)) * a[:-1, :] * a[1:, :]
    n = float((a[:, :-1] * a[:, 1:]).sum() + (a[:-1, :] * a[1:, :]).sum())
    if n <= 0:
        return 0.0
    return float((gx.sum() + gy.sum()) / n)


def _masked_psnr(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    """只在 mask 为真的像素上计算 PSNR（透明像素的 RGB 没有意义，必须排除）。"""
    m = mask.astype(np.float32)[..., None]
    d = a[..., :3].astype(np.float32) - b[..., :3].astype(np.float32)
    denom = max(1.0, float(m.sum()) * 3.0)
    mse = float(((d ** 2) * m).sum() / denom)
    if mse <= 1e-9:
        return 99.0
    if float(m.sum()) <= 0:
        return float("nan")
    return float(10.0 * math.log10((255.0 ** 2) / mse))


def verify_against_reference(pack_dir: Path, src_dir: Path, ref_zip: Path,
                             scale: int, max_samples: int = 400) -> Dict[str, Any]:
    """
    对同时存在于「我的输出」「源包」「参考包」中的贴图做三项指标：

      roundtrip_psnr : 把我的输出用 BOX 降采样回原尺寸，与源图在**可见像素**上比较。
                       → 衡量"颜色保真度"。最近邻恒为 99（完全无损）。
      edge_ratio     : 我的输出可见边缘强度 / 参考包可见边缘强度。
                       → 衡量"锐利度是否接近人工重绘的参考包"。
      colors         : 输出图的唯一颜色数。
    """
    with zipfile.ZipFile(ref_zip) as zf:
        ref_names = {n for n in zf.namelist() if n.lower().endswith(".png")}
    mine = {p.relative_to(pack_dir).as_posix(): p
            for p in pack_dir.rglob("*.png")}
    common = sorted(r for r in mine
                    if r in ref_names and (src_dir / r).is_file())
    if max_samples and len(common) > max_samples:
        step = len(common) / max_samples
        common = [common[int(i * step)] for i in range(max_samples)]

    per_cat: Dict[str, Dict[str, List[float]]] = {}
    with zipfile.ZipFile(ref_zip) as zf:
        for rel in common:
            try:
                cat, _prof = classify(rel)
                with Image.open(src_dir / rel) as s:
                    s.load()
                    src = np.asarray(to_rgba(s), dtype=np.uint8)
                with Image.open(mine[rel]) as m:
                    m.load()
                    out = np.asarray(to_rgba(m), dtype=np.uint8)
                with zf.open(rel) as fsrc:
                    with Image.open(fsrc) as r:
                        r.load()
                        ref = np.asarray(to_rgba(r), dtype=np.uint8)
                if ref.shape[:2] != out.shape[:2]:
                    ref = np.asarray(to_rgba(Image.fromarray(ref, "RGBA").resize(
                        (out.shape[1], out.shape[0]), Image.Resampling.LANCZOS)),
                        dtype=np.uint8)
                # 可见像素掩码（由源图 alpha 最近邻放大得到）
                vis_src = src[..., 3] > 0
                if not vis_src.any():
                    continue
                vis_out = np.asarray(Image.fromarray(
                    (vis_src * 255).astype(np.uint8), "L").resize(
                    (out.shape[1], out.shape[0]), Image.Resampling.NEAREST)) > 127

                # BOX 降采样回原尺寸，与源图比较颜色保真度
                small = np.asarray(Image.fromarray(out, "RGBA").resize(
                    (src.shape[1], src.shape[0]), Image.Resampling.BOX), dtype=np.uint8)
                psnr = _masked_psnr(small, src, vis_src)

                # 边缘强度对比（参考图边缘过弱时无意义，跳过该样本）
                e_mine = _edge_energy(out)
                e_ref = _edge_energy(ref)
                if e_ref <= 1e-4:
                    continue

                d = per_cat.setdefault(cat, {"psnr": [], "edge": [], "colors": []})
                if not math.isnan(psnr):
                    d["psnr"].append(psnr)
                d["edge"].append(e_mine / e_ref)
                d["colors"].append(len(np.unique(pack_rgba(out).ravel())))
            except Exception as exc:
                LOG.debug("verify 跳过 %s: %s", rel, exc)
    summary: Dict[str, Any] = {}
    for cat, d in sorted(per_cat.items()):
        n = max(1, len(d["psnr"]))
        ne = max(1, len(d["edge"]))
        summary[cat] = {
            "samples": max(len(d["psnr"]), len(d["edge"])),
            "roundtrip_psnr_db": round(sum(d["psnr"]) / n, 2) if d["psnr"] else None,
            "edge_ratio_vs_reference": round(sum(d["edge"]) / ne, 3) if d["edge"] else None,
            "avg_colors": int(sum(d["colors"]) / max(1, len(d["colors"]))),
        }
    return summary


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def write_report(report_path: Path, md_path: Path, stats: Dict[str, Any],
                 verify: Optional[Dict[str, Any]]) -> None:
    atomic_write_json(report_path, stats)
    lines: List[str] = []
    ap = lines.append
    ap("# 材质包 16x → 32x 处理报告")
    ap("")
    ap(f"- 源包：`{stats['input']}`")
    ap(f"- 输出：`{stats['output']}`")
    ap(f"- 模型：`{stats['model']}`　倍率：**{stats['scale']}x**　调色板量化：**{stats['colors']} 色**")
    ap(f"- 用时：{stats['elapsed']}　并行：{stats['workers']}")
    ap("")
    ap("## 总体统计")
    ap("")
    ap("| 项目 | 数量 |")
    ap("| --- | --- |")
    for k in ("total", "ok", "fail", "skip", "copy", "png_total", "png_ok"):
        if k in stats:
            ap(f"| {k} | {stats[k]} |")
    ap("")
    ap("## 分类统计")
    ap("")
    ap("| 类别 | 处理档案 | 成功 | 失败 | 跳过 | 平均耗时 | 输入尺寸 | 输出尺寸 |")
    ap("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for cat, c in stats["by_category"].items():
        ap(f"| {cat} | {c.get('profile','')} | {c['ok']} | {c['fail']} | {c['skip']} "
           f"| {c['avg_ms']:.0f} ms | {c.get('sizes','')} | {c.get('out_sizes','')} |")
    ap("")
    if stats.get("failures"):
        ap("## 失败明细")
        ap("")
        for rel, detail in stats["failures"][:60]:
            ap(f"- `{rel}` — {detail}")
        ap("")
    ap("## 需要人工重绘的类别")
    ap("")
    ap("| 类别 | 风险 | 说明 |")
    ap("| --- | --- | --- |")
    for cat, risk, note in stats["manual_redraw"]:
        ap(f"| {cat} | {risk} | {note} |")
    ap("")
    if verify:
        ap("## 与参考风格包的质量对比")
        ap("")
        ap("| 类别 | 样本 | 往返 PSNR (dB) | 边缘锐利度比 (我的/参考) | 平均色数 |")
        ap("| --- | --- | --- | --- | --- |")
        for cat, v in verify.items():
            ps = v.get("roundtrip_psnr_db")
            er = v.get("edge_ratio_vs_reference")
            ap(f"| {cat} | {v['samples']} "
               f"| {'-' if ps is None else ps} "
               f"| {'-' if er is None else er} | {v['avg_colors']} |")
        ap("")
        ap("> `往返 PSNR` = 把放大结果 BOX 降采样回原尺寸后，与源图在**可见像素**上的相似度，"
           "越高越保真；最近邻恒为 99（数学上完全保真）。")
        ap("> `边缘锐利度比` ≈ 1 表示与人工重绘的参考包锐利度一致，> 1 偏硬/偏噪，< 1 偏糊。")
        ap("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def manual_redraw_table(by_cat: Dict[str, Any], verify: Optional[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """综合「类别特性」+「实测指标」给出人工重绘风险等级。"""
    rows: List[Tuple[str, str, str]] = []
    for cat, c in sorted(by_cat.items(), key=lambda kv: -kv[1]["total"]):
        note = CATEGORY_ADVICE.get(cat, "常规贴图，超分效果通常可接受。")
        if cat in GUI_CATS:
            risk = "高"
        elif cat in ("font",):
            risk = "必须"
        elif cat in ("trims",):
            risk = "中"
        elif cat in SMOOTH_CATS | LUT_CATS:
            risk = "中"
        else:
            risk = "低"
        v = (verify or {}).get(cat) or {}
        er = v.get("edge_ratio_vs_reference")
        ps = v.get("roundtrip_psnr_db")
        if er is not None and cat not in GUI_CATS:
            if er < 0.55:
                risk = "高"
                note += f"（实测边缘锐利度仅为参考包的 {er:.2f}，明显偏糊）"
            elif er > 1.8:
                risk = "中"
                note += f"（实测边缘锐利度为参考包的 {er:.2f}，偏硬/偏噪）"
        if ps is not None and ps < 28 and cat not in GUI_CATS:
            risk = "高" if risk != "必须" else risk
            note += f"（往返 PSNR 仅 {ps} dB，颜色失真明显）"
        if c.get("fail"):
            risk = "高"
            note += f"（有 {c['fail']} 个文件处理失败，请查看日志）"
        rows.append((cat, risk, note))
    return rows


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    t_start = time.perf_counter()
    here = Path(__file__).resolve().parent

    p = argparse.ArgumentParser(
        description="Minecraft 16x → 32x 资源包批量放大工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-i", "--input", required=True, help="源材质包 .zip（或已解压目录）")
    p.add_argument("-r", "--reference", default=None, help="参考风格包 .zip（可选，用于 pack_format 与质量对比）")
    p.add_argument("-o", "--output", default="output_32x.zip", help="输出 zip 路径")
    p.add_argument("-w", "--workdir", default=str(here / ".mc_upscale_work"), help="工作目录")
    p.add_argument("-m", "--model", default="auto",
                   help="像素画模型: auto|nearest|scale2x|lanczos|box|realesrgan|"
                        "realesrgan-x4plus|realesrgan-x2plus|realesrgan-anime-6b|realesrnet-x4plus")
    p.add_argument("-s", "--scale", type=int, default=2, help="放大倍率，默认 2")
    p.add_argument("-j", "--workers", type=int, default=0, help="并行进程数，0=自动")
    p.add_argument("-c", "--colors", type=int, default=256, help="调色板量化最大色数，0=关闭")
    p.add_argument("--quant-max-error", type=int, default=8,
                   help="量化误差保护阈值（0-255）。量化后可见像素最大 RGB 误差超过它"
                        "就放弃量化、保留原色，防止画作/火焰出色带。0=关闭保护")
    p.add_argument("--gui-model", default="nearest", help="gui/ 与 font/ 使用的模型")
    p.add_argument("--lut-model", default="lanczos", help="colormap/palettes/misc 使用的模型")
    p.add_argument("--despeckle", type=int, default=1, help="神经网络模型的去伪影次数")
    p.add_argument("--dither", action="store_true", help="量化时启用 Floyd-Steinberg 抖动")
    p.add_argument("--harden-alpha", action="store_true", help="alpha 通道二值化（硬边）")
    p.add_argument("--pack-format", default="auto", help="pack_format: 15 / 84-97 / auto")
    p.add_argument("--pack-desc", default="32x Resource Pack (upscaled from 16x)")
    p.add_argument("--tile", type=int, default=0, help="Real-ESRGAN 分块大小，0=不分块")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    p.add_argument("--fp32", action="store_true", help="强制 fp32 推理")
    p.add_argument("--weights", default=None, help="Real-ESRGAN 权重 .pth 路径")
    p.add_argument("--rrdb-num-block", type=int, default=None, help="覆盖 RRDB 块数")
    p.add_argument("--rrdb-num-feat", type=int, default=None, help="覆盖 RRDB 特征数")
    p.add_argument("--rrdb-scale", type=int, default=None, help="覆盖 RRDB 倍率")
    p.add_argument("--only", default=None, help="只处理匹配该正则的路径")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 张（调试用）")
    p.add_argument("--force", action="store_true", help="忽略断点，全部重算")
    p.add_argument("--zip-only", action="store_true", help="跳过处理，直接打包已有工作目录")
    p.add_argument("--verify", action="store_true", help="与参考包对比并输出质量报告")
    p.add_argument("--keep-temp", action="store_true", help="保留中间目录")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        LOG.error("输入不存在: %s", input_path)
        return 2
    workdir = Path(args.workdir).resolve()
    src_dir = workdir / "src"
    pack_dir = workdir / "pack"
    out_zip = Path(args.output).resolve()

    setup_logging(workdir / "process.log", args.verbose)
    LOG.info("=" * 72)
    LOG.info("工作目录: %s", workdir)

    # ---- pack_format 决策 ------------------------------------------------ #
    # 优先级：源包自带的 pack.mcmeta > --pack-format > 参考包 > 兜底 15
    # 源包自带 mcmeta 时文件会原样复制，这里只决定"需要新生成时写什么"。
    src_min = src_max = None
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        src_min, src_max = detect_pack_format(input_path)
        if src_min is not None:
            LOG.info("源包 pack.mcmeta: pack_format=%s（将原样保留）",
                     src_min if src_max is None or src_max == src_min
                     else f"{src_min}..{src_max}")
    ref_min = ref_max = None
    if args.reference:
        ref_path = Path(args.reference).resolve()
        if ref_path.exists():
            ref_min, ref_max = detect_pack_format(ref_path)
            if ref_min:
                LOG.info("参考包 pack_format: %s..%s", ref_min, ref_max)
    fmt_min, fmt_max = parse_pack_format(args.pack_format)
    if fmt_min == -1:  # auto
        if src_min is not None:
            # 源包已声明兼容范围，放大不改变内容兼容性，沿用即可
            fmt_min, fmt_max = src_min, src_max
        elif ref_min:
            fmt_min, fmt_max = ref_min, ref_max
            LOG.warning(
                "pack_format 采用参考包的范围 %s..%s。"
                "注意：请确认源包内容与参考包的目标版本一致。",
                fmt_min, fmt_max)
        else:
            fmt_min, fmt_max = 15, None
            LOG.warning("未检测到 pack.mcmeta，pack_format 采用 15（对应 1.20-1.20.1）。")
    if fmt_min is None:
        fmt_min, fmt_max = 15, None

    # ---- 模型决策 -------------------------------------------------------- #
    try:
        import torch  # noqa: F401
        has_torch = True
    except Exception:
        has_torch = False
    model = resolve_default_model(args.model, has_torch)
    if model in NEURAL_MODELS and not has_torch:
        LOG.error("模型 %s 需要 PyTorch，但未安装。请执行: pip install torch", model)
        return 2
    if args.model == "auto":
        LOG.info("像素画模型: scale2x（默认）")
        if has_torch:
            LOG.info("提示: 已检测到 torch。实测 Real-ESRGAN 对像素画会抹糊并产生条带，"
                     "故默认仍用 Scale2x；如源包含照片质感贴图，可对那一类单独用 "
                     "--model realesrgan-x4plus --only <正则>")
    else:
        LOG.info("像素画模型: %s", model)

    profiles = default_profiles(
        model, args.colors, args.despeckle, args.gui_model, args.lut_model,
        args.dither, args.harden_alpha, args.quant_max_error)

    # ---- --zip-only：只把已有工作目录重新打包 ---------------------------- #
    if args.zip_only:
        if not pack_dir.is_dir():
            LOG.error("--zip-only 需要已存在的工作目录：%s", pack_dir)
            return 2
        if not (pack_dir / "pack.mcmeta").exists():
            write_pack_mcmeta(pack_dir, args.pack_desc, fmt_min, fmt_max)
        n_files, raw_bytes = build_zip(pack_dir, out_zip)
        LOG.info("打包完成：%s （%d 个文件，原始 %s，压缩后 %s）",
                 out_zip, n_files, human(raw_bytes), human(out_zip.stat().st_size))
        return 0

    # ---- 阶段一：解压 ---------------------------------------------------- #
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        signature = zip_signature(input_path)
        manifest = stage_source(input_path, src_dir, signature)
    else:
        src_dir = input_path
        manifest = {}
        for f in sorted(src_dir.rglob("*")):
            if f.is_file() and f.name != ".manifest.json":
                rel = f.relative_to(src_dir).as_posix()
                manifest[rel] = sha256_file(f)

    # ---- 阶段二：处理 ---------------------------------------------------- #
    params_hash = hashlib.sha256(json.dumps({
        "model": model, "scale": args.scale, "colors": args.colors,
        "gui_model": args.gui_model, "lut_model": args.lut_model,
        "despeckle": args.despeckle, "dither": args.dither,
        "harden_alpha": args.harden_alpha, "tile": args.tile,
        "quant_max_error": args.quant_max_error,
        "profiles": {k: (v.model, v.quantize, v.dither, v.despeckle, v.harden_alpha,
                         v.max_error)
                     for k, v in profiles.items()},
    }, sort_keys=True).encode()).hexdigest()[:16]

    state = State(workdir / "state.json", params_hash)
    if args.force:
        state.data["files"] = {}

    jobs: List[Job] = []
    skipped: List[str] = []
    other_stats = {"pixel": 0, "smooth": 0}
    for rel, sha in sorted(manifest.items()):
        if not rel.lower().endswith(".png"):
            continue
        if args.only and not re.search(args.only, rel):
            continue
        cat, prof = classify(rel)
        try:
            with Image.open(src_dir / rel) as im:
                w, h = im.size
        except Exception as exc:
            LOG.warning("无法读取 %s: %s", rel, exc)
            continue
        # 无法可靠按路径判定的贴图，按内容决定用像素画还是平滑渐变处理：
        #   - Respckopts 的 extend_helper/*.png（other）
        #   - 直接放在 textures/ 下的 HUD 图集如 health.png / absorption.png（textures_root）
        if prof in ("other", "pixel") and cat in ("other", "textures_root"):
            try:
                with Image.open(src_dir / rel) as im:
                    arr = np.asarray(to_rgba(im), dtype=np.uint8)
                if looks_like_pixel_art(arr):
                    prof = "pixel"
                    other_stats["pixel"] += 1
                else:
                    prof = "smooth"
                    other_stats["smooth"] += 1
            except Exception:
                prof = "pixel"
                other_stats["pixel"] += 1
        jobs.append(Job(rel=rel, category=cat, profile=prof, src_sha=sha,
                        width=w, height=h))
    if other_stats["pixel"] or other_stats["smooth"]:
        LOG.info("按内容判定的贴图: %d 张按像素画处理, %d 张按平滑渐变处理",
                 other_stats["pixel"], other_stats["smooth"])

    # 复制所有非 PNG 文件（json/mcmeta/ogg/txt...）
    copy_jobs: List[str] = []
    for rel, sha in sorted(manifest.items()):
        if rel.lower().endswith(".png"):
            continue
        copy_jobs.append(rel)

    LOG.info("待处理 PNG: %d 张；待复制非 PNG: %d 个", len(jobs), len(copy_jobs))

    todo: List[Job] = []
    for j in jobs:
        if state.is_done(j.rel, j.src_sha) and (pack_dir / j.rel).is_file():
            skipped.append(j.rel)
            continue
        todo.append(j)
    if args.limit:
        todo = todo[: args.limit]
    LOG.info("断点续跑：已完成 %d 张，本次处理 %d 张", len(skipped), len(todo))

    cfg = {
        "profiles": profiles, "scale": args.scale, "weights": args.weights,
        "device": args.device, "tile": args.tile, "fp32": args.fp32,
        "rrdb": {"num_block": args.rrdb_num_block, "num_feat": args.rrdb_num_feat,
                 "scale": args.rrdb_scale},
    }

    t0 = time.perf_counter()
    ok = fail = 0
    by_cat: Dict[str, Dict[str, Any]] = {}
    failures: List[Tuple[str, str]] = []
    results: List[Result] = []
    sha_by_rel = {j.rel: j.src_sha for j in todo}

    def bucket(res: Result) -> Dict[str, Any]:
        return by_cat.setdefault(res.category or "?", {
            "total": 0, "ok": 0, "fail": 0, "skip": 0, "profile": res.profile,
            "ms": [], "sizes": set(), "out_sizes": set(),
        })

    if todo:
        workers = args.workers or min(4, max(1, (os.cpu_count() or 2)))
        workers = max(1, min(workers, len(todo)))
        LOG.info("启动 %d 个处理进程", workers)
        payload = [(j, cfg, str(src_dir), str(pack_dir)) for j in todo]
        if workers == 1:
            _worker_init(cfg)
            it = (_worker_entry(x) for x in payload)
            for res in it:
                results.append(res)
        else:
            ctx = mp.get_context("spawn")
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                     initializer=_worker_init, initargs=(cfg,)) as ex:
                for res in ex.map(_worker_entry, payload, chunksize=max(1, len(payload) // (workers * 8))):
                    results.append(res)
        for res in results:
            if res.status == "ok":
                ok += 1
                state.mark(res.rel, sha_by_rel.get(res.rel, ""), res)
            else:
                fail += 1
                failures.append((res.rel, res.detail))
                LOG.error("处理失败 %s: %s", res.rel, res.detail)
                state.mark(res.rel, sha_by_rel.get(res.rel, ""), res)
            if (ok + fail) % 200 == 0:
                state.save()
        state.save()

    # ---- 统计：从「本次结果 + 状态文件」重建，保证全量跳过的续跑也有完整报告 ---- #
    res_by_rel = {r.rel: r for r in results}
    for j in jobs:
        rec = state.data["files"].get(j.rel) or {}
        res = res_by_rel.get(j.rel)
        if res is not None and res.status == "fail":
            b = bucket(res)
            b["total"] += 1
            b["fail"] += 1
            b["ms"].append(res.ms)
            continue
        if rec.get("status") == "ok":
            b = by_cat.setdefault(j.category, {
                "total": 0, "ok": 0, "fail": 0, "skip": 0, "profile": j.profile,
                "ms": [], "sizes": set(), "out_sizes": set(),
            })
            b["total"] += 1
            b["ok"] += 1
            b["ms"].append(rec.get("ms", 0.0) or 0.0)
            if rec.get("in_size"):
                b["sizes"].add(rec["in_size"])
            if rec.get("out_size"):
                b["out_sizes"].add(rec["out_size"])
        else:
            b = by_cat.setdefault(j.category, {
                "total": 0, "ok": 0, "fail": 0, "skip": 0, "profile": j.profile,
                "ms": [], "sizes": set(), "out_sizes": set(),
            })
            b["total"] += 1  # 尚未完成（例如被 --limit 截断）
    # 历史上失败的记录也要出现在报告里
    for rel, rec in state.data["files"].items():
        if rec.get("status") == "fail" and rel not in {r for r, _ in failures}:
            failures.append((rel, rec.get("detail", "")))

    # 复制非 PNG
    copied = 0
    for rel in copy_jobs:
        dst = pack_dir / rel
        s = src_dir / rel
        if not s.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(s, dst)
        copied += 1

    # 复制被跳过的 PNG（断点续跑时它们已在 pack_dir 里，这里兜底）
    for rel in skipped:
        s, d = src_dir / rel, pack_dir / rel
        if s.is_file() and not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)

    LOG.info("处理完成：成功 %d，失败 %d，跳过 %d，复制 %d，耗时 %.1fs",
             ok, fail, len(skipped), copied, time.perf_counter() - t0)

    # ---- 阶段三：pack.mcmeta + 打包 -------------------------------------- #
    if not (pack_dir / "pack.mcmeta").exists():
        write_pack_mcmeta(pack_dir, args.pack_desc, fmt_min, fmt_max)
        LOG.info("生成 pack.mcmeta: pack_format=%s", fmt_min if fmt_max is None else f"{fmt_min}..{fmt_max}")

    verify_summary = None
    if args.verify and args.reference and not args.zip_only:
        LOG.info("与参考包对比评估中 ...")
        verify_summary = verify_against_reference(
            pack_dir, src_dir, Path(args.reference).resolve(), args.scale)
        LOG.info("对比完成：%d 个类别", len(verify_summary))

    n_files, raw_bytes = build_zip(pack_dir, out_zip)
    LOG.info("打包完成：%s （%d 个文件，原始 %s，压缩后 %s）",
             out_zip, n_files, human(raw_bytes), human(out_zip.stat().st_size))

    # ---- 报告 ------------------------------------------------------------ #
    for b in by_cat.values():
        b["avg_ms"] = (sum(b["ms"]) / len(b["ms"])) if b["ms"] else 0.0
        b["sizes"] = ", ".join(sorted(b["sizes"])[:4])
        b["out_sizes"] = ", ".join(sorted(b["out_sizes"])[:4])
    stats = {
        "input": str(input_path), "output": str(out_zip),
        "model": model, "scale": args.scale, "colors": args.colors,
        "workers": args.workers or "auto",
        "elapsed": f"{time.perf_counter() - t_start:.1f}s",
        "total": len(jobs), "ok": ok, "fail": fail, "skip": len(skipped),
        "copy": copied, "png_total": len(jobs),
        "by_category": by_cat,
        "failures": failures,
        "manual_redraw": manual_redraw_table(by_cat, verify_summary),
        "pack_format": [fmt_min, fmt_max],
    }
    write_report(workdir / "report.json", workdir / "REPORT.md", stats, verify_summary)
    LOG.info("报告已写入 %s 和 REPORT.md", workdir / "report.json")

    if not args.keep_temp:
        # 源暂存目录可以从 zip 秒级重建，删掉省磁盘；
        # pack/ 保留，这样 --zip-only / --verify / 断点续跑都能直接用。
        if src_dir.exists() and src_dir.parent == workdir:
            shutil.rmtree(src_dir, ignore_errors=True)
            LOG.info("已清理源暂存目录 %s（保留 %s 供续跑/重打包）", src_dir, pack_dir)

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
