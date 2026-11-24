"""看屏·按变化取帧 —— WhaleLife 边车版。

从 AstrBot 的 astrbot_plugin_see_now 0.1.0 搬来（思路借自 dsh-see-now，Apache-2.0，
作者 SAOS-3624）。抓屏、判分、留帧、定档、装箱、认字这一整条主线一行没改；
只把「AstrBot 插件」换成「命令行边车」：参数走命令行，结果一行 JSON 到 stdout，
日志全走 stderr，父进程读完就退出。

用法：
    python see_now_sidecar.py [选项]

选项：
    --seconds 10           抓多久（2–60 秒）
    --max-frames 6         一张图最多装几帧（1–12）
    --tier-style native    native / up / down
    --tier-lock 0          锁死一档 1-3，0 = 不锁，压过 tier-style
    --focus auto           auto / whole / content（auto：热点 8-60% 才裁）
    --max-width 1600       成品图最大宽度，超出等比缩
    --shot-dir DIR         截图目录，默认脚本旁边的 shots
    --text-layer           UIA + OCR 认一次字（默认关，一次几百毫秒）
    --grid auto            叠坐标网格（0 关 / auto / 像素步长）
    --list                 只打印目录和依赖自检

识图不在这里做：出的是图，谁来看由 WhaleLife 那头决定。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageGrab  # noqa: F401


class logger:
    """AstrBot 的 logger 垫片：原文里 logger.info(...) 全是 %-格式，照原样能用。"""

    @staticmethod
    def _emit(level: str, msg, *a) -> None:
        try:
            text = (msg % a) if a else str(msg)
        except Exception:
            text = str(msg)
        print("[see_now] %s %s" % (level, text), file=sys.stderr, flush=True)

    @classmethod
    def info(cls, msg, *a):
        cls._emit("I", msg, *a)

    @classmethod
    def warning(cls, msg, *a):
        cls._emit("W", msg, *a)

    @classmethod
    def error(cls, msg, *a):
        cls._emit("E", msg, *a)


# AstrBot 的这几个名字只出现在搬过来但用不上的残留函数签名里，给个占位免得 NameError。
AstrMessageEvent = object
filter = object
Context = object
Star = object


def register(*_a, **_k):
    return lambda cls: cls


# ---- 采集 ----
#: 最近一次抓屏拿到的物理分辨率。坐标网格要靠它把图上像素换算回屏幕像素。
_RAW_SIZE = [0, 0]
#: 这次抓的是哪块屏——它在虚拟桌面里的物理左上角。多屏时主屏不一定是 (0,0)，
#: 坐标网格要靠它把图上像素换算回真实屏幕坐标。
_SHOT_ORIGIN = [0, 0]
# 抓屏范围：screen 按显示器设置抓整屏；window 只抓前台窗口那一块（窗口动，逐帧跟着走）。
_TARGET = "screen"
_WIN_SCREEN = None       # 窗口模式下用来夹边界的屏尺寸
_WIN_FALLBACK = False    # 窗口没抓到、退回整屏时为 True，写进说明

#: 这次要抓哪些显示器（dict 列表，空 = 用 ImageGrab 默认的主屏）。
_MON_RECTS = []
#: 整台机器上所有显示器，多屏时要拿未选中的那些去涂灰。
_ALL_MONS = []
#: 多屏的遮罩，第一次抓屏时按尺寸建一次，之后复用。
_MON_MASK = None
FPS = 6
SHOT_W = 800             # 成品宽度；高度不写死，按抓到的屏的原比例算
SHOT = (SHOT_W, 450)     # 兜底尺寸：还没抓到屏、或尺寸异常时用（16:9）

# ---- 判分（口径照 dsh-see-now 的 README）----
GRID = (64, 36)
CUT_ABS = 12.0          # 绝对地板：低于它一律不算切点
CUT_PEAK = 2.5          # 尖峰：要比邻居大这么多才算「一下子」
REFRACT = 0.4           # 不应期，秒
NEED = 144.0            # 积分阈值 = 4 分 × 12fps × 3 秒

# ---- 裁切：模型那头的图会被压到同一个 token 栅格，全屏送过去等于自废细节 ----
#: 成品包的上限。控件四十件、合起来八十块——再多，模型读它跟读整屏没区别。
_BLOCK_CTRL_CAP = 40
_BLOCK_CAP = 80

DIFF_GRID = (320, 180)  # 算包围盒用的分辨率
DIFF_LEVEL = 10.0       # 灰度差多少算「变了」
_DIFF_LEVEL = DIFF_LEVEL # 运行时可调：包围盒阈值（配置 diff_level）
CROP_PAD = 24           # 包围盒往外放宽多少像素（原图尺度）
CROP_MAX = 0.55         # 变化区占全屏超过这个比例就不裁，不如整屏看
#: 单帧裁切：变化区的宽或高窄到全屏的这个比例以下就不裁。面积小不代表形状能用——
#: 一个细高条（侧栏在刷）和一个扁宽条（任务栏在闪）面积都可能很小，裁出来没法看。
CROP_MIN_SIDE = 0.10
#: 单帧裁切后的兜底封顶。形状判据没想到的情况靠它兜住，不是主力。
CROP_MAX_W = 800
CROP_MAX_H = 1400

# ---- 档位：分数越高，占的格子越大 ----
TIERS = ((45.0, 1), (18.0, 2), (0.0, 3))
TIER_MAX = 3            # 最大档号；k 越小格子越大，1 档独占整行
#: 一张成图的高度上限。锁档时帧数按它截断——六帧全锁 1 档是 2700px 的长条，
#: token 全花在空处。三行封顶，够看也够省。
#: 一张成图的高度上限改成按屏比例算，见 _max_pack_h()

# ---- 认内容：整屏递过去，一半格子交给界面外壳，真正在看的东西反而只有半幅 ----
#: 热点区低于这个占屏比例，说明变化是零散噪点，没内容区可挑。
FOCUS_MIN = 0.06
#: 高于这个比例，说明满屏都在动（切页、拖窗口），挑出来也没意义，整幅更好。
FOCUS_MAX = 0.85
#: auto 档：热点占屏落在这个区间才裁，太小是噪点、太大是整屏在动。
AUTO_MIN = 0.08
AUTO_MAX = 0.60
#: 前台窗口占屏区间。低于下限是浮窗/提示条，高于上限是全屏——两头都不裁。
FG_MIN = 0.08
FG_MAX = 0.92
#: 热点图累计峰值的门槛。整段都没热起来，就别硬裁一块出来放大空气。
FOCUS_PEAK = 3.0
#: 认内容时换一套留帧口径。整屏平均差一两分其实就是视频在放，原来那套（12 分起）
#: 会把整段画面当静止，一帧不留——裁得再准也没有帧可裁。
FOCUS_CUT_ABS = 2.5
FOCUS_SPIKE = 2.0
FOCUS_NEED = 24.0


def _grid(img: "Image.Image", step: int = 0,
          origin: tuple[int, int] = (0, 0),
          scale: tuple[float, float] = (1.0, 1.0)) -> tuple["Image.Image", int]:
    """在原图上叠一层坐标网格，边上标出像素坐标。

    模型看图只能说「右上角那个按钮」；有了刻度就能说「(1480, 320)」，
    做裁剪、点坐标时不用再猜。标出来的是这张图自己的像素坐标。
    step 传 0 就按宽度自动挑：线太密看着糊，太疏又定不准位。
    origin / scale 用来把图上的像素换算回屏幕像素：图是缩过的，
    标屏幕坐标才有用——说「(1480, 320)」能直接拿去点，说「(460, 100)」没人知道是哪。
    origin 是「图上 (0,0) 那一点对应的屏幕坐标」，所以换算就是 origin + 图上坐标 × scale。
    以前写的是 (x - origin) × scale，只在 origin 恰好等于边框宽度、屏幕原点又是 0 时才对——
    抓窗口、副屏、裁切这三种情况全都偏，等于刻度白标。
    """
    w, h = img.size
    if not step or step <= 0:
        step = 128 if w >= 1600 else 96
        while w // step > 14:
            step *= 2
        while w // step < 6 and step > 32:
            step = max(32, int(step * 0.75))
    step = max(16, int(step))

    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out, "RGBA")
    for x in range(step, w, step):
        draw.line([(x, 0), (x, h)], fill=(255, 90, 90, 105), width=1)
    for y in range(step, h, step):
        draw.line([(0, y), (w, y)], fill=(255, 90, 90, 105), width=1)

    font = None
    for name in ("arialbd.ttf", "arial.ttf", "seguisb.ttf"):
        try:
            font = ImageFont.truetype(name, 15)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    def sx(x: int) -> int:
        return int(round(origin[0] + x * scale[0]))

    def sy(y: int) -> int:
        return int(round(origin[1] + y * scale[1]))

    def tag(x: int, y: int, text: str) -> None:
        box = draw.textbbox((x, y), text, font=font)
        pad = 2
        draw.rectangle(
            (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad),
            fill=(255, 255, 255, 210),
        )
        draw.text((x, y), text, fill=(190, 30, 30, 255), font=font)

    # 靠边那格的数字要往回收，否则正好被画布边缘裁掉一半。
    for x in range(step, w, step):
        text = str(sx(x))
        tw = draw.textbbox((0, 0), text, font=font)[2]
        tx = x + 2
        if tx + tw + 4 > w:
            tx = max(2, x - tw - 4)
        tag(tx, 2, text)
    for y in range(step, h, step):
        text = str(sy(y))
        th = draw.textbbox((0, 0), text, font=font)[3]
        ty = y + 1
        if ty + th + 2 > h:
            ty = max(2, y - th - 2)
        tag(2, ty, text)
    # 原屏尺寸只写进说明，不画在图上——左上角那格会盖住第一条竖线的刻度。
    return out, step


def _shot() -> tuple[Image.Image, np.ndarray]:
    """一次抓屏，出两份：原图留着备取，小图拿去判分。

    原先每帧都缩到 800×450 再缩到 64×36，中间那张大图九成时候没人要。
    抓一次花 46 毫秒，白缩一次 18 毫秒——省下的不是钱，是采样率。

    多显示器：_MON_RECTS 是这次选中的那几块。
    只有一块时直接按它的矩形抓，快，而且图就是那一块；
    选了不止一块时抓整个虚拟桌面，未选中的区域涂成深灰——保留相对位置，
    坐标才换得回去（横排拼起来坐标就废了）。
    """
    global _MON_MASK, _WIN_FALLBACK, _APP_FALLBACK
    rects = _MON_RECTS
    if _TARGET == "app":
        hit = _pick_app()
        if hit is not None:
            got = _print_window(hit["hwnd"])
            if got is not None:
                img, org = got
                _SHOT_ORIGIN[0], _SHOT_ORIGIN[1] = org
                _RAW_SIZE[0], _RAW_SIZE[1] = img.size
                small = np.asarray(img.convert("RGB").resize(GRID, Image.BILINEAR),
                                   dtype=np.float32)
                return img, small
        # 没匹配到、或匹配到的都最小化了：退回整屏，别让整次调用失败。
        _APP_FALLBACK = True
    if _TARGET == "window":
        box = _window_box(_WIN_SCREEN or _display_size(rects))
        if box:
            img = ImageGrab.grab(bbox=box)
            _SHOT_ORIGIN[0], _SHOT_ORIGIN[1] = box[0], box[1]
            _RAW_SIZE[0], _RAW_SIZE[1] = img.size
            small = np.asarray(img.convert("RGB").resize(GRID, Image.BILINEAR), dtype=np.float32)
            return img, small
        # 窗口最小化、拿不到句柄：退回正常抓法，别让整次调用失败。
        _WIN_FALLBACK = True
    if not rects:
        img = ImageGrab.grab()
        _SHOT_ORIGIN[0], _SHOT_ORIGIN[1] = 0, 0
    elif len(rects) == 1:
        m = rects[0]
        img = ImageGrab.grab(bbox=(m["left"], m["top"], m["right"], m["bottom"]))
        _SHOT_ORIGIN[0], _SHOT_ORIGIN[1] = m["left"], m["top"]
    else:
        img = ImageGrab.grab(all_screens=True)
        vx = min(m["left"] for m in (_ALL_MONS or rects))
        vy = min(m["top"] for m in (_ALL_MONS or rects))
        _SHOT_ORIGIN[0], _SHOT_ORIGIN[1] = vx, vy
        if _MON_MASK is None or _MON_MASK.size != img.size:
            mask = Image.new("L", img.size, 255)
            pen = ImageDraw.Draw(mask)
            for m in rects:
                pen.rectangle(
                    (m["left"] - vx, m["top"] - vy,
                     m["right"] - vx - 1, m["bottom"] - vy - 1),
                    fill=0,
                )
            _MON_MASK = mask
        img = img.copy()
        img.paste((36, 36, 42), (0, 0), _MON_MASK)
    _RAW_SIZE[0], _RAW_SIZE[1] = img.size
    small = np.asarray(img.convert("RGB").resize(GRID, Image.BILINEAR), dtype=np.float32)
    return img, small


def _fit_size(w: int, h: int, width: int = SHOT_W) -> tuple[int, int]:
    """按原图比例算成品尺寸：宽度固定，高度跟着比例走。

    原来一律缩成 SHOT（800×450），而屏多是 16:10，每一帧都被压扁约 7%；
    多帧拼图里裁出来的内容区比例更杂，压得更狠。
    """
    if w <= 0 or h <= 0:
        return SHOT
    return (width, max(1, int(round(width * h / float(w)))))


def _max_pack_h() -> int:
    """一张成图的高度上限。按屏的真实比例算，不再写死成 16:9 的三行。"""
    if _RAW_SIZE[0]:
        r = _RAW_SIZE[1] / float(_RAW_SIZE[0])
    else:
        r = SHOT[1] / float(SHOT[0])
    return max(SHOT[1], int(round(SHOT_W * r * 3)))


def _big(img: Image.Image) -> np.ndarray:
    """只有决定留帧了，才把它缩到装图用的尺寸。比例照原图，不压扁。"""
    return np.asarray(img.convert("RGB").resize(_fit_size(*img.size), Image.BILINEAR), dtype=np.float32)


def _score(prev: np.ndarray, cur: np.ndarray) -> float:
    """0-255 的变化分：HSV 三通道加权差，H 走环形。"""
    a = np.asarray(Image.fromarray(prev.astype(np.uint8)).convert("HSV"), dtype=np.float32)
    b = np.asarray(Image.fromarray(cur.astype(np.uint8)).convert("HSV"), dtype=np.float32)
    dh = np.abs(a[..., 0] - b[..., 0])
    dh = np.minimum(dh, 255.0 - dh)
    ds = np.abs(a[..., 1] - b[..., 1])
    dv = np.abs(a[..., 2] - b[..., 2])
    return float((0.20 * dh + 0.25 * ds + 0.55 * dv).mean())


def _tier(s: float) -> int:
    for floor, k in TIERS:
        if s >= floor:
            return k
    return 3

def _pick_tier(s: float, style: str = "native", lock: int = 0) -> int:
    """分数 → 档号。lock 压过 style；style 只把算出来的结果整体挪一档。

    k 越小格子越大：1 档独占整行（800×450），3 档三分之一宽。原版把这两件事
    拧在一起——节奏一变，格子跟着变，用户看不出是哪儿改的，也改不动。这里拆开。
    """
    if lock:
        return max(1, min(TIER_MAX, int(lock)))
    k = _tier(s)
    if style == "up":
        k = max(1, k - 1)
    elif style == "down":
        k = min(TIER_MAX, k + 1)
    return k

def _frame_limit(lock: int) -> int:
    """锁档后一张图还塞得下几帧；不锁就返回 12（工具参数的上限）。

    1 档一行只放得下一个，三行封顶就是 3 帧。2、3 档一行放 k 个，
    高度预算按屏的真实比例估——原来拿写死的 16:9 高度算，现在屏多是 16:10，
    会少算一行。
    """
    if not lock:
        return 12
    k = max(1, min(TIER_MAX, int(lock)))
    per_row = k
    r = (_RAW_SIZE[1] / float(_RAW_SIZE[0])) if _RAW_SIZE[0] else SHOT[1] / float(SHOT[0])
    row_h = max(1, int(round(SHOT[0] / float(k) * r)))
    rows = max(1, _max_pack_h() // row_h)
    return max(1, min(12, per_row * rows))

def _tier_note(plan: dict | None, style: str, lock: int) -> str:
    """给人看的一行档位说明："锁 2 档（2档×4，共 4 帧）"。

    以前只能看出「格子变了」，说不清是节奏、偏移还是锁档干的。
    """
    tiers = (plan or {}).get("tiers") or {}
    if not tiers:
        return ""
    if lock:
        head = "锁 %d 档" % lock
    elif style == "up":
        head = "整体大一档"
    elif style == "down":
        head = "整体小一档"
    else:
        head = "按变化分档"
    body = "，".join("%d档×%d" % (k, n) for k, n in sorted(tiers.items()))
    tail = "，共 %d 帧" % sum(tiers.values())
    dropped = (plan or {}).get("dropped") or 0
    if dropped:
        tail += "；另 %d 帧超高度上限没装" % dropped
    return "%s（%s%s）" % (head, body, tail)


def _changed_box(prev: Image.Image, cur: Image.Image):
    """两张原图之间，到底哪一块变了。返回原图坐标系里的 (l, t, r, b)，没变就返回 None。

    这一步是本地算的：阈值、比较、包围盒，都不花一个 token。模型只负责解释
    「这块变成什么了」，不用它判断「有没有变」——它在这方面爱编。
    """
    if prev is None:
        return None
    a = np.asarray(prev.convert("L").resize(DIFF_GRID, Image.BILINEAR), dtype=np.float32)
    b = np.asarray(cur.convert("L").resize(DIFF_GRID, Image.BILINEAR), dtype=np.float32)
    mask = np.abs(a - b) > _DIFF_LEVEL
    if not mask.any():
        return None
    ys, xs = np.nonzero(mask)
    gw, gh = DIFF_GRID
    W, H = cur.size
    sx, sy = W / float(gw), H / float(gh)
    left = max(0.0, xs.min() * sx - CROP_PAD)
    top = max(0.0, ys.min() * sy - CROP_PAD)
    right = min(float(W), (xs.max() + 1) * sx + CROP_PAD)
    bottom = min(float(H), (ys.max() + 1) * sy + CROP_PAD)
    return (left, top, right, bottom)


def _crop_for_model(img: Image.Image, box, min_side: float = CROP_MIN_SIDE):
    """只把变化那一块裁出来。小图会被原样放过，细节才保得住。

    返回 (图, 说明)；变化太大、形状太窄或没有变化时返回 None，让调用方走整屏。

    min_side 是形状判据：变化区的宽或高窄到全屏这个比例以下就不裁。面积限制不住
    形状，一道竖条占屏才一成也可能被裁成一根面条。
    """
    if box is None:
        return None
    W, H = img.size
    l, t, r, b = box
    area = max(0.0, (r - l)) * max(0.0, (b - t))
    if area <= 0 or area > (W * H) * CROP_MAX:
        return None
    if (r - l) < W * min_side or (b - t) < H * min_side:
        return None
    crop = img.crop((int(l), int(t), int(r), int(b)))
    if crop.size[0] < 120 or crop.size[1] < 90:
        return None
    return crop


def _fit_crop(crop: "Image.Image", max_w: int, max_h: int) -> "Image.Image":
    """裁出来的图按宽、高两个上限等比缩小，只缩不放。

    形状判据挡住「细高条」「扁宽条」绝大多数情况；这一层是兜底，防的是判据没想到
    的形状。最坏的情况原来是：2560×1600 屏上一条 300 宽的侧栏在动，占屏 11.7%
    被放行，按宽缩到 800 是放大 2.3 倍，高就是 3677px——一张图全花在空处。
    """
    if crop.width <= 0 or crop.height <= 0:
        return crop
    cap_w = min(int(max_w), CROP_MAX_W)
    ratio = min(1.0, cap_w / float(crop.width), float(max_h) / float(crop.height))
    if ratio >= 1.0:
        return crop
    return crop.resize((max(1, int(crop.width * ratio)), max(1, int(crop.height * ratio))),
                       Image.LANCZOS)


def _pack_font(size: int = 13):
    for name in ("arialbd.ttf", "arial.ttf", "seguisb.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _pack(frames: list[tuple[np.ndarray, float]], out: str,
          style: str = "native", lock: int = 0, max_width: int = 0) -> dict:
    """按档位分行摆：格子宽度照着档位分，高度按每一帧自己的比例算。

    原来高度写死成 SHOT[1] // 3 的整数倍，等于给每帧发一个 16:9 的框；而屏多是
    16:10，每帧都被压扁一点。模型看久了会把比例读错，坐标跟着偏。
    现在行宽不变，行高取该行各帧按比例算出的最大值，帧在行内居中——
    多帧里那张裁出来的内容区（比例各不相同）也不会再被硬拉伸。

    只有一帧时强制铺满。lock > 0 时所有帧压到同一档，超过高度预算的从尾巴砍掉：
    按时间顺序留前面，后面的变化没人在等了，宁可少给也别给一张超长条。

    返回 {"path", "tiers": {档号: 帧数}, "dropped", "size"}。
    """
    single = len(frames) == 1
    row_w = SHOT[0]                       # 满宽一行（画布实际宽度是 col_w * 3，比它窄 2px）
    col_w = row_w // 3                    # 一格的列宽，档位在它上面乘
    gap = 6

    def tile_size(img: np.ndarray, w: int) -> tuple[int, int]:
        inner = max(1, w - gap)
        sh, sw = img.shape[0], img.shape[1]
        return inner, max(1, int(round(inner * sh / float(max(1, sw)))))

    rows: list[list[tuple[np.ndarray, int]]] = []
    cur: list[tuple[np.ndarray, int]] = []
    used = 0
    for img, s in frames:
        k = 1 if single else _pick_tier(s, style, lock)
        w = col_w * 3 // k
        if used + w > row_w and cur:
            rows.append(cur)
            cur, used = [], 0
        cur.append((img, k, s))
        used += w
    if cur:
        rows.append(cur)

    def row_h(row: list[tuple[np.ndarray, int]]) -> int:
        return max(tile_size(img, col_w * 3 // k)[1] for img, k, _s in row)

    dropped = 0
    if lock and not single and rows:
        while len(rows) > 1:
            # 只比图本身的高度，间隔不计——间隔是包装，不该把整帧挤出去。
            if sum(row_h(r) for r in rows) <= _max_pack_h():
                break
            dropped += len(rows.pop())

    heights = [row_h(r) for r in rows]
    canvas = Image.new("RGB", (col_w * 3, sum(heights) + gap * (len(rows) + 1)),
                       (18, 18, 18))
    pen = ImageDraw.Draw(canvas, "RGBA")
    font = _pack_font()
    y = gap
    tiers: dict[int, int] = {}
    for row, h in zip(rows, heights):
        x = gap
        for img, k, s in row:
            w = col_w * 3 // k
            tw, th = tile_size(img, w)
            tile = Image.fromarray(img.astype(np.uint8)).resize((tw, th), Image.BILINEAR)
            ty = y + max(0, (h - th) // 2)
            canvas.paste(tile, (x, ty))
            if not single:
                # 每帧角上写两样：相对开始的秒数、图上一像素等于屏幕多少像素。
                # 拼图里的像素坐标换不回屏幕，光看一格格图分不清先后、也说不清位置。
                ratio = (_RAW_SIZE[0] / float(max(1, tw))) if _RAW_SIZE[0] else 1.0
                label = "+%.1fs  x%.1f" % (float(s), ratio)
                box = pen.textbbox((0, 0), label, font=font)
                lx, ly = x + 3, ty + 2
                pen.rectangle((lx - 2, ly - 1, lx + (box[2] - box[0]) + 2,
                               ly + (box[3] - box[1]) + 2), fill=(18, 18, 18, 195))
                pen.text((lx, ly), label, fill=(255, 208, 110, 255), font=font)
            x += w
            tiers[k] = tiers.get(k, 0) + 1
        y += h + gap
    if max_width and canvas.width > max_width:
        ratio = max_width / float(canvas.width)
        canvas = canvas.resize((max_width, max(1, int(canvas.height * ratio))), Image.LANCZOS)
    canvas.save(out)
    return {"path": out, "tiers": tiers, "dropped": dropped, "size": canvas.size}



def _dpi_scale(hwnd=None) -> float:
    """Win32 给窗口坐标是逻辑像素还是物理像素，取决于进程有没有声明 DPI 感知。

    ImageGrab 抓的永远是物理像素，所以这里要把窗口框换算到物理坐标上。
    进程已经感知了（awareness != 0）就不用换，返回 1.0。
    """
    if not sys.platform.startswith("win"):
        return 1.0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        aware = None
        try:
            v = ctypes.c_int()
            if ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(v)) == 0:
                aware = v.value != 0
        except Exception:
            aware = None
        if aware is True:
            return 1.0
        if aware is None:
            # 判断不出来就别乘：乘错比不乘更糟（会把框裁到一小块）
            return 1.0
        dpi = 0
        if hwnd:
            try:
                dpi = int(user32.GetDpiForWindow(ctypes.c_void_p(int(hwnd))) or 0)
            except Exception:
                dpi = 0
        if not dpi:
            try:
                dpi = int(user32.GetDpiForSystem() or 0)
            except Exception:
                dpi = 0
        if not dpi:
            return 1.0
        return dpi / 96.0
    except Exception:
        return 1.0


def _monitors() -> list[dict]:
    """列出这台机器上的显示器，按 1 编号，主屏排第一。

    坐标来源是 EnumDisplayMonitors —— 它跟 GetWindowRect 一样受进程 DPI 感知影响，
    而 ImageGrab 抓的是物理像素，所以要乘一次缩放比（跟取窗口框是同一个坑）。
    """
    if not sys.platform.startswith("win"):
        return []
    try:
        import ctypes

        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong),
                        ("szDevice", ctypes.c_wchar * 32)]

        found: list[dict] = []
        PROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(RECT), ctypes.c_double)

        def _cb(hmon, hdc, lprc, data):
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcMonitor
                found.append({
                    "device": mi.szDevice,
                    "left": int(r.left), "top": int(r.top),
                    "right": int(r.right), "bottom": int(r.bottom),
                    "primary": bool(mi.dwFlags & 1),
                })
            return 1

        user32.EnumDisplayMonitors(None, None, PROC(_cb), 0)
        if not found:
            return []
        sc = _dpi_scale()
        if sc != 1.0:
            for m in found:
                for k in ("left", "top", "right", "bottom"):
                    m[k] = int(round(m[k] * sc))

        # 自校准：_dpi_scale 走的是 GetDpiForSystem，未声明 DPI 感知的进程会被
        # 虚拟化成 96，返回 1.0 —— 可 ImageGrab 抓的是物理像素。两者差 1.5 倍时，
        # 单屏抓出来就只有屏幕左上角那一块（之前实测 1707×1067，真屏 2560×1600）。
        # 拿一张实抓的主屏图比宽度，以它为准，不信任何一层换算。
        try:
            probe = ImageGrab.grab()
            pw = probe.size[0]
            pm = [m for m in found if m["primary"]] or found
            base = (pm[0]["right"] - pm[0]["left"]) if pm else 0
            if base > 0 and pw > 0:
                factor = pw / float(base)
                if 0.25 < factor < 4.0 and abs(factor - 1.0) > 0.02:
                    for m in found:
                        for k in ("left", "top", "right", "bottom"):
                            m[k] = int(round(m[k] * factor))
        except Exception as exc:
            logger.warning("[see_now] 显示器尺寸校准失败，按原值用：%s", exc)
        found.sort(key=lambda m: (not m["primary"], m["left"], m["top"]))
        for i, m in enumerate(found, 1):
            m["n"] = i
            m["w"] = m["right"] - m["left"]
            m["h"] = m["bottom"] - m["top"]
        return found
    except Exception as exc:
        logger.warning("[see_now] 列显示器失败：%s", exc)
        return []


def _pick_monitors(spec, mons=None) -> list[dict]:
    """按配置挑要抓的屏。

    primary / 空 = 主屏；all = 全部；也可以写编号，用逗号隔开，比如 1,3。
    写错了就当主屏——宁可抓错一块，也别一帧不抓。
    """
    mons = mons if mons is not None else _monitors()
    if not mons:
        return []
    raw = str(spec or "").strip().lower().replace("，", ",")
    primary = [m for m in mons if m["primary"]] or [mons[0]]
    if raw in ("", "primary", "main", "主屏"):
        return primary[:1]
    if raw in ("all", "*", "全部"):
        return list(mons)
    picks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        for m in mons:
            if m["n"] == n and m not in picks:
                picks.append(m)
    # 按编号排一遍：配置里写 2,1 也按 1,2 抓，拼图顺序才稳定。
    picks.sort(key=lambda m: m["n"])
    return picks or primary[:1]


def _set_monitors(spec) -> list[dict]:
    """把这次要抓的屏记到全局，_shot 每次抓屏都按它来。"""
    global _MON_RECTS, _ALL_MONS, _MON_MASK
    mons = _monitors()
    picked = _pick_monitors(spec, mons)
    _ALL_MONS = mons
    _MON_RECTS = picked
    _MON_MASK = None
    return picked


def _display_size(rects=None):
    """这次能抓到的最大屏尺寸，给窗口框夹边界用。"""
    if _ALL_MONS:
        return (max(m["right"] for m in _ALL_MONS),
                max(m["bottom"] for m in _ALL_MONS))
    if rects:
        return (rects[0]["w"], rects[0]["h"])
    try:
        return ImageGrab.grab().size
    except Exception:
        return (1920, 1080)


# ---- 按名字抓窗口（target = app）----
#: 这次匹配的串（进程名或窗口标题片段，不分大小写）。空 = 没填，抓不到就退回整屏。
_APP_SPEC = ""
#: 这次抓到的是哪个窗口，写进说明用。
_APP_HIT = None
#: 传了 app 却没抓到、退回整屏时为 True。
_APP_FALLBACK = False
#: 这次匹配到几个窗口。多个时取面积最大的那个。
_APP_MATCHES = 0
#: 匹配到的窗口里有几个是无响应的（IsHungAppWindow）。
_APP_HUNG = 0
#: PrintWindow 拿回来的图几乎是单色（全黑、全白）时记下的原因；空串表示没这回事。
_PRINT_BLANK = ""


def _proc_name(pid: int) -> str:
    """pid 对应的可执行文件名（不带目录）；拿不到就空串。"""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        if not ok:
            return ""
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
    except Exception:
        return ""


def _app_list(spec: str) -> list[dict]:
    """当前可见窗口里，标题或进程名含 spec 的那些。

    进程名连 .exe 一起比，填「horizon-client」和「horizon-client.exe」都能中。
    最小化的也列出来——抓不抓得到由调用方决定，不在这里静默丢掉。
    """
    out: list[dict] = []
    if not spec or not sys.platform.startswith("win"):
        return out
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            title = ""
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, b, n + 1)
                title = b.value
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = _proc_name(pid.value)
            if spec not in title.lower() and spec not in proc.lower():
                return True
            r = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            out.append({
                "hwnd": hwnd, "title": title, "proc": proc,
                "iconic": bool(u32.IsIconic(hwnd)),
                "hung": bool(u32.IsHungAppWindow(hwnd)),
                "w": r.right - r.left, "h": r.bottom - r.top,
            })
            return True

        u32.EnumWindows(_cb, 0)
    except Exception:
        return out
    return out


def _pick_app():
    """挑这次要抓的那个窗口：非最小化、尺寸够大的里面面积最大的一个。

    多个匹配是常事——同一个程序有主窗口、有提示条、有无形的 1x1 窗口。
    取面积最大的，等于挑主窗口；提示条、透明窗口自然落选。
    """
    global _APP_HIT, _APP_MATCHES, _APP_HUNG
    hits = _app_list(_APP_SPEC)
    _APP_MATCHES = len(hits)
    _APP_HUNG = len([h for h in hits if h.get("hung")])
    # 无响应的跳过：PrintWindow 是同步发消息，对方不回就挂死整次抓屏。
    usable = [h for h in hits
              if not h["iconic"] and not h.get("hung")
              and h["w"] >= 160 and h["h"] >= 120]
    if not usable:
        _APP_HIT = None
        return None
    usable.sort(key=lambda h: h["w"] * h["h"], reverse=True)
    _APP_HIT = usable[0]
    return _APP_HIT


def _print_window(hwnd):
    """把一个窗口连内容一起画到内存里，返回（PIL 图，原点 (l, t)）；拿不到返回 None。

    和 ImageGrab 的区别：ImageGrab 抓屏幕上那块像素，窗口被别的窗口挡住时，
    抓回来的就是挡住它的那个人。PrintWindow 让窗口自己画到内存 DC 上，
    所以后台、被遮挡都能拿到真画面——实测被 Chrome 盖住的启动器、后台的
    rust 版 MC（OpenGL）都能抓全。
    最小化的拿不到：Windows 对最小化窗口只给任务栏那么大一个框。
    """
    global _PRINT_BLANK
    _PRINT_BLANK = ""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32
        hwnd_int = int(hwnd)
        hwnd = wintypes.HWND(hwnd_int)
        if u32.IsIconic(hwnd):
            return None
        # 无响应的窗口不能碰：PrintWindow 会一直等它回消息。
        if u32.IsHungAppWindow(hwnd):
            return None
        rect = wintypes.RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        s = _dpi_scale(hwnd_int)
        W = int(round((rect.right - rect.left) * s))
        H = int(round((rect.bottom - rect.top) * s))
        if W < 160 or H < 120:
            return None

        class _BMIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        hdc = u32.GetWindowDC(hwnd)
        if not hdc:
            return None
        mdc = g32.CreateCompatibleDC(hdc)
        bmp = g32.CreateCompatibleBitmap(hdc, W, H)
        old = g32.SelectObject(mdc, bmp)
        try:
            if not u32.PrintWindow(hwnd, mdc, 2):  # PW_RENDERFULLCONTENT
                return None
            bi = _BMIH()
            bi.biSize = ctypes.sizeof(_BMIH)
            bi.biWidth = W
            bi.biHeight = -H  # 负数 = 从上到下，省一次翻转
            bi.biPlanes = 1
            bi.biBitCount = 32
            buf = ctypes.create_string_buffer(W * H * 4)
            if g32.GetDIBits(mdc, bmp, 0, H, buf, ctypes.byref(bi), 0) != H:
                return None
            img = Image.frombytes("RGB", (W, H), buf.raw[: W * H * 4], "raw", "BGRX")
            # 有些窗口 PrintWindow 报成功，harness 画出来却是一片单色（全黑/全白）。
            # 当成抓到就会把一张空图送去识图，看着像「屏幕是黑的」。缩到 64×36 抽一下，差太小就退回。
            probe = np.asarray(img.convert("RGB").resize((64, 36), Image.BILINEAR), dtype=np.int16)
            if int(probe.max()) - int(probe.min()) < 8:
                _PRINT_BLANK = "抓到的画面是一片单色（%s），这个窗口自己画不出内容" % (
                    "全黑" if int(probe.max()) < 24 else "全是同一个颜色")
                return None
            return img, (int(round(rect.left * s)), int(round(rect.top * s)))
        finally:
            try:
                g32.SelectObject(mdc, old)
                g32.DeleteObject(bmp)
                g32.DeleteDC(mdc)
                u32.ReleaseDC(hwnd, hdc)
            except Exception:
                pass
    except Exception:
        return None



def _window_rect():
    """前台窗口的物理像素矩形 (l, t, r, b)；拿不到就 None。

    和 _foreground_box 的区别：那个要判「占屏多少」，超出区间就当没抓住；
    这个不做区间判断——用户明确说了只看这个窗口，全屏窗口也该照抓。
    最小化的窗口抓出来是空的，返回 None，由调用方退回整屏。
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        if user32.IsIconic(wintypes.HWND(hwnd)):
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        _s = _dpi_scale(hwnd)
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        if _s != 1.0:
            left, top, right, bottom = (int(round(left * _s)), int(round(top * _s)),
                                        int(round(right * _s)), int(round(bottom * _s)))
        if right - left < 160 or bottom - top < 120:
            return None
        return (left, top, right, bottom)
    except Exception:
        return None


def _window_box(screen_size):
    """窗口矩形夹到屏内，给出抓屏用的 bbox；夹完太小就当没抓到。"""
    rect = _window_rect()
    if not rect:
        return None
    W, H = screen_size
    left = max(0, min(W, rect[0]))
    top = max(0, min(H, rect[1]))
    right = max(0, min(W, rect[2]))
    bottom = max(0, min(H, rect[3]))
    if right - left < 160 or bottom - top < 120:
        return None
    return (left, top, right, bottom)


def _foreground_box(screen_size):
    """取当前前台窗口在屏幕上的比例框；拿不到、或占屏超出 FG 区间，返回 None。

    热点图挑的是「哪块动得多」。终端在刷输出、视频同时在放的时候，它可能挑中
    你不看的那块——所以 auto 先问前台窗口：带焦点的那个才是眼睛对着的地方。
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        # GetWindowRect 为主：实测它给的是真窗口框。
        # DWM 那条（DWMWA_EXTENDED_FRAME_BOUNDS=9）在某些驱动上会吐整屏尺寸，
        # 拿它当首选会把「全屏」误判成「整屏在动」而永远不裁，所以只拿它做一次校验。
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        # 进程没声明 DPI 感知时，GetWindowRect 给的是「逻辑像素」，
        # 而 ImageGrab 抓回来的是物理像素。150% 缩放下两者差 1.5 倍，
        # 直接拿去比占屏比会把全屏窗口算成 43%，于是每帧都裁错。
        _s = _dpi_scale(hwnd)
        try:
            dwm = wintypes.RECT()
            if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                    wintypes.HWND(hwnd), 9, ctypes.byref(dwm), ctypes.sizeof(dwm)) == 0:
                dw = dwm.right - dwm.left
                dh = dwm.bottom - dwm.top
                W0, H0 = screen_size
                # 只有 DWM 给的框更小、又不像整屏，才采用（去掉阴影那几像素）
                if 160 <= dw < W0 and 120 <= dh < H0:
                    rect = dwm
        except Exception:
            pass

        if _s != 1.0:
            rect = wintypes.RECT(
                int(round(rect.left * _s)), int(round(rect.top * _s)),
                int(round(rect.right * _s)), int(round(rect.bottom * _s)),
            )

        W, H = screen_size
        left = max(0, min(W, int(rect.left)))
        top = max(0, min(H, int(rect.top)))
        right = max(0, min(W, int(rect.right)))
        bottom = max(0, min(H, int(rect.bottom)))
        if right - left < 160 or bottom - top < 120:
            return None

        area = ((right - left) * (bottom - top)) / float(W * H)
        if area < FG_MIN or area > FG_MAX:
            return None
        return (left / float(W), top / float(H), right / float(W), bottom / float(H), area)
    except Exception:
        return None



def _inner_box(outer, inner, min_area: float = 0.06):
    """前台窗口与热点区取交集，交集太小就 None。

    实测热点框常跨出窗口——窗口在放视频、它下面的终端同时在刷输出，两个热点连成
    一个更大的外接框，包含式判断永远不成立，只能框住整个窗口连标签栏一起送。
    交集既保住「只看前台」，又贴到真正在动的那块。
    """
    if not outer or not inner:
        return None
    x0 = max(outer[0], inner[0])
    y0 = max(outer[1], inner[1])
    x1 = min(outer[2], inner[2])
    y1 = min(outer[3], inner[3])
    if x1 <= x0 or y1 <= y0:
        return None
    area = (x1 - x0) * (y1 - y0)
    if area < min_area:
        return None
    return (x0, y0, x1, y1, area)

def _focus_box(hot: np.ndarray, lo: float = FOCUS_MIN, hi: float = FOCUS_MAX, peak_min: float = FOCUS_PEAK):
    """从累计热点图里挑出「一直在变的那块」，返回比例框；挑不出就 None。

    整屏差异把两件事混成一个分数：页面跳一下满屏都换，分数爆表；视频真在放，
    只有播放器那一块在动，一平均就剩个位数。所以不再看总分，改看「哪块热」。
    """
    if hot.size == 0:
        return None
    peak = float(hot.max())
    if peak < peak_min:
        return None
    ys, xs = np.where(hot >= max(FOCUS_PEAK, peak * 0.25))
    if ys.size == 0:
        return None
    h, w = hot.shape
    x0, x1 = float(xs.min()) / w, float(xs.max() + 1) / w
    y0, y1 = float(ys.min()) / h, float(ys.max() + 1) / h
    area = (x1 - x0) * (y1 - y0)
    if area < lo or area > hi:
        return None
    return (x0, y0, x1, y1, area)


def _cut_focus(img: Image.Image, box):
    """按比例框从原图裁内容区。裁完太小就当没裁——那点像素放大也没东西。"""
    W, H = img.size
    x0, y0, x1, y1 = box[:4]
    left = max(0, int(x0 * W) - CROP_PAD)
    top = max(0, int(y0 * H) - CROP_PAD)
    right = min(W, int(x1 * W) + CROP_PAD)
    bottom = min(H, int(y1 * H) + CROP_PAD)
    if right - left < 160 or bottom - top < 120:
        return None
    return np.asarray(img.convert("RGB").crop((left, top, right, bottom)), dtype=np.float32)


def _grab_run(seconds: float, max_frames: int, focus: str = "whole",
              cut_abs: float = None, cut_peak: float = None, need: float = None):
    """抓一段，按判据留帧。返回（抓了多少帧，留下了哪些，最后那帧的原图）。

    一帧都没留下时，列表是空的，由调用方决定要不要复用上一张。
    原图另给一份——文字层要拿它去 OCR，缩过的小图糊得认不出字。

    focus=True 时多一步：拿整段的累计热点找内容区，每帧按同一块裁出来再装包，
    标签栏、地址栏、侧边推荐都不进图。第五项是这步的说明，没做就是 None。
    """
    frames: list[tuple[np.ndarray, float]] = []
    kept_raw: list[Image.Image] = []
    hot = np.zeros((GRID[1], GRID[0]), dtype=np.float32)
    first, prev_small = _shot()
    last_raw = first
    prev_raw = first
    diff_pair = None
    prev_score = 1.0
    integ = 0.0
    last_cut = 0.0
    t0 = time.time()
    shots = 0
    next_at = t0

    # 认内容时不跟整屏突变走，跟「一直在动」走：门槛压到 2.5 分、积分攒够 24
    # 就留一帧，视频那种缓慢流动才留得住。静止画面里顶多被光标和时钟勾一两帧，
    # 有 max_frames 兜着，不至于满盘。
    tracking = focus in ("content", "auto")
    # 三个阈值都可由配置覆盖；没给就按原样——认内容时压门槛，整屏时按突变判。
    cut_abs = cut_abs if cut_abs is not None else (FOCUS_CUT_ABS if tracking else CUT_ABS)
    cut_peak = cut_peak if cut_peak is not None else (FOCUS_SPIKE if tracking else CUT_PEAK)
    need = need if need is not None else (FOCUS_NEED if tracking else NEED)

    while time.time() - t0 < seconds and len(frames) < max_frames:
        # 按节拍走，而不是「睡一觉再干活」——后者会把每帧的耗时也加进周期，
        # 目标 6 帧/秒实际只跑出 4.4 帧/秒。
        next_at += 1.0 / FPS
        time.sleep(max(0.0, next_at - time.time()))
        img, cur_small = _shot()
        last_raw = img
        s = _score(prev_small, cur_small)
        shots += 1
        now = time.time()

        if tracking:
            # 每帧顺手把差异摊到热点图上，抓完才知道「哪块一直在动」。
            hot += np.abs(cur_small - prev_small).mean(axis=2)

        if (s >= cut_abs and s > cut_peak * max(1.0, prev_score)
                and now - last_cut >= REFRACT):
            frames.append((_big(img), s))
            if tracking:
                kept_raw.append(img)
            last_cut = now
            integ = 0.0
            diff_pair = (prev_raw, img)
        else:
            integ += s
            if integ >= need:
                frames.append((_big(img), s))
                if focus:
                    kept_raw.append(img)
                integ = 0.0
                diff_pair = (prev_raw, img)

        prev_small, prev_score = cur_small, s
        prev_raw = img

    note = None
    if focus in ("content", "auto") and frames and len(kept_raw) == len(frames):
        if focus == "auto":
            # 前台窗口优先：它带焦点，代表眼睛真正对着的那块。
            # 但它连标签栏一起框进来，所以里面若有更贴画面的变化区，就取那块。
            size = kept_raw[0].size
            fg = _foreground_box(size)
            hm = _focus_box(hot, AUTO_MIN, AUTO_MAX, FOCUS_PEAK)
            inner = _inner_box(fg, hm) if fg else None
            if inner:
                box, src_of_box = inner, "前台窗口内的变化区"
            elif fg:
                box, src_of_box = fg, "前台窗口"
            else:
                box, src_of_box = hm, "热点区"
        else:
            box, src_of_box = _focus_box(hot), "热点区"
        cut = [(_cut_focus(raw, box) if box else None, s)
               for (_arr, s), raw in zip(frames, kept_raw)]
        if box and all(c[0] is not None for c in cut):
            frames = [(c[0], c[1]) for c in cut]
            note = "只送内容区（%s 占屏 %d%%），外壳裁掉" % (src_of_box, round(box[4] * 100))
        elif focus == "content":
            note = "没挑出单一内容区，照原样整幅"
        else:
            note = ("前台窗口没拿到或占屏超出 8%-92%，"
                    "热点区也不在 8%-60% 之间（太小是噪点、太大是整屏在动），照整屏送")

    return shots, frames, last_raw, diff_pair, note


def _despace(text: str) -> str:
    """中文被 OCR 一格一格拆开了，这里把字缝捏回去，别的一律不动。"""
    import re as _re
    prev = None
    while prev != text:
        prev = text
        text = _re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def _near(a: str, b: str) -> bool:
    """两条名字像不像同一个东西。给成品包去重用——OCR 那头的错字不该占两行。

    控件名常常是几段字连起来报的（"聊天 输入框 发送"），OCR 报的是其中一段。
    所以不能只比整串：拿 OCR 那段的长短，在控件名上滑一遍，取最像的一窗。
    """
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    if len(a) > len(b):
        a, b = b, a
    if len(a) < 4:
        return False
    import difflib

    best = 0.0
    win = len(a)
    for i in range(0, len(b) - win + 1):
        r = difflib.SequenceMatcher(None, a, b[i:i + win]).ratio()
        if r > best:
            best = r
            if best >= 0.86:
                return True
    return best >= 0.86


def _text_layer(png: str, timeout: float = 8.0) -> dict:
    """把屏幕编译成文字与坐标，摆在模型面前当文本读。

    两条腿都借自 Windows 自己：UI Automation 报控件树，Media.Ocr 抠字。
    比起把图递过去让它猜，这一步只是把「猜」换成「问」。
    """
    import subprocess

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pack_screen.ps1")
    if not os.path.exists(script):
        return {"ok": False, "error": "pack_screen.ps1 不在"}
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Path", png, "-MaxElements", "40", "-MaxLines", "80"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    raw = (proc.stdout or "").strip()
    if not raw:
        return {"ok": False, "error": "powershell 没回话"}
    try:
        data = json.loads(raw.splitlines()[-1])
    except Exception:
        return {"ok": False, "error": "回话不是 JSON"}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("ocr_error") or "打包失败"}

    elements = []
    for item in data.get("elements") or []:
        name = str(item.get("n") or "").strip()
        if not name:
            continue
        elements.append("%s[%s] %s" % (name, item.get("t", ""),
                                       "%d,%d" % (item.get("x", 0), item.get("y", 0))))

    lines, seen = [], set()
    ocr_rows = []
    for item in data.get("lines") or []:
        raw_text = str(item.get("t") or "")
        text = _despace(raw_text).strip()
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        lines.append(text)
        ocr_rows.append({
            "k": "字",
            "t": text,
            "x": int(item.get("x") or 0),
            "y": int(item.get("y") or 0),
        })

    text = "\n".join(lines)
    if len(text) > 1400:
        text = text[:1400] + "…（后面截了）"

    # 成品包：控件和字本来分两条腿，模型拿到手还得自己按坐标拼成「屏幕上是什么」。
    # 这里先拼好——同一处、同一个名字的只留一份（控件名通常比 OCR 认出来的干净），
    # 再按从上到下、从左到右排一遍，模型照顺序读就是眼睛的顺序。
    blocks = []
    ctrl_names = set()
    for item in data.get("elements") or []:
        if len(blocks) >= _BLOCK_CTRL_CAP:
            break
        name = str(item.get("n") or "").strip()
        if not name:
            continue
        ctrl_names.add(_despace(name).strip())
        blocks.append({
            "k": "控件",
            "t": name,
            "r": str(item.get("t") or ""),
            "x": int(item.get("x") or 0),
            "y": int(item.get("y") or 0),
        })
    for row in ocr_rows:
        if len(blocks) >= _BLOCK_CAP:
            break
        if row["t"] in ctrl_names:
            continue
        # OCR 认字常把 I 认成 l、把 U 认成 Ul；控件名是程序自己报的，准得多。
        # 两成半的字不一样就当成同一处，别再让同一样东西在包里出现两回。
        if any(_near(row["t"], name) for name in ctrl_names):
            continue
        blocks.append(row)
    blocks.sort(key=lambda b: (b["y"] // 28, b["x"]))

    return {
        "ok": True,
        "window": str(data.get("window") or "").strip(),
        "elements": elements[:40],
        "blocks": blocks[:_BLOCK_CAP],
        "block_count": len(blocks[:_BLOCK_CAP]),
        "text": text,
        "chars": len(text),
        "ms": int(data.get("elapsed_ms") or 0),
    }


# ---------------- 边车入口：把工具那段搬成命令行 ----------------

def _last_path(shot_dir: str) -> str:
    return os.path.join(shot_dir, "last.json")


def _load_last(shot_dir: str) -> dict:
    """取上一张图的下落。没有就返回空字典，不抛异常。"""
    try:
        with open(_last_path(shot_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_last(shot_dir: str, payload: dict) -> None:
    """记下这一张：路径、描述、时刻。屏幕没动时靠它把上一张原样递回去。"""
    record = {
        "path": payload.get("path", ""),
        "look": payload.get("look", ""),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kept_frames": payload.get("kept_frames", 0),
        "text": payload.get("text", {}),
    }
    path = _last_path(shot_dir)
    try:
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("[see_now] 快照记不住：%s: %s", type(exc).__name__, exc)


def _sweep_old_shots(shot_dir: str, seconds: float = 0) -> None:
    """写入新图之前先扫一遍，把超过几秒的老截图清掉。只删 .png。

    seconds <= 0 表示不长期留：本次这张图照样出，但旧的全清掉，
    下次抓屏时本次这张也会被清（隐私上是「只留最近一张」）。
    """
    if seconds is None or seconds <= 0:
        cutoff = time.time()
    else:
        cutoff = time.time() - seconds
    try:
        names = os.listdir(shot_dir)
    except OSError:
        return
    for name in names:
        if not name.lower().endswith(".png"):
            continue
        fp = os.path.join(shot_dir, name)
        try:
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
        except OSError:
            pass


def _pool_dir_of(shot_dir: str, cfg: dict) -> str:
    want = str(cfg.get("pool_dir") or "").strip()
    return want or os.path.join(shot_dir, "pool")


def _sweep_pool(pdir: str, ttl: float, max_files: int, max_mb: float) -> None:
    """缓存池清理：先按时间清，再按张数、体积从最旧的开始删。"""
    try:
        names = [n for n in os.listdir(pdir) if n.lower().endswith(".png")]
    except OSError:
        return
    items = []
    now = time.time()
    for n in names:
        fp = os.path.join(pdir, n)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if ttl > 0 and now - st.st_mtime > ttl:
            try:
                os.remove(fp)
            except OSError:
                pass
            continue
        items.append((st.st_mtime, st.st_size, fp))
    items.sort()
    if max_files > 0:
        while len(items) > max_files:
            _m, _s, fp = items.pop(0)
            try:
                os.remove(fp)
            except OSError:
                pass
    if max_mb > 0:
        budget = max_mb * 1024 * 1024
        total = sum(s for _m, s, _fp in items)
        while items and total > budget:
            _m, s, fp = items.pop(0)
            try:
                os.remove(fp)
                total -= s
            except OSError:
                pass


def _dump_pool(frames: list, shot_dir: str, cfg: dict, span: float) -> list:
    """把这次留下的帧逐张按时间戳落进缓存池，返回清单。

    ttl <= 0 表示不落盘（阅后即焚）：这一轮只交出拼好的那张，
    原始帧一张都不留。ttl > 0 才写，并且写完再清一遍（TTL + 张数 + 体积）。
    """
    try:
        ttl = float(cfg.get("pool_ttl") or 0)
    except (TypeError, ValueError):
        ttl = 0.0
    if ttl <= 0 or not frames:
        return []
    pdir = _pool_dir_of(shot_dir, cfg)
    try:
        os.makedirs(pdir, exist_ok=True)
    except OSError as exc:
        logger.warning("[see_now] 缓存池建不出来：%s: %s", type(exc).__name__, exc)
        return []
    try:
        max_files = int(cfg.get("pool_max_files") or 200)
    except (TypeError, ValueError):
        max_files = 200
    try:
        max_mb = float(cfg.get("pool_max_mb") or 300)
    except (TypeError, ValueError):
        max_mb = 300.0
    _sweep_pool(pdir, ttl, max_files, max_mb)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = []
    n = len(frames)
    for i, item in enumerate(frames):
        arr, score = item[0], item[1]
        fp = os.path.join(pdir, "%s_f%02d.png" % (stamp, i + 1))
        try:
            im = Image.fromarray(np.ascontiguousarray(arr.astype(np.uint8)))
            im.save(fp)
        except Exception as exc:
            logger.warning("[see_now] 这一帧没存进池：%s: %s", type(exc).__name__, exc)
            continue
        out.append({
            "i": i + 1,
            "file": fp,
            "t": round(span * (i / float(n - 1)) if n > 1 else 0.0, 1),
            "score": round(float(score), 2),
        })
    _sweep_pool(pdir, ttl, max_files, max_mb)
    # 刚写的这张也可能被上限挤掉（池设得太小）：清单只报还在的，免得回执给出不存在的路径。
    return [o for o in out if os.path.exists(o["file"])]


def run_once(cfg: dict) -> dict:
    """抓一段 → 留帧 → 装箱 / 裁变化区 → 可选认字 → 返回一份 JSON 用的字典。"""
    global _TARGET, _DIFF_LEVEL, _WIN_FALLBACK, _WIN_SCREEN
    global _APP_SPEC, _APP_FALLBACK, _APP_HIT, _APP_MATCHES, _APP_HUNG
    picked = _set_monitors(str(cfg.get("monitors") or "").strip())
    _TARGET = str(cfg.get("target") or "screen").strip().lower()
    if _TARGET not in ("screen", "window", "app"):
        _TARGET = "screen"
    _APP_SPEC = str(cfg.get("app") or "").strip().lower()
    _APP_FALLBACK = False
    _APP_HIT = None
    _APP_MATCHES = 0
    _APP_HUNG = 0
    _WIN_FALLBACK = False
    _WIN_SCREEN = None
    if _TARGET == "window":
        mons = _ALL_MONS or _monitors()
        if mons:
            _WIN_SCREEN = (max(m["right"] for m in mons) - min(m["left"] for m in mons),
                           max(m["bottom"] for m in mons) - min(m["top"] for m in mons))
    try:
        _DIFF_LEVEL = float(cfg.get("diff_level"))
    except (TypeError, ValueError):
        _DIFF_LEVEL = DIFF_LEVEL
    _DIFF_LEVEL = max(1.0, min(80.0, _DIFF_LEVEL))
    def _num(name, lo, hi, fallback=None):
        try:
            v = float(cfg.get(name))
        except (TypeError, ValueError):
            return fallback
        return max(lo, min(hi, v))
    cut_abs_arg = _num("cut_abs", 1.0, 255.0)
    need_arg = _num("need", 1.0, 2000.0)
    span = max(2.0, min(60.0, float(cfg.get("seconds") or 10.0)))
    cap = max(1, min(12, int(cfg.get("max_frames") or 6)))
    tier_lock = int(cfg.get("tier_lock") or 0)
    tier_style = str(cfg.get("tier_style") or "native")
    if tier_style not in ("native", "up", "down"):
        tier_style = "native"
    focus = str(cfg.get("focus") or "auto")
    if focus not in ("whole", "content", "auto"):
        focus = "auto"
    max_width = int(cfg.get("max_width") or 1600)
    max_width = max(400, min(4096, max_width))
    try:
        crop_min_pct = float(cfg.get("crop_min_side_pct"))
    except (TypeError, ValueError):
        crop_min_pct = CROP_MIN_SIDE * 100.0
    crop_min_pct = max(0.0, min(90.0, crop_min_pct))
    try:
        crop_max_h = int(float(cfg.get("crop_max_h")))
    except (TypeError, ValueError):
        crop_max_h = CROP_MAX_H
    crop_max_h = max(120, min(4000, crop_max_h))
    shot_dir = str(cfg.get("shot_dir") or "")
    if not shot_dir:
        shot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
    os.makedirs(shot_dir, exist_ok=True)

    # 锁档时抓多了也装不下，先按高度预算砍掉，省得白抓、白算一遍分。
    cap = min(cap, _frame_limit(tier_lock))

    t_start = time.time()
    shots, frames, last_raw, diff_pair, focus_note = _grab_run(
        span, cap, focus, cut_abs=cut_abs_arg, need=need_arg)

    # 整段一帧都没留下，说明屏幕基本没动。
    # 这里原来把上一张原样递回去：省一次识图，但交出去的图带着上一次的时间戳，
    # 问「我现在在干嘛」会拿到几小时前的画面。宁可多一张图，也要是现在的。
    static_run = False
    if not frames:
        img, _ = _shot()
        frames = [(_big(img), CUT_ABS)]
        static_run = True

    name = "see_now_" + time.strftime("%Y%m%d-%H%M%S") + ".png"
    path = os.path.join(shot_dir, name)
    keep_seconds = cfg.get("keep_seconds")
    try:
        keep_seconds = 0.0 if keep_seconds is None else float(keep_seconds)
    except (TypeError, ValueError):
        keep_seconds = 0.0
    _sweep_old_shots(shot_dir, keep_seconds)

    # 只留下一帧时，先问一句「变的是哪块」。变化集中，就只把那块裁出来——
    # 模型那头会把整屏压到同一个 token 栅格，裁一小块反而换回一倍细节。
    crop = None
    crop_box = None
    if len(frames) == 1 and diff_pair and focus == "whole":
        crop_box = _changed_box(diff_pair[0], diff_pair[1])
        crop = _crop_for_model(diff_pair[1], crop_box, crop_min_pct / 100.0)

    plan = None
    if crop is not None:
        crop = _fit_crop(crop, max_width, crop_max_h)
        crop.save(path)
        crop_note = "这张只裁了变化的区域（%d×%d），细节比整屏清楚。" % crop.size
    else:
        plan = _pack(frames, path, tier_style, tier_lock, max_width)
        crop_note = ""

    payload = {
        "status": "ok",
        "path": path,
        "shot_frames": shots,
        "kept_frames": len(frames),
        "seconds": round(span, 1),
        "elapsed_ms": int((time.time() - t_start) * 1000),
        "reused": False,
        "look": "",
        "target": _TARGET,
        "monitors": {
            "picked": [m["n"] for m in _MON_RECTS],
            "count": len(_MON_RECTS) or 1,
            "all": [{"n": m["n"], "primary": m["primary"],
                     "rect": [m["left"], m["top"], m["right"], m["bottom"]]}
                    for m in _ALL_MONS],
            "origin": list(_SHOT_ORIGIN),
        },
        "note": ("抓了 %d 帧，只留 %d 帧（画面变化才留）。%s%s"
                 "识图不在这里做：图在 path，接着交给 WhaleLife 那头的视觉模型。"
                 % (shots, len(frames),
                    (focus_note + "。") if focus_note else "",
                    crop_note)),
    }
    if static_run:
        payload["static"] = True
        payload["note"] = ("这段时间画面几乎没动，这是刚刚（%s）抓的%s，不是上一张旧图。"
                           % (time.strftime("%H:%M:%S"),
                              "窗口那一块"
                              if _TARGET == "app" and not _APP_FALLBACK
                              else "前台窗口那一块"
                              if _TARGET == "window" and not _WIN_FALLBACK
                              else "一整屏"))

    if _TARGET == "app":
        if not _APP_FALLBACK and _APP_HIT:
            payload["note"] += (" 这次只抓窗口《%s》（进程 %s，%d×%d）那一块，"
                                "不在前台、被别的窗口挡着也照样抓得到。"
                                % (_APP_HIT["title"] or "(无标题)", _APP_HIT["proc"],
                                   _APP_HIT["w"], _APP_HIT["h"]))
        else:
            if _PRINT_BLANK:
                _why = _PRINT_BLANK
            elif _APP_MATCHES == 0:
                _why = "没找到这个窗口"
            elif _APP_HUNG >= _APP_MATCHES:
                _why = "匹配到 %d 个，全都没响应（可能卡住了）" % _APP_MATCHES
            elif _APP_HUNG > 0:
                _why = ("匹配到 %d 个，其中 %d 个没响应、其余最小化了"
                        % (_APP_MATCHES, _APP_HUNG))
            else:
                _why = "匹配到 %d 个，但都抓不到（最小化了，或者尺寸太小）" % _APP_MATCHES
            payload["note"] += (" 想抓窗口「%s」但没抓到（%s），这次退回整屏。"
                                % (_APP_SPEC or "没填匹配串", _why))
    if _TARGET == "window":
        payload["note"] += (" 这次只抓前台窗口那一块（不是整屏）。"
                            if not _WIN_FALLBACK else
                            " 想只抓前台窗口，但没拿到窗口框（可能最小化了），这次退回整屏。")
    if len(_MON_RECTS) > 1:
        payload["note"] += (" 这次抓了 %d 块屏（第 %s 块），没选中的那块涂了深灰，"
                            "保留相对位置好让坐标对得回去。"
                            % (len(_MON_RECTS),
                               "、".join(str(m["n"]) for m in _MON_RECTS)))
    if crop is None and len(frames) > 1:
        payload["note"] += (" 拼图里每帧左上角标了 +秒数 和 x缩放"
                            "（图上一像素等于屏幕多少个像素），按这个能分出先后。")
        if _SHOT_ORIGIN[0] or _SHOT_ORIGIN[1]:
            payload["note"] += ("这轮抓的是第 %s 块屏，这块屏的原点在虚拟桌面 %d,%d。"
                                % ("、".join(str(m["n"]) for m in _MON_RECTS),
                                   _SHOT_ORIGIN[0], _SHOT_ORIGIN[1]))
        else:
            payload["note"] += "每帧都是整屏，屏幕左上角就是 0,0。"

    tier_line = _tier_note(plan, tier_style, tier_lock)
    if tier_line:
        payload["tier"] = tier_line

    if cfg.get("text_layer"):
        # 认字只认「变的那块」：整屏六十行里大半是没动的字。装箱那张缩到三分之一，
        # 字早糊了，所以只借裁切图、不借装箱图。
        src_png = os.path.join(shot_dir, "_ocr_src.png")
        ocr_src = last_raw
        origin = (0, 0)
        if crop is not None and crop_box is not None:
            ocr_src = crop
            origin = (int(crop_box[0]), int(crop_box[1]))
        try:
            ocr_src.save(src_png)
            pack = _text_layer(src_png)
        finally:
            try:
                os.remove(src_png)
            except OSError:
                pass
        if isinstance(pack, dict) and pack.get("ok") and origin != (0, 0):
            # 坐标换回整屏，不然模型拿着裁剪图里的坐标去点，全偏到左上角。
            for line in pack.get("lines") or []:
                if isinstance(line, dict):
                    line["x"] = int(line.get("x") or 0) + origin[0]
                    line["y"] = int(line.get("y") or 0) + origin[1]
            pack["crop_origin"] = {"x": origin[0], "y": origin[1]}
        if isinstance(pack, dict) and pack.get("ok"):
            payload["text"] = pack
            payload["note"] += (" 另外 text.blocks 已经拼好：控件和字合成一份，"
                                "按屏幕从上到下、从左到右排过序，x/y 是整屏坐标。")

    # 坐标网格：叠在成品图上，标的是这张图自己的像素坐标。
    grid_raw = str(cfg.get("grid") or "").strip()
    grid_step = 0
    if grid_raw in ("", "0", "off", "no", "false"):
        grid_step = 0
    elif grid_raw == "auto":
        grid_step = -1
    else:
        try:
            grid_step = int(grid_raw)
        except ValueError:
            grid_step = -1
    if grid_step and len(frames) > 1:
        payload["note"] += " 这轮是多帧拼图，没叠坐标网格（拼图坐标对不回屏幕）。"
        grid_step = 0
    if grid_step:
        origin, scale = (0, 0), (1.0, 1.0)
        try:
            with Image.open(path) as base:
                w, h = base.size
                if _RAW_SIZE[0] and w:
                    if crop is not None and crop_box is not None:
                        # 裁过：图上 (0,0) 对应屏幕 crop_box 左上角。
                        bx, by, bw, bh = (float(v) for v in crop_box[:4])
                        origin = (int(round(bx)) + _SHOT_ORIGIN[0],
                                  int(round(by)) + _SHOT_ORIGIN[1])
                        scale = (bw / float(w), bh / float(h))
                    elif plan and plan.get("size"):
                        gw = 6  # _pack 里每格留的间隔
                        sc = (_RAW_SIZE[0] / float(max(1, w - gw)),
                              _RAW_SIZE[1] / float(max(1, h - 2 * gw)))
                        scale = sc
                        # 图上 (gw,gw) 对应整幅的左上角，也就是 _SHOT_ORIGIN。
                        origin = (_SHOT_ORIGIN[0] - gw * sc[0],
                                  _SHOT_ORIGIN[1] - gw * sc[1])
                framed, used = _grid(base, 0 if grid_step < 0 else grid_step, origin, scale)
            framed.save(path)
            payload["grid"] = {"step": used, "size": list(framed.size)}
            payload["note"] += (" 图上叠了坐标网格：边上的数字是屏幕像素坐标"
                                "（原屏 %dx%d），可以直接拿来说位置或做裁剪。"
                                % (_RAW_SIZE[0], _RAW_SIZE[1]) if _RAW_SIZE[0]
                                else " 图上叠了坐标网格：边上的数字是屏幕像素坐标。")
        except Exception as exc:
            payload["note"] += " 网格没叠成：%s: %s" % (type(exc).__name__, exc)

    pool = _dump_pool(frames, shot_dir, cfg, span)
    payload["pool"] = pool

    _save_last(shot_dir, payload)
    logger.info("[see_now] %s 抓 %d 留 %d", name, shots, len(frames))
    return payload


def main(argv=None) -> int:
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="see_now_sidecar", add_help=True)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--max-frames", type=int, default=6)
    ap.add_argument("--tier-style", default="native", choices=["native", "up", "down"])
    ap.add_argument("--tier-lock", type=int, default=0)
    ap.add_argument("--focus", default="auto", choices=["whole", "content", "auto"])
    ap.add_argument("--crop-min-side-pct", type=float,
                    default=CROP_MIN_SIDE * 100.0)
    ap.add_argument("--crop-max-h", type=int, default=CROP_MAX_H)
    ap.add_argument("--max-width", type=int, default=1600)
    ap.add_argument("--shot-dir", default="")
    ap.add_argument("--text-layer", action="store_true")
    ap.add_argument("--grid", default="0", help="坐标网格：0 关，auto 自动，或给像素步长")
    ap.add_argument("--target", default="screen", choices=["screen", "window", "app"],
                    help="screen 抓整屏 / window 只抓前台窗口那一块")
    ap.add_argument("--app", default="",
                    help="target=app 时按进程名或标题片段找窗口，不分大小写")
    ap.add_argument("--diff-level", type=float, default=DIFF_LEVEL,
                    help="灰度差多少算变了，1-80，默认 10")
    ap.add_argument("--cut-abs", type=float, default=None,
                    help="单帧突变门槛，不给按默认（整屏 12 / 认内容 2.5）")
    ap.add_argument("--need", type=float, default=None,
                    help="累计积分门槛，不给按默认（整屏 144 / 认内容 24）")
    ap.add_argument("--monitors", default="primary",
                    help="抓哪些显示器：primary 主屏 / all 全部 / 编号逗号分隔如 1,3")
    ap.add_argument("--keep-seconds", type=float, default=0.0,
                    help="截图在磁盘上留几秒，0 表示只留本次这张（下次抓屏时清掉）")
    ap.add_argument("--pool-dir", default="", help="帧缓存池目录，留空则 shots/pool")
    ap.add_argument("--pool-ttl", type=float, default=0.0,
                    help="缓存池里的帧留多少秒，0 = 阅后即焚（不落盘）")
    ap.add_argument("--pool-max-files", type=int, default=200)
    ap.add_argument("--pool-max-mb", type=float, default=300.0)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--list-monitors", action="store_true",
                    help="只列出显示器编号和矩形，不抓屏")
    a = ap.parse_args(argv)

    shot_dir = a.shot_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")

    if a.list:
        print(json.dumps({
            "ok": True,
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "pil": getattr(__import__("PIL"), "__version__", "?"),
            "shot_dir": shot_dir,
            "grab": True,
        }, ensure_ascii=False))
        return 0

    cfg = {
        "seconds": a.seconds,
        "max_frames": a.max_frames,
        "tier_style": a.tier_style,
        "tier_lock": a.tier_lock,
        "focus": a.focus,
        "crop_min_side_pct": a.crop_min_side_pct,
        "crop_max_h": a.crop_max_h,
        "max_width": a.max_width,
        "shot_dir": shot_dir,
        "text_layer": a.text_layer,
        "grid": a.grid,
        "keep_seconds": a.keep_seconds,
        "pool_dir": a.pool_dir,
        "pool_ttl": a.pool_ttl,
        "pool_max_files": a.pool_max_files,
        "pool_max_mb": a.pool_max_mb,
        "monitors": a.monitors,
        "target": a.target,
        "app": a.app,
        "diff_level": a.diff_level,
        "cut_abs": a.cut_abs,
        "need": a.need,

    }
    try:
        if getattr(a, "list_monitors", False):
            mons = _monitors()
            out = {"status": "ok", "monitors": [
                {"n": m["n"], "primary": m["primary"],
                 "rect": [m["left"], m["top"], m["right"], m["bottom"]],
                 "size": [m["w"], m["h"]]} for m in mons]}
            print(json.dumps(out, ensure_ascii=False))
            return 0
        payload = run_once(cfg)
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"status": "error",
                          "message": "抓屏失败：%s: %s" % (type(exc).__name__, exc)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
