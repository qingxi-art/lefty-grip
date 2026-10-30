# -*- coding: utf-8 -*-
"""
左撇子握笔辅助器 · 参数化生成工具（V2 · 实时预览版）
=====================================================

在原 V2 的基础上改的两件事（几何/参数/公式一律没动）：

  1. **参数一动，右边的图立刻重画**
     预览图不再是固定照片，而是按当前参数现画的正视图 + 俯视图（含尺寸标注），
     滑块只重跑「参数 + 预览」这一块（Streamlit fragment），不重跑整页。

  2. **CadQuery 变成可选依赖**
     以前 `import cadquery` 写在文件最上面，这台机器上它一崩，
     整个网页就直接报错白屏。现在改成用到时才导入：
     导入失败只是不能导 STL，页面和其它功能照常。

参数（改版：尺寸链从「笔杆直径」推出来）：
  pen_dia   笔杆直径   8~14  → 内孔 = 笔杆 + 0.3 配合间隙
  grip_dia  握持直径  16~26  → 外径；壁厚 =（外径 − 内孔）/ 2，自动算
  slot_depth 卡槽深度 0~3 ｜ finger_len 40~90 ｜ palm_len 140~230 ｜ palm_width 60~110

运行方式（必须用 streamlit 启动，不能直接 python 运行）：
    streamlit run grip_app_v2.py
    或运行同目录的 run_v2.py（会自动挑端口并打开浏览器）
"""

from __future__ import annotations

import inspect
import math
import os
import struct
import tempfile
import time

import numpy as np
import streamlit as st

# ================================================================
# 模块 0: 常量与默认值
# ----------------------------------------------------------------
# 尺寸链：笔杆直径 → 内孔（+0.3 配合间隙）→ 壁厚（握持直径 − 内孔）/ 2
# ================================================================
CLEARANCE = 0.3           # 笔杆与内孔的配合间隙 (mm)，装取顺畅
MIN_WALL = 1.2            # FDM 最小壁厚（0.4 mm 喷嘴 × 3 圈）
SLOT_TAPER = 30           # 指槽底部斜角 (度)
N_BUMPS = 16              # 兜底用，实际由手掌宽度推导

RANGE_PEN_DIA = (8.0, 14.0, 0.5, 10.0)
RANGE_GRIP_DIA = (14.0, 26.0, 0.5, 17.0)
RANGE_SLOT_DEPTH = (0.0, 3.0, 0.25, 1.5)
RANGE_FINGER_LEN = (40.0, 90.0, 1.0, 60.0)
RANGE_PALM_LEN = (140.0, 230.0, 1.0, 180.0)
RANGE_PALM_WIDTH = (60.0, 110.0, 1.0, 85.0)

PARAM_KEYS = ("pen_dia", "grip_dia", "slot_depth",
              "finger_len", "palm_len", "palm_width")
DEFAULTS = {
    "pen_dia": RANGE_PEN_DIA[3],
    "grip_dia": RANGE_GRIP_DIA[3],
    "slot_depth": RANGE_SLOT_DEPTH[3],
    "finger_len": RANGE_FINGER_LEN[3],
    "palm_len": RANGE_PALM_LEN[3],
    "palm_width": RANGE_PALM_WIDTH[3],
}

IMAGE_FILE = "grip_demo.png"   # 参考实拍照（可选，缺了也不报错）

USAGE_TEXT = """
**怎么用**

1. 先量两样：**笔杆直径**（卡尺或卷尺绕一圈读周长 ÷3.14）、**握持直径**（想握多粗，成人 16~18 mm 舒服）；
2. 左边拖滑块，右边图立刻跟着变，壁厚是自动算出来的；
3. 看图确认斜楔、卡槽的位置和比例是否合适；
4. 满意后点「生成 / 更新 STL」，下载模型去切片打印。

**尺寸链**

笔杆直径 → 内孔 = 笔杆 + 0.3 mm → 壁厚 =（握持直径 − 内孔）÷ 2。
握持直径给得太小、壁厚不足 1.2 mm 时，页面会提示并自动放大外径。
"""


# ================================================================
# 模块 1: 版本兼容小工具
# ================================================================
def fit(fn) -> dict:
    """返回当前 Streamlit 版本下「撑满容器」该用的关键字参数。"""
    try:
        if "width" in inspect.signature(fn).parameters:
            return {"width": "stretch"}
    except (TypeError, ValueError):
        pass
    return {"use_container_width": True}


_FRAGMENT = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)


def fragment(fn):
    """老版本 Streamlit 没有 fragment 时，退化成普通函数（整页重跑）。"""
    return _FRAGMENT(fn) if _FRAGMENT else fn


# ================================================================
# 模块 2: 参数化示意图渲染（纯 Pillow，不依赖 CadQuery）
# ================================================================
import io
import os
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFont

# ----------------------------------------------------------------- 视觉常量
BASE_W, BASE_H = 1240, 780
SS = 2                      # 超采样倍率，画完再缩小，线条更顺滑

BG = (252, 252, 250)
INK = (38, 52, 45)
INK2 = (104, 118, 108)
LINE = (178, 190, 180)
FILL = (233, 239, 233)
ACCENT = (176, 74, 52)
GRID = (226, 232, 226)

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)
_BOLD_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
)


def _pick_font(candidates) -> str | None:
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


_FONT_PATH = _pick_font(_FONT_CANDIDATES)
_BOLD_PATH = _pick_font(_BOLD_CANDIDATES) or _FONT_PATH


def _font(size: int, bold: bool = False):
    path = _BOLD_PATH if bold else _FONT_PATH
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:                       # Pillow < 10.1
        return ImageFont.load_default()


def _fmt(value: float) -> str:
    """18.0 -> '18'；18.5 -> '18.5'"""
    return f"{value:.1f}".rstrip("0").rstrip(".")


# ----------------------------------------------------------------- 画布封装
class _Canvas:
    """所有坐标都按 BASE 尺寸给，内部自动乘超采样倍率。"""

    def __init__(self):
        self.k = SS
        self.img = Image.new("RGB", (BASE_W * SS, BASE_H * SS), BG)
        self.d = ImageDraw.Draw(self.img)

    # -- 基础图元 --------------------------------------------------
    def _p(self, x, y):
        return (x * self.k, y * self.k)

    def _pts(self, pts):
        return [(x * self.k, y * self.k) for x, y in pts]

    def line(self, pts, fill=INK, width=2, dash=None):
        if dash:
            self._dashed(pts, fill, width, dash)
            return
        self.d.line(self._pts(pts), fill=fill, width=max(1, int(width * self.k)),
                    joint="curve")

    def _dashed(self, pts, fill, width, dash):
        seg, gap = dash
        for (x1, y1), (x2, y2) in zip(pts[:-1], pts[1:]):
            length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if length == 0:
                continue
            ux, uy = (x2 - x1) / length, (y2 - y1) / length
            pos = 0.0
            while pos < length:
                end = min(pos + seg, length)
                self.line([(x1 + ux * pos, y1 + uy * pos),
                           (x1 + ux * end, y1 + uy * end)], fill, width)
                pos = end + gap

    def dashdot(self, pts, fill=INK2, width=1.4):
        """中心线：长划 + 点"""
        for (x1, y1), (x2, y2) in zip(pts[:-1], pts[1:]):
            length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if length == 0:
                continue
            ux, uy = (x2 - x1) / length, (y2 - y1) / length
            pos = 0.0
            pattern = [(26, True), (7, False), (3, True), (7, False)]
            i = 0
            while pos < length:
                seg, on = pattern[i % len(pattern)]
                end = min(pos + seg, length)
                if on:
                    self.line([(x1 + ux * pos, y1 + uy * pos),
                               (x1 + ux * end, y1 + uy * end)], fill, width)
                pos = end
                i += 1

    def rect(self, x0, y0, x1, y1, fill=None, outline=INK, width=2):
        self.d.rectangle(
            [self._p(x0, y0), self._p(x1, y1)],
            fill=fill,
            outline=outline,
            width=max(1, int(width * self.k)) if outline else 0,
        )

    def circle(self, cx, cy, r, fill=None, outline=INK, width=2):
        self.d.ellipse(
            [self._p(cx - r, cy - r), self._p(cx + r, cy + r)],
            fill=fill,
            outline=outline,
            width=max(1, int(width * self.k)) if outline else 0,
        )

    def poly(self, pts, fill=None, outline=INK, width=2):
        self.d.polygon(self._pts(pts), fill=fill, outline=outline,
                       width=max(1, int(width * self.k)) if outline else 0)

    def text(self, x, y, s, size=13, fill=INK, bold=False, anchor="la"):
        self.d.text(self._p(x, y), s, font=_font(int(size * self.k), bold),
                    fill=fill, anchor=anchor)

    # -- 尺寸标注 --------------------------------------------------
    def _arrow(self, x, y, ch, color=ACCENT, size=9.0):
        """在 (x,y) 处画一个箭头，ch 为指向：'l' 'r' 'u' 'd'"""
        s = size
        if ch == "r":
            pts = [(x, y), (x - s, y - s * 0.34), (x - s, y + s * 0.34)]
        elif ch == "l":
            pts = [(x, y), (x + s, y - s * 0.34), (x + s, y + s * 0.34)]
        elif ch == "u":
            pts = [(x, y), (x - s * 0.34, y + s), (x + s * 0.34, y + s)]
        else:
            pts = [(x, y), (x - s * 0.34, y - s), (x + s * 0.34, y - s)]
        self.d.polygon(self._pts(pts), fill=color)

    def dim_h(self, x1, x2, y, label, tick=10, color=ACCENT, size=12):
        self.line([(x1, y - tick), (x1, y + tick)], color, 1.2)
        self.line([(x2, y - tick), (x2, y + tick)], color, 1.2)
        self.line([(x1, y), (x2, y)], color, 1.2)
        self._arrow(x1, y, "l", color)
        self._arrow(x2, y, "r", color)
        self.text((x1 + x2) / 2, y - 7, label, size=size, fill=color, anchor="mb")

    def dim_v(self, y1, y2, x, label, tick=10, color=ACCENT, size=12, side="l"):
        self.line([(x - tick, y1), (x + tick, y1)], color, 1.2)
        self.line([(x - tick, y2), (x + tick, y2)], color, 1.2)
        self.line([(x, y1), (x, y2)], color, 1.2)
        self._arrow(x, y1, "u", color)
        self._arrow(x, y2, "d", color)
        if side == "l":
            self.text(x - 7, (y1 + y2) / 2, label, size=size, fill=color, anchor="rm")
        else:
            self.text(x + 7, (y1 + y2) / 2, label, size=size, fill=color, anchor="lm")

    def leader(self, x, y, dx, dy, label, color=ACCENT, size=12):
        """引线标注：从 (x,y) 拉到 (x+dx, y+dy) 再写文字"""
        self.line([(x, y), (x + dx, y + dy)], color, 1.2)
        self.d.ellipse([self._p(x - 2.2, y - 2.2), self._p(x + 2.2, y + 2.2)],
                       fill=color)
        tx, ty = x + dx, y + dy
        anchor = "lm" if dx >= 0 else "rm"
        self.text(tx + (6 if dx >= 0 else -6), ty, label, size=size,
                  fill=color, anchor=anchor)


# ----------------------------------------------------------------- 主渲染
def render_preview(params: Dict[str, float], derived: Dict[str, float]) -> Image.Image:
    """把参数画成示意图，返回 PIL.Image。

    params  需包含 slot_depth / finger_len / palm_len / palm_width（可选 pen_dia）
    derived 需包含 bore / outer_d / wall / grip_height / slot_width /
            wedge_height / n_bumps
    """
    slot_depth = float(params["slot_depth"])
    finger_len = float(params["finger_len"])
    palm_len = float(params["palm_len"])
    palm_width = float(params["palm_width"])
    pen_dia = float(params.get("pen_dia", derived.get("pen_dia", 10.0)))

    grip_h = float(derived["grip_height"])
    slot_w = float(derived["slot_width"])
    wedge_h = float(derived["wedge_height"])
    n_bumps = int(derived["n_bumps"])

    outer_d = float(derived["outer_d"])
    pen_id = float(derived["bore"])
    wall = float(derived.get("wall", (outer_d - pen_id) / 2))

    c = _Canvas()

    # ---------------- 抬头 ----------------
    c.text(52, 34, "左撇子握笔辅助器 · 参数预览", size=25, bold=True, fill=INK)
    c.text(BASE_W - 52, 44,
           f"笔杆 Ø{_fmt(pen_dia)} → 内孔 Ø{_fmt(pen_id)} · 外径 Ø{_fmt(outer_d)} · "
           f"壁厚 {_fmt(wall)} · 总高 {_fmt(grip_h)}",
           size=12, fill=INK2, anchor="ra")
    c.line([(52, 78), (BASE_W - 52, 78)], LINE, 1)

    # ---------------- 比例：两个视图共用一个比例尺 ----------------
    scale = min(300.0 / outer_d, 380.0 / grip_h)

    fx, fy = 330, 356                      # 正视图中心
    tx, ty = 930, 356                      # 俯视图中心
    label_y = 672                          # 两个视图标题同一基线

    w = outer_d * scale
    h = grip_h * scale
    x0, x1 = fx - w / 2, fx + w / 2
    y0, y1 = fy - h / 2, fy + h / 2

    # ================= 正视图 =================
    c.rect(x0, y0, x1, y1, fill=FILL, outline=INK, width=2.4)

    # 内孔（虚线，表示被挡住）
    r_bore = pen_id / 2 * scale
    c.line([(fx - r_bore, y0 + 3), (fx - r_bore, y1 - 3)], INK2, 1.3, dash=(7, 5))
    c.line([(fx + r_bore, y0 + 3), (fx + r_bore, y1 - 3)], INK2, 1.3, dash=(7, 5))

    # 顶部斜楔（给左手食指的搭放斜面）
    chamfer = max(12.0, min(wedge_h * scale, w * 0.42))
    c.poly([(x0, y0), (x0 + chamfer, y0), (x0, y0 + chamfer)],
           fill=BG, outline=INK, width=2.4)

    # 侧面卡槽（U 形开口，只画三条边）
    hw = min(slot_w * scale / 2, h * 0.42)
    dp = max(4.0, slot_depth * scale) if slot_depth > 0.01 else 0.0
    if dp:
        c.poly([(x1 - dp, fy - hw), (x1 + 3, fy - hw),
                (x1 + 3, fy + hw), (x1 - dp, fy + hw)], fill=BG, outline=None)
        c.line([(x1 - dp, fy - hw), (x1 + 1, fy - hw)], INK, 2.4)
        c.line([(x1 - dp, fy + hw), (x1 + 1, fy + hw)], INK, 2.4)
        c.line([(x1 - dp, fy - hw), (x1 - dp, fy + hw)], INK, 2.4)

    # 中心线
    c.dashdot([(x0 - 26, fy), (x1 + 26, fy)])

    # 标注
    c.dim_h(x0, x1, y1 + 40, f"外径 Ø{_fmt(outer_d)}")
    c.dim_v(y0, y1, x0 - 46, f"{_fmt(grip_h)}")
    c.leader(x0 + chamfer * 0.5, y0 + chamfer * 0.5, 64, (y0 - 30) - (y0 + chamfer * 0.5),
             "食指斜面", size=11)

    c.text(fx, label_y, "正视图", size=14, bold=True, fill=INK2, anchor="ma")

    # ================= 俯视图 =================
    R = outer_d / 2 * scale
    r = pen_id / 2 * scale

    c.circle(tx, ty, R, fill=FILL, outline=INK, width=2.4)
    c.circle(tx, ty, r, fill=BG, outline=INK, width=2.4)

    # 卡槽开口（从圆周切进去，只画开口的三条边）
    if dp:
        hw2 = min(slot_w * scale / 2, R * 0.9)
        edge = (R ** 2 - hw2 ** 2) ** 0.5
        c.poly([(tx + R - dp, ty - hw2), (tx + R + 40, ty - hw2),
                (tx + R + 40, ty + hw2), (tx + R - dp, ty + hw2)],
               fill=BG, outline=None)
        c.line([(tx + R - dp, ty - hw2), (tx + edge, ty - hw2)], INK, 2.2)
        c.line([(tx + R - dp, ty + hw2), (tx + edge, ty + hw2)], INK, 2.2)
        c.line([(tx + R - dp, ty - hw2), (tx + R - dp, ty + hw2)], INK, 2.2)

    # 防滑圆坑：沿外缘均布
    import math
    for i in range(n_bumps):
        ang = 2 * math.pi * i / n_bumps
        rr = R - max(3.0, 1.35 * scale)
        cx, cy = tx + rr * math.cos(ang), ty + rr * math.sin(ang)
        c.circle(cx, cy, max(2.0, 1.1 * scale), fill=BG, outline=INK2, width=1.1)

    c.dashdot([(tx - R - 24, ty), (tx + R + 24, ty)])
    c.dashdot([(tx, ty - R - 24), (tx, ty + R + 24)])

    c.dim_h(tx - R, tx + R, ty + R + 40, f"外径 Ø{_fmt(outer_d)}")
    c.leader(tx - r * 0.707, ty - r * 0.707, -54, -44, f"内径 Ø{_fmt(pen_id)}", size=11)
    if dp:
        hw2 = min(slot_w * scale / 2, R * 0.9)
        c.dim_v(ty - hw2, ty + hw2, tx + R + 34, f"槽宽 {_fmt(slot_w)}",
                tick=7, size=11, side="r")
        c.leader(tx + R - dp, ty + hw2 * 0.5, 44, 56, f"槽深 {_fmt(slot_depth)}", size=11)

    c.text(tx, label_y, "俯视图", size=14, bold=True, fill=INK2, anchor="ma")

    # ---------------- 页脚 ----------------
    c.line([(52, BASE_H - 58), (BASE_W - 52, BASE_H - 58)], GRID, 1)
    c.text(52, BASE_H - 44,
           f"按当前参数等比缩放 · 单位 mm · 手部 {_fmt(finger_len)} / {_fmt(palm_len)} / "
           f"{_fmt(palm_width)}（指长 / 掌长 / 掌宽）",
           size=11, fill=INK2, anchor="la")
    c.text(BASE_W - 52, BASE_H - 44, "左撇子 · 参数化生成工具",
           size=11, fill=INK2, anchor="ra")

    return c.img.resize((BASE_W, BASE_H), Image.LANCZOS)


def preview_png_bytes(params: Dict[str, float], derived: Dict[str, float]) -> bytes:
    """渲染并编码为 PNG 字节流（可直接 st.image / 下载）。"""
    buf = io.BytesIO()
    render_preview(params, derived).save(buf, format="PNG", optimize=True)
    return buf.getvalue()

# ================================================================
# 模块 3: CadQuery（惰性导入 + 坏依赖自动顶替）
# ================================================================
_CQ = None
_CQ_ERROR = ""
_CQ_NOTE = ""

# cadquery 有两个「用不上但一 import 就炸」的依赖：
#   ezdxf  → 只服务于 DXF 导入
#   casadi → 只服务于装配体约束求解
# 本工具只做实体建模 + 导出 STL，两者都用不到。
# 有的电脑上它们的 .pyd 会被系统策略拦（DLL load failed / 应用程序控制策略已阻止），
# 这时用空壳模块顶掉，CAD 内核 OCP 本身是好的，建模和导出照常。
_STUB_DEPS = (
    "ezdxf", "ezdxf.entities", "ezdxf.layouts", "ezdxf.math",
    "ezdxf.path", "ezdxf.recover", "ezdxf.document",
    "casadi", "casadi.casadi",
)


def _stub_broken_deps() -> list:
    """把 import 失败的依赖换成空壳，返回被顶替的包名列表。"""
    import sys
    from unittest.mock import MagicMock

    replaced = []
    for name in _STUB_DEPS:
        try:
            __import__(name)
            continue                      # 能正常导入，不动它
        except Exception:
            pass
        # 清掉可能半加载的残骸，再塞一个万能空壳
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]
        sys.modules[name] = MagicMock(name=name)
        top = name.split(".")[0]
        if top not in replaced:
            replaced.append(top)
    return replaced


def get_cq():
    """第一次真正要建模时才 import cadquery。

    正常情况下直接导入；如果因为 ezdxf / casadi 崩掉，就先顶掉它们再试一次。
    实在不行才记错误信息 —— 页面本身永远不会因为这里失败而打不开。
    """
    global _CQ, _CQ_ERROR, _CQ_NOTE
    if _CQ is not None:
        return _CQ
    if _CQ_ERROR:
        return None
    try:
        import cadquery
        _CQ = cadquery
        return _CQ
    except Exception as first_err:
        try:
            replaced = _stub_broken_deps()
            import cadquery
            _CQ = cadquery
            if replaced:
                _CQ_NOTE = ("已自动跳过加载失败的可选依赖 "
                            + " / ".join(replaced) + "（本工具用不到）")
            return _CQ
        except Exception as second_err:
            _CQ_ERROR = (f"首次导入：{type(first_err).__name__}: {first_err}；"
                         f"兜底后仍失败：{type(second_err).__name__}: {second_err}")
            _CQ = None
            return None


def cq_status() -> tuple[bool, str]:
    cq = get_cq()
    return (cq is not None, _CQ_ERROR if cq is None else _CQ_NOTE)


def derive_dimensions(pen_dia: float, grip_dia: float,
                      finger_len: float, palm_len: float, palm_width: float) -> dict:
    """尺寸链：笔杆直径 → 内孔 → 壁厚；手部尺寸 → 高度/槽宽/斜楔/圆坑。

    内孔 = 笔杆直径 + 配合间隙(0.3)
    外径 = 握持直径（若壁厚不足 MIN_WALL，则把外径放大到刚够）
    壁厚 = (外径 − 内孔) / 2
    """
    bore = pen_dia + CLEARANCE
    outer = max(grip_dia, bore + 2 * MIN_WALL)
    wall = (outer - bore) / 2
    return {
        "bore": bore,
        "outer_d": outer,
        "wall": wall,
        "wall_ok": grip_dia >= bore + 2 * MIN_WALL - 1e-9,
        "grip_height": max(24.0, palm_len * 0.18),
        "slot_width": min(14.0, max(6.0, finger_len * 0.16)),
        "wedge_height": max(6.0, finger_len * 0.13),
        "n_bumps": int(min(20, max(12, palm_width / 5))),
    }


def build_grip(pen_dia, grip_dia, slot_depth, finger_len, palm_len, palm_width):
    """生成左撇子握笔辅助器 3D 模型。"""
    cq = get_cq()
    if cq is None:
        raise RuntimeError(_CQ_ERROR or "CadQuery 不可用")

    dim = derive_dimensions(pen_dia, grip_dia, finger_len, palm_len, palm_width)
    grip_height = dim["grip_height"]
    slot_width = dim["slot_width"]
    wedge_height = dim["wedge_height"]
    n_bumps = dim["n_bumps"]

    outer_d = dim["outer_d"]
    pen_id = dim["bore"]

    model = (
        cq.Workplane("XY")
        .circle(outer_d / 2)
        .circle(pen_id / 2)
        .extrude(grip_height)
    )

    wedge = (
        cq.Workplane("XY")
        .transformed(rotate=(0, 0, 0), offset=(0, 0, grip_height))
        .polyline([(-outer_d, 0), (-outer_d, -outer_d), (0, 0)])
        .close()
        .extrude(-wedge_height)
    )
    model = model.cut(wedge)

    if slot_depth > 0.01:
        box = (
            cq.Workplane("XY")
            .transformed(rotate=(SLOT_TAPER, 0, 0))
            .box(slot_width, grip_height + 10, (slot_depth + 5) * 2)
            .translate((outer_d / 2 - slot_depth / 2, 0, grip_height / 2))
        )
        model = model.cut(box)

    bump_r = outer_d / 2 + 0.4
    for i in range(n_bumps):
        ang = 2 * math.pi * i / n_bumps
        bx, by = bump_r * math.cos(ang), bump_r * math.sin(ang)
        model = model.cut(
            cq.Workplane("XY").sphere(1.2).translate((bx, by, grip_height / 2))
        )
    return model


def export_stl(model, tolerance: float = 0.1) -> bytes:
    cq = get_cq()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "grip.stl")
        cq.exporters.export(model, path, tolerance=tolerance, angularTolerance=0.3)
        with open(path, "rb") as f:
            return f.read()


# ================================================================
# 模块 3.5: 网格分析 —— 材料用量 / 打印时间 / 悬垂面积
# ================================================================
MATERIALS = {"PLA": 1.24, "PETG": 1.27, "TPU": 1.21, "ABS": 1.04}


def analyze_stl(data: bytes) -> dict:
    """解析二进制 STL：体积、表面积、悬垂面积（法向朝下且斜于 45°）。

    悬垂判据：三角面法向与竖直向下方向的夹角 < 45°，
    即 nz < -cos(45°) = -0.7071 —— 这些面自下而上打印时必须靠支撑或桥接。
    """
    n = struct.unpack("<I", data[80:84])[0]
    raw = np.frombuffer(data[84:84 + n * 50], dtype=np.uint8).reshape(n, 50)
    tri = raw[:, 12:48].copy().view("<f4").reshape(n, 3, 3)

    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    cr = np.cross(b - a, c - a)
    area = np.linalg.norm(cr, axis=1) / 2.0
    vol = float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0))
    nz = cr[:, 2] / (2.0 * area + 1e-12)
    over = float(area[nz < -0.7071].sum())
    total = float(area.sum()) + 1e-12

    lo = tri.reshape(-1, 3).min(axis=0)
    hi = tri.reshape(-1, 3).max(axis=0)
    return {
        "tris": int(n),
        "volume": vol / 1000.0,                 # cm³
        "area": total / 100.0,                  # cm²
        "overhang": over,                       # mm²
        "overhang_pct": over / total * 100.0,   # %
        "bbox": (hi - lo),                      # mm
    }


def estimate_print_minutes(volume_cm3: float, layer: float = 0.2,
                           width: float = 0.45, speed: float = 60.0,
                           eff: float = 0.7) -> float:
    """粗估打印时间：体积 / 有效挤出速率，再乘 1.35 的空走/回抽开销。"""
    flow = layer * width * speed * eff          # mm³/s
    return volume_cm3 * 1000.0 / flow * 1.35 / 60.0


def _stl_triangles(data: bytes):
    n = struct.unpack("<I", data[80:84])[0]
    raw = np.frombuffer(data[84:84 + n * 50], dtype=np.uint8).reshape(n, 50)
    return raw[:, 12:48].copy().view("<f4").reshape(n, 3, 3)


def render_mesh_png(data: bytes, W: int = 880, H: int = 620) -> bytes:
    """把 STL 画成等轴测效果图，悬垂面（>45°、且不在底面上）标红。"""
    import io as _io

    from PIL import Image as _Image, ImageDraw as _ImageDraw

    tri = _stl_triangles(data)
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    cr = np.cross(b - a, c - a)
    area = np.linalg.norm(cr, axis=1) / 2.0 + 1e-12
    nrm = cr / (2.0 * area[:, None])

    # 悬垂：法向朝下且陡于 45°；但底面（贴平台的那一面）不算悬垂
    on_bed = tri[:, :, 2].max(axis=1) < 0.5
    overhang = (nrm[:, 2] < -0.7071) & (~on_bed)

    # 相机：方位角 35°、仰角 20°，标准 look-at 投影
    az, el = math.radians(38.0), math.radians(-18.0)   # 从下往上看，才能看见悬垂面
    d = np.array([math.sin(az) * math.cos(el),
                  -math.cos(az) * math.cos(el),
                  math.sin(el)])
    right = np.cross(np.array([0.0, 0.0, 1.0]), d)
    right /= np.linalg.norm(right)
    up = np.cross(d, right)

    sx = tri @ right
    sy = tri @ up
    depth = (tri.mean(axis=1) @ d)
    n_dot = nrm @ d

    xy = np.stack([sx, sy], axis=-1)
    mn, mx = xy.reshape(-1, 2).min(axis=0), xy.reshape(-1, 2).max(axis=0)
    span = float((mx - mn).max()) + 1e-9
    scale = (min(W, H) - 90) / span
    xy = (xy - mn) * scale + 45
    xy[:, :, 1] = H - xy[:, :, 1]

    facing = n_dot > 0.02                       # 背面剔除，只画朝向相机的面
    light = right * 0.75 + up * 0.25 + d * 0.18
    light /= np.linalg.norm(light)
    shade = np.clip(nrm @ light, 0, 1) * 0.55 + 0.28

    img = _Image.new("RGB", (W, H), (252, 252, 250))
    d2 = _ImageDraw.Draw(img)
    for i in np.argsort(depth):
        if not facing[i]:
            continue
        g = int(70 + 170 * shade[i])
        fill = (206, 92, 68) if overhang[i] else (g, g + 5, g - 3)
        d2.polygon([tuple(xy[i, k]) for k in range(3)], fill=fill)

    buf = _io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ================================================================
# 模块 4: 缓存（预览与 STL 各自独立）
# ================================================================
@st.cache_data(show_spinner=False, max_entries=64)
def cached_preview(pen_dia, grip_dia, slot_depth, finger_len, palm_len, palm_width):
    params = dict(pen_dia=pen_dia, grip_dia=grip_dia, slot_depth=slot_depth,
                  finger_len=finger_len, palm_len=palm_len, palm_width=palm_width)
    derived = derive_dimensions(pen_dia, grip_dia, finger_len, palm_len, palm_width)
    return preview_png_bytes(params, derived), derived


@st.cache_data(show_spinner=False, max_entries=8)
def cached_stl(pen_dia, grip_dia, slot_depth, finger_len, palm_len, palm_width) -> bytes:
    model = build_grip(pen_dia, grip_dia, slot_depth, finger_len, palm_len, palm_width)
    return export_stl(model)


# ================================================================
# 模块 5: 状态
# ================================================================
PRESETS = {
    "儿童手": dict(pen_dia=9.0, grip_dia=15.0, slot_depth=1.2,
                   finger_len=45.0, palm_len=150.0, palm_width=70.0),
    "女生手": dict(pen_dia=10.0, grip_dia=16.5, slot_depth=1.5,
                   finger_len=55.0, palm_len=170.0, palm_width=78.0),
    "男生手": dict(pen_dia=10.5, grip_dia=18.0, slot_depth=1.5,
                   finger_len=65.0, palm_len=190.0, palm_width=88.0),
}


_RANGES = {
    "pen_dia": RANGE_PEN_DIA,
    "grip_dia": RANGE_GRIP_DIA,
    "slot_depth": RANGE_SLOT_DEPTH,
    "finger_len": RANGE_FINGER_LEN,
    "palm_len": RANGE_PALM_LEN,
    "palm_width": RANGE_PALM_WIDTH,
}


def _clamp_into_range(values: dict):
    """把要写进 session_state 的参数夹到滑块范围内，避免越界报错。"""
    for key, value in values.items():
        lo, hi = _RANGES[key][0], _RANGES[key][1]
        st.session_state[key] = max(lo, min(hi, float(value)))


def reset_params():
    _clamp_into_range(DEFAULTS)


def apply_preset(preset: dict):
    _clamp_into_range(preset)


def current_params() -> dict:
    return {k: float(st.session_state.get(k, DEFAULTS[k])) for k in PARAM_KEYS}


# ================================================================
# 模块 6: 界面 —— 参数 + 实时预览（独立刷新区）
# ================================================================
def slider_row(label, key, spec, help_text=""):
    lo, hi, step, _default = spec
    return st.slider(label, min_value=lo, max_value=hi, step=step,
                     key=key, help=help_text)


@fragment
def params_and_preview():
    """改任何滑块 → 只重跑这一块 → 右边的图立刻按新参数重画。"""
    left, right = st.columns([0.85, 1.15], gap="large")

    with left:
        st.markdown("##### 🖊️ 笔的尺寸 → 内外径")
        pcols = st.columns(len(PRESETS))
        for pcol, (pname, preset) in zip(pcols, PRESETS.items()):
            pcol.button(pname, on_click=apply_preset, args=(preset,),
                        **fit(st.button), help="一键套用这套手型尺寸")
        pen_dia = slider_row("笔杆直径 (mm)", "pen_dia", RANGE_PEN_DIA,
                             "卡尺量笔杆，或绕一圈读周长 ÷3.14；中性笔一般 9~11 mm")
        grip_dia = slider_row("握持直径 (mm)", "grip_dia", RANGE_GRIP_DIA,
                              "打印出来握在手里的外径，成人 16~18 mm 比较舒服")

        # 按手宽给握持直径一个建议值（经验式：约等于手宽的 1/5）
        palm_w_now = float(st.session_state.get("palm_width", RANGE_PALM_WIDTH[3]))
        grip_rec = max(RANGE_GRIP_DIA[0], min(22.0, round(palm_w_now * 0.20 * 2) / 2))
        rc1, rc2 = st.columns([1.5, 1])
        rc1.caption(f"按手宽 {palm_w_now:.0f} mm，建议 ≈ **{grip_rec:.1f} mm**（手宽 × 0.2）")
        rc2.button("用建议值", on_click=apply_preset,
                   args=({"grip_dia": grip_rec},), **fit(st.button))

        slot_depth = slider_row("卡槽深度 (mm)", "slot_depth", RANGE_SLOT_DEPTH,
                                "侧面指槽，0 表示不开槽；不要超过壁厚")

        st.markdown("##### ✋ 手的尺寸")
        finger_len = slider_row("手指长度 (mm)", "finger_len", RANGE_FINGER_LEN,
                                "中指指尖到指根，决定指槽宽度与斜楔深度")
        palm_len = slider_row("手掌长度 (mm)", "palm_len", RANGE_PALM_LEN,
                              "手腕到中指指根，决定握笔器总高度")
        palm_width = slider_row("手掌宽度 (mm)", "palm_width", RANGE_PALM_WIDTH,
                                "手掌最宽处，影响防滑圆坑密度")
        st.button("↺ 恢复默认参数", on_click=reset_params, **fit(st.button))

    with right:
        st.markdown("##### 🖼️ 实时预览")
        t0 = time.perf_counter()
        png, derived = cached_preview(pen_dia, grip_dia, slot_depth,
                                      finger_len, palm_len, palm_width)
        cost_ms = (time.perf_counter() - t0) * 1000

        st.image(png, **fit(st.image))
        st.caption(
            f"参数一变，这张图单独重绘 · 本次 {cost_ms:.0f} ms · "
            f"内孔 Ø{derived['bore']:.1f} / 外径 Ø{derived['outer_d']:.1f} / "
            f"H{derived['grip_height']:.1f} mm"
        )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("内孔", f"Ø{derived['bore']:.1f}")
        c2.metric("外径", f"Ø{derived['outer_d']:.1f}")
        c3.metric("壁厚", f"{derived['wall']:.1f}")
        c4.metric("总高", f"{derived['grip_height']:.1f}")

        if not derived["wall_ok"]:
            st.warning(
                f"握持直径 {grip_dia:g} 相对笔杆 {pen_dia:g} 太小，壁厚只剩 "
                f"{(grip_dia - derived['bore']) / 2:.2f} mm（低于 {MIN_WALL} mm 打不出来）。"
                f"已把外径放大到 Ø{derived['outer_d']:.1f} mm，想更细可以把笔杆直径调小。",
                icon="⚠️",
            )

        remain = derived["wall"] - slot_depth
        if remain < MIN_WALL and slot_depth > 0.01:
            st.warning(
                f"卡槽深度 {slot_depth:g} mm 几乎等于壁厚 {derived['wall']:.2f} mm，"
                f"槽底只剩 {max(remain, 0):.2f} mm，侧壁会被切穿。"
                f"建议槽深 ≤ {max(derived['wall'] - MIN_WALL, 0):.1f} mm。",
                icon="⚠️",
            )

        now = dict(pen_dia=pen_dia, grip_dia=grip_dia, slot_depth=slot_depth,
                   finger_len=finger_len, palm_len=palm_len, palm_width=palm_width)
        built = st.session_state.get("stl_params")
        if built and built != now:
            st.warning("预览已按新参数更新；下方 STL 还是旧模型，记得点「生成 / 更新 STL」。",
                       icon="⚠️")

        st.download_button(
            "⬇️ 下载预览图 PNG",
            data=png,
            file_name=(f"grip_preview_pen{pen_dia:g}_grip{grip_dia:g}"
                       f"_h{derived['grip_height']:.1f}.png"),
            mime="image/png",
            **fit(st.download_button),
        )

        sc1, sc2 = st.columns(2)
        if sc1.button("📌 存为方案", **fit(st.button), help="最多存 3 组，并排对比"):
            snaps = list(st.session_state.get("snaps") or [])
            snaps.append(dict(params=dict(now), derived=dict(derived), png=png))
            st.session_state["snaps"] = snaps[-3:]
        if sc2.button("🗑 清空方案", **fit(st.button)):
            st.session_state["snaps"] = []

    # 参数清单也放在这块里，保证和预览永远是同一套数值
    with st.expander("📋 参数清单 · 含尺寸链推导", expanded=False):
        t1, t2 = st.columns([1.05, 1], gap="large")
        with t1:
            params_table(now, derived, st.session_state.get("stl_bytes"))
        with t2:
            st.markdown(USAGE_TEXT)

    # 方案对比
    snaps = st.session_state.get("snaps") or []
    if snaps:
        st.markdown("##### 📌 已存方案对比")
        scols = st.columns(len(snaps))
        for i, (scol, snap) in enumerate(zip(scols, snaps), 1):
            with scol:
                st.image(snap["png"], **fit(st.image))
                d, p = snap["derived"], snap["params"]
                st.caption(
                    f"**方案 {i}** · 笔杆 {p['pen_dia']:g} → 内孔 {d['bore']:.1f} / "
                    f"外径 {d['outer_d']:.1f} / 壁厚 {d['wall']:.1f} / "
                    f"H{d['grip_height']:.1f}"
                )


def params_table(params: dict, derived: dict, stl_bytes):
    rows = [
        ("笔杆直径（输入）", f"{params['pen_dia']:.1f} mm"),
        ("握持直径（输入）", f"{params['grip_dia']:.1f} mm"),
        ("配合间隙", f"+{CLEARANCE:.1f} mm"),
        ("内孔直径（推导）", f"{derived['bore']:.1f} mm"),
        ("外径（推导）", f"{derived['outer_d']:.1f} mm"),
        ("壁厚（推导）", f"{derived['wall']:.2f} mm"
                        + ("" if derived["wall_ok"] else " ⚠ 已按最小壁厚放大")),
        ("卡槽深度", f"{params['slot_depth']:.2f} mm"),
        ("手指长度", f"{params['finger_len']:.0f} mm"),
        ("手掌长度", f"{params['palm_len']:.0f} mm"),
        ("手掌宽度", f"{params['palm_width']:.0f} mm"),
        ("总高度（自适应）", f"{derived['grip_height']:.1f} mm"),
        ("指槽宽度（自适应）", f"{derived['slot_width']:.1f} mm"),
        ("斜楔深度（自适应）", f"{derived['wedge_height']:.1f} mm"),
        ("防滑圆坑", f"{derived['n_bumps']} 个"),
        ("STL 大小", f"{len(stl_bytes) / 1024:.0f} KB" if stl_bytes else "未生成"),
    ]
    st.markdown("| 参数 | 数值 |\n|---|---|\n" +
                "\n".join(f"| {k} | **{v}** |" for k, v in rows))


# ================================================================
# 模块 7: 界面 —— 3D 模型与导出（重活，按需触发）
# ================================================================
def model_section(params: dict):
    st.markdown("### 🔧 3D 模型与导出")
    cq_ok, cq_msg = cq_status()
    st.caption("预览是毫秒级的；这里才真正调用 CadQuery 建模，所以单独放按钮，按需生成。")
    if cq_ok and cq_msg:
        st.caption("🧩 " + cq_msg)

    built = st.session_state.get("stl_params")
    stl_bytes = st.session_state.get("stl_bytes")
    stale = bool(built) and built != params

    left, right = st.columns([1, 1], gap="large")

    with left:
        if not cq_ok:
            st.error("这台电脑上的 CadQuery 加载失败，暂时只能看预览图（页面其它功能正常）。")
            with st.expander("查看原因和解决办法", expanded=False):
                st.code(cq_msg or "未知错误", language="text")
                st.markdown(
                    """
                    常见原因是 Windows 的「智能应用控制 / WDAC」拦截了
                    cadquery 依赖里的未签名扩展（例如 `ezdxf`、`casadi` 的 .pyd），
                    或者装的是和 Python 版本不匹配的 cadquery。

                    **三条路，任选一条：**

                    1. 换一个 Python 3.12 / 3.13 的虚拟环境，重新
                       `pip install cadquery==2.6.1`（最省事，也是作业原本的环境）；
                    2. 在「Windows 安全中心 → 应用和浏览器控制 → 智能应用控制」
                       里关闭它（注意：关闭后很难再打开，请自己权衡）；
                    3. 换一台没开这类策略的电脑做导出。
                    """
                )
        else:
            if st.button("🔧 生成 / 更新 STL", type="primary", **fit(st.button)):
                with st.spinner("🔧 正在生成 3D 模型..."):
                    try:
                        stl_bytes = cached_stl(
                            params["pen_dia"], params["grip_dia"],
                            params["slot_depth"], params["finger_len"],
                            params["palm_len"], params["palm_width"],
                        )
                    except Exception as exc:
                        st.error(f"❌ 模型生成失败：{type(exc).__name__}: {exc}")
                        stl_bytes = None
                    else:
                        st.session_state["stl_bytes"] = stl_bytes
                        st.session_state["stl_params"] = dict(params)
                        stale = False

            if stale:
                st.warning("参数改过了，点上面的按钮同步 3D 模型（预览图已经是新的）。", icon="⚠️")
            elif stl_bytes:
                st.success("✅ 模型已是最新，可直接下载导入切片软件。")

    with right:
        if stl_bytes:
            st.download_button(
                "⬇️ 下载 STL 模型",
                data=stl_bytes,
                file_name=(f"lefty_grip_pen{params['pen_dia']:g}"
                           f"_grip{params['grip_dia']:g}"
                           f"_s{params['slot_depth']:g}"
                           f"_f{params['finger_len']:g}"
                           f"_p{params['palm_len']:g}"
                           f"_w{params['palm_width']:g}.stl"),
                mime="model/stl",
                **fit(st.download_button),
            )
            st.caption(f"STL 大小 {len(stl_bytes) / 1024:.0f} KB · 单位 mm · 水密实体")
        elif cq_ok:
            st.info("还没有生成 STL。点左边的按钮，或先看预览图确认形状。", icon="ℹ️")
        else:
            st.info("CadQuery 恢复后，这里会出现 STL 下载按钮。右边先看预览图。", icon="ℹ️")

    if stl_bytes:
        print_estimate(stl_bytes, params)


# ================================================================
# 模块 7.5: 打印预估面板
# ================================================================
def print_estimate(stl_bytes: bytes, params: dict):
    st.markdown("#### 🖨️ 能不能打印")
    mat = st.radio("材料", list(MATERIALS.keys()), horizontal=True, key="material")
    stats = analyze_stl(stl_bytes)

    grams = stats["volume"] * MATERIALS[mat]
    minutes = estimate_print_minutes(stats["volume"])
    bbox = stats["bbox"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("材料用量", f"{grams:.1f} g", help=f"按 {mat} 密度 {MATERIALS[mat]} g/cm³")
    c2.metric("打印时间", f"{minutes:.0f} 分钟", help="0.2 mm 层高 / 0.45 线宽 / 60 mm/s 估算")
    c3.metric("悬垂面积", f"{stats['overhang']:.0f} mm²",
              help="法向朝下且陡于 45° 的面；其中大部分是 16 个防滑圆坑的凹面")
    c4.metric("三角面", f"{stats['tris']}")

    st.caption(f"实体体积 {stats['volume']:.2f} cm³ · 表面积 {stats['area']:.1f} cm² · "
               f"外形 {bbox[0]:.1f} × {bbox[1]:.1f} × {bbox[2]:.1f} mm")

    # 真正需要判断的是「卡槽上方那块悬空」——圆坑凹面太小，后果只是轻微下垂
    slot_w = min(14.0, max(6.0, params["finger_len"] * 0.16))
    slot_area = params["slot_depth"] * slot_w

    if slot_area <= 100:
        n_bumps = int(min(20, max(12, params["palm_width"] / 5)))
        advice = (f"竖直摆放、卡槽朝上，不用支撑。卡槽上方悬空仅 {slot_area:.0f} mm²，"
                  f"斜楔和 {n_bumps} 个圆坑的悬垂都很小。")
        st.success("✅ **" + advice + "**")
    elif slot_area <= 300:
        advice = (f"卡槽上方悬空 {slot_area:.0f} mm²，偏大，建议勾选「仅支撑悬垂」，"
                  f"或把卡槽深度降到 {max(params['slot_depth'] - 1, 0):.1f} mm 以下。")
        st.warning(advice, icon="⚠️")
    else:
        advice = (f"卡槽上方悬空 {slot_area:.0f} mm²，竖直打印会塌 —— 必须加支撑，"
                  "或把卡槽深度调小、卡槽朝下摆放。")
        st.error(advice)

    with st.expander("🔍 悬垂位置（仰视图，红色 = 陡于 45° 的面）", expanded=False):
        st.image(render_mesh_png(stl_bytes), **fit(st.image))
        st.caption("红色面法向朝下且陡于 45°，打印时靠桥接或支撑；底面贴平台，不计入悬垂。")

    derived = derive_dimensions(params["pen_dia"], params["grip_dia"],
                                params["finger_len"], params["palm_len"],
                                params["palm_width"])
    png_bytes = cached_preview(params["pen_dia"], params["grip_dia"],
                               params["slot_depth"], params["finger_len"],
                               params["palm_len"], params["palm_width"])[0]
    zip_bytes = build_zip(stl_bytes, png_bytes, params, derived, stats, mat, advice)
    md_bytes = build_markdown(params, derived, stats, mat, advice).encode("utf-8")

    dc1, dc2 = st.columns(2)
    with dc1:
        st.download_button(
            "📦 整套交付包（zip）",
            data=zip_bytes,
            file_name=(f"lefty_grip_pen{params['pen_dia']:g}_grip{params['grip_dia']:g}"
                       f"_H{derived['grip_height']:.0f}.zip"),
            mime="application/zip",
            help="STL + 预览图 + 悬垂图 + 参数表 + 打印说明 + 设计说明",
            **fit(st.download_button),
        )
    with dc2:
        st.download_button(
            "📄 设计说明（.md）",
            data=md_bytes,
            file_name=(f"设计说明_pen{params['pen_dia']:g}"
                       f"_grip{params['grip_dia']:g}.md"),
            mime="text/markdown",
            help="可直接当作业文档交，含尺寸链、结构说明、参数表、打印分析",
            **fit(st.download_button),
        )


def param_rows(params: dict, derived: dict):
    """参数表（CSV 与页面共用同一套口径）。"""
    return [
        ("笔杆直径", f"{params['pen_dia']:.1f} mm"),
        ("握持直径", f"{params['grip_dia']:.1f} mm"),
        ("配合间隙", f"+{CLEARANCE:.1f} mm"),
        ("内孔直径", f"{derived['bore']:.1f} mm"),
        ("外径", f"{derived['outer_d']:.1f} mm"),
        ("壁厚", f"{derived['wall']:.2f} mm"),
        ("卡槽深度", f"{params['slot_depth']:.2f} mm"),
        ("手指长度", f"{params['finger_len']:.0f} mm"),
        ("手掌长度", f"{params['palm_len']:.0f} mm"),
        ("手掌宽度", f"{params['palm_width']:.0f} mm"),
        ("总高度", f"{derived['grip_height']:.1f} mm"),
        ("指槽宽度", f"{derived['slot_width']:.1f} mm"),
        ("斜楔深度", f"{derived['wedge_height']:.1f} mm"),
        ("防滑圆坑", f"{derived['n_bumps']} 个"),
    ]


def build_zip(stl_bytes: bytes, png: bytes, params: dict, derived: dict,
              stats: dict, mat: str, advice: str) -> bytes:
    """打包交付：模型 + 预览图 + 悬垂图 + 参数表 + 打印说明。"""
    import io as _io
    import zipfile

    grams = stats["volume"] * MATERIALS[mat]
    minutes = estimate_print_minutes(stats["volume"])
    report = "\n".join([
        "左撇子握笔辅助器 · 打印说明",
        "=" * 34,
        "",
        f"材料：{mat}（密度 {MATERIALS[mat]} g/cm³）",
        f"材料用量：{grams:.1f} g",
        f"预估打印时间：{minutes:.0f} 分钟（0.2 mm 层高 / 0.45 线宽 / 60 mm/s）",
        f"实体体积：{stats['volume']:.2f} cm³ ｜ 表面积：{stats['area']:.1f} cm²",
        f"外形尺寸：{stats['bbox'][0]:.1f} × {stats['bbox'][1]:.1f} × {stats['bbox'][2]:.1f} mm",
        f"悬垂面积：{stats['overhang']:.0f} mm²（占 {stats['overhang_pct']:.1f}%）",
        "",
        "摆放建议：",
        f"  {advice}",
        "",
        "切片设置：",
        "  · 竖直摆放，卡槽朝上，无需支撑",
        "  · 层高 0.16~0.2 mm，壁厚按 3 圈走线",
        "  · TPU 最佳，PLA / PETG 亦可",
        "",
        "尺寸链：",
        "  内孔 = 笔杆直径 + 0.3 mm 间隙",
        "  壁厚 =（握持直径 − 内孔）/ 2",
    ])

    csv = "\ufeff参数,数值\n" + "\n".join(f"{k},{v}" for k, v in param_rows(params, derived))
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("grip.stl", stl_bytes)
        z.writestr("预览图.png", png)
        z.writestr("悬垂分析.png", render_mesh_png(stl_bytes))
        z.writestr("参数表.csv", csv)
        z.writestr("打印说明.txt", report)
        z.writestr("设计说明.md", build_markdown(params, derived, stats, mat, advice))
    return buf.getvalue()


def build_markdown(params: dict, derived: dict, stats: dict, mat: str,
                   advice: str) -> str:
    """生成可直接交作业的 Markdown 设计说明。"""
    from datetime import datetime

    grams = stats["volume"] * MATERIALS[mat]
    minutes = estimate_print_minutes(stats["volume"])
    table = "\n".join(f"| {k} | {v} |" for k, v in param_rows(params, derived))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""# 左撇子握笔辅助器 · 设计说明

> 由参数化生成工具导出 · {now}

## 一、设计目标

左手书写时，手腕需要额外外翻、视线容易被手掌挡住，食指和拇指的受力点也和右手完全不同。
普通笔杆没有为这种姿势做补偿，长时间书写容易累、也容易把笔握死。

这个握笔器要解决三件事：

1. **给左手食指一个明确的落点**（顶部斜楔）；
2. **让拇指有地方卡住、不打滑**（侧面卡槽 + 防滑圆坑）；
3. **适配不同粗细的笔和不同的手**（尺寸链由笔杆直径和手部尺寸推导）。

## 二、尺寸链

```
笔杆直径 {params['pen_dia']:.1f} mm
   + 配合间隙 {CLEARANCE:.1f} mm
   = 内孔 {derived['bore']:.1f} mm
握持直径 {params['grip_dia']:.1f} mm（手里的外径）
   壁厚 =（{params['grip_dia']:.1f} − {derived['bore']:.1f}）÷ 2 = {derived['wall']:.2f} mm
```

手部尺寸决定形态：

```
手掌长度 {params['palm_len']:.0f} mm × 0.18  → 总高 {derived['grip_height']:.1f} mm
手指长度 {params['finger_len']:.0f} mm × 0.16  → 指槽宽 {derived['slot_width']:.1f} mm
手指长度 {params['finger_len']:.0f} mm × 0.13  → 斜楔深 {derived['wedge_height']:.1f} mm
手掌宽度 {params['palm_width']:.0f} mm ÷ 5     → 防滑圆坑 {derived['n_bumps']} 个
```

## 三、结构说明

- **顶部斜楔**：从左上方切出一个斜面，左手食指搭上去时角度更自然，不用靠手腕硬掰；
- **侧面卡槽**：深 {params['slot_depth']:.2f} mm，给拇指一个明确的定位凹槽，防止笔杆转动；
- **防滑圆坑**：外表面均布 {derived['n_bumps']} 个半径 1.2 mm 的小凹坑，提升抓握摩擦；
- **壁厚校核**：壁厚低于 {MIN_WALL} mm 时会自动放大外径，避免打印出来一捏就裂。

## 四、参数表

| 参数 | 数值 |
|---|---|
{table}

## 五、打印分析

| 项目 | 数值 |
|---|---|
| 材料 | {mat}（{MATERIALS[mat]} g/cm³） |
| 材料用量 | {grams:.1f} g |
| 预估时间 | {minutes:.0f} 分钟（0.2 mm 层高 / 0.45 线宽 / 60 mm/s） |
| 实体体积 | {stats['volume']:.2f} cm³ |
| 表面积 | {stats['area']:.1f} cm² |
| 外形尺寸 | {stats['bbox'][0]:.1f} × {stats['bbox'][1]:.1f} × {stats['bbox'][2]:.1f} mm |
| 悬垂面积 | {stats['overhang']:.0f} mm²（占 {stats['overhang_pct']:.1f}%） |

**摆放建议**：{advice}

## 六、附件

| 文件 | 说明 |
|---|---|
| `grip.stl` | 模型文件，单位 mm，水密实体 |
| `预览图.png` | 正视图 + 俯视图，含尺寸标注 |
| `悬垂分析.png` | 仰视图，红色为陡于 45° 的悬垂面 |
| `参数表.csv` | 全部参数，可直接进 Excel |
"""


# ================================================================
# 模块 8: 说明 / 照片
# ================================================================
def design_notes():
    st.markdown(
        """
        本握笔器专为**左撇子**书写姿势设计：

        - **尺寸链**：笔杆直径 → 内孔（+0.3 mm 配合间隙）→ 壁厚 =（握持直径 − 内孔）/ 2；
          壁厚低于 1.2 mm 时自动放大外径，保证能打印；
        - **顶部斜楔**：为左手食指提供搭放斜面，引导正确握姿；
        - **侧面卡槽**：深度可调，用于拇指 / 食指定位防滑；
        - **手部自适应**：手指长度 → 指槽宽度与斜楔深度；手掌长度 → 总高度；
          手掌宽度 → 防滑圆坑密度；
        - **防滑圆坑**：外表面均布，提升抓握摩擦力；
        - **预览图**：正视图 + 俯视图按当前参数等比缩放，可单独导出 PNG。
        """
    )


def print_notes():
    st.markdown(
        """
        - **材料**：TPU 柔性材料最佳，PLA / PETG 亦可；
        - **摆放**：竖直放置打印，卡槽朝上，无需支撑；
        - **层高**：0.16~0.2 mm；壁厚 3 mm 已按 3 圈走线设计；
        - **公差**：内径建议比笔杆直径大 0.2~0.3 mm，装取更顺；
        - **验证**：先用 10% 尺寸打印一个试件，确认指槽位置再打整件。
        """
    )


def reference_photo():
    img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), IMAGE_FILE)
    if os.path.exists(img_path):
        st.image(img_path, caption="左撇子握笔辅助器使用示意（实拍参考）",
                 **fit(st.image))
    else:
        st.info(f"没有找到参考照片 `{IMAGE_FILE}`，把它放到本脚本同目录即可显示。")


# ================================================================
# 模块 9: 主入口
# ================================================================
def main():
    st.set_page_config(
        page_title="左撇子握笔辅助器生成器",
        page_icon="✍️",
        layout="wide",
    )

    # 参数先塞进 session_state：滑块只认 key、不传 value，
    # 这样「恢复默认」回调改状态时不会触发默认值告警。
    for key, value in DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value

    st.title("✍️ 左撇子握笔辅助器 · 参数化生成工具")
    st.caption("《AIGC编程基础》课程作业 | Python + Streamlit + Pillow"
               "（CadQuery 负责导出 STL）| 参数一动，右侧预览图单独重绘")

    params_and_preview()

    params = current_params()

    st.divider()
    model_section(params)
    st.divider()

    tab1, tab2, tab3 = st.tabs(["🖨️ 设计说明", "🧾 打印建议", "📷 参考照片"])
    with tab1:
        design_notes()
    with tab2:
        print_notes()
    with tab3:
        reference_photo()

    st.caption("生成引擎：CadQuery 2.x ｜ 预览：Pillow ｜ 界面：Streamlit")


def _launch_in_browser():
    """直接 `python grip_app_v2.py` 时，自动转成 streamlit 启动并打开网页，
    免得只看到一堆 missing ScriptRunContext 警告。"""
    import socket
    import sys
    from streamlit.web import cli as stcli

    def free_port(start=8501, end=8599):
        for port in range(start, end + 1):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        return start

    port = free_port()
    sys.argv = ["streamlit", "run", os.path.abspath(__file__),
                "--server.port", str(port),
                "--server.headless", "false",
                "--browser.gatherUsageStats", "false"]
    stcli.main()


if __name__ == "__main__":
    if st.runtime.exists():
        main()
    else:
        _launch_in_browser()
