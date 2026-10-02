#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
厦门短临预警哨兵 v3
================================================================
职责：定时拉 NMC 华东雷达拼图 → 对每个监测点按《厦门短临预警台》五档判据定级
     → 触发 / 升级 / 持续 / 降级 / 解除 时推送通知。

与 v2 的差别（2026-10-01 重写）：
  1. 【分档对齐页面】v2 是纯距离三档（较高／警戒／关注，只看距离），认不出
     「较高／高等／特高／极高」。v3 改为与页面逐项同源的「距离 + dBZ」双因子五档。
  2. 【推送策略】v2 只在「等级提升」时推一次，同档持续期间再不复发。
     v3 改为：首次触发即推 → 同档持续每 REPEAT_MIN 分钟复推一次 → 升级立即推
     → 降级／解除各推一条。
  3. 【修崩溃】v2 的 motion_gate() 用 math.radians(方位) 处理中文方位字符串，
     只要趋势样本够 3 帧就必抛 TypeError；而最外层 except 把异常吞了，
     于是「静默崩溃」——触发了也发不出通知。v3 增加方位字符串→角度转换。
  4. 【多通道】方糖（微信）/ Bark（iOS，可强制响铃）/ ntfy / 企业微信 / 钉钉。

运行：python xm_sentinel.py            # 正常巡检
     python xm_sentinel.py --test-push # 只发一条测试通知，不判级
     python xm_sentinel.py --dry       # 正常判级但不真发通知，打印结果
"""

import io
import os
import sys
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ==================================================================
# 一、监测对象（与预警台页面同一份名单）
# ==================================================================
MODE = "stations"          # "stations" = 下方 13 个固定点位；"custom" = CUSTOM_POINTS
CUSTOM_POINTS = [
    ("我的位置", 24.4798, 118.0894),
]

TERMS = [
    ("嵩屿集装箱码头", 24.4472, 118.0322),
    ("远海码头", 24.4575, 117.9702),
    ("新海达码头", 24.4580, 117.9625),
    ("海天码头", 24.5097, 118.0819),
    ("海翔码头", 24.5351, 118.2273),
    ("引航点(17#灯浮)", 24.3883, 118.1167),
    ("九节礁(厦门港入海口)", 24.3457, 118.1493),
]
CITIES = [
    ("港务大厦", 24.4884, 118.0712),
    ("五缘湾湿地公园", 24.5196, 118.1783),
    ("牡丹国际大酒店", 24.4770, 118.1290),
    ("银行中心", 24.4631, 118.0740),
    ("厦门大学(思明校区)", 24.4399, 118.0930),
    ("禾祥鑫天地", 24.4725, 118.1025),
]

# ==================================================================
# 二、五档判据 —— 与《厦门短临预警台》逐项对齐（改这里必须同步改页面）
# ==================================================================
SEV_NAMES = ["正常", "关注提醒", "较高预警", "高等预警", "特高预警", "极高预警"]

DBZ_SEARCH = 35     # 「最近回波」搜索门槛（与页面 state.settings.dbz 默认值逐字同源）。
                    # ★ 2026-10-01 用户要求：各强对流模块的「最近 ≥45dBZ 回波」统一改为「≥35dBZ 回波」。
                    #   它不是纯文案：d_obs（最近回波距离）由它过滤，直接进趋势核算与档位判定，
                    #   改它等于放宽「最早能看到多远/多弱的回波」，判定会整体前移。
DBZ_ACT = 40        # 较高预警：10 km 内峰值 40~50 dBZ
DBZ_HIGH = 51       # 高等预警：51~55 dBZ
DBZ_EHIGH = 56      # 特高预警：56~60 dBZ
DBZ_XHIGH = 61      # 极高预警：≥61 dBZ
T_ACT_KM = 10.0     # 较高预警及以上：明确逼近至 5~10 km
T_WATCH_KM = 12.0   # 关注提醒：外围 10~12 km 有对流云团且明确逼近
T_HIT_KM = 0.5      # 已抵达：≤0.5 km ＝实况影响（既成事实，不受逼近总闸限制）
T_NEAR_KM = 5.0      # 临近确认圈：预警（≥较高）存续期间，最近 ≥40 dBZ 强回波首次进入 5 km
                     # → 追加一条「临近确认」。5 km 对快速风暴（30~45 km/h）约 6~10 分钟到达，
                     # 是「不是预测、马上就到」的节点；首报仍留在 10 km 以争取 15~25 分钟提前量。
T_NEAR_EXIT_KM = 5.8  # 出圈滞回：>5.8 km 才复位确认标记，防临界抖动反复推

# ---- 「明确逼近」三重佐证：位移死区 / 速率门槛 / 方位一致性 ----
MOVE_MIN_KM = 0.5    # 位移死区：窗口净位移不足此值 → 一律判「停滞」（既不逼近也不远离）。
                     # ★ 必须与页面 trendFrom 里的 MOVE_MIN_KM 逐字同源：
                     #   2026-10-01 用户要求 2.0 → 1.0 → 0.5，最终定 0.5 km。
                     #   本窗口只有 18 分钟（4 帧），0.5 km/0.3 h ≈ 1.7 km/h，与速率线同量级。
BEAR_CON_MIN = 0.70  # 方位一致性下限（单位向量合成度）：低于此值视为换团／外沿跳变（2026-10-01 用户要求由 0.85 放宽到 0.70）
V_NEAR = -1.5        # km/h，< -1.5 才算逼近
V_AWAY = 1.5         # km/h，> +1.5 才算远离
MAD_MAX = 25.0       # 速率离散上限（km/h），超过视为抖动过大不采信
V_MAX = 120.0        # 移速可信范围（km/h，与页面 trendFrom 同口径）。超过即趋势不可判，
                     # ★ 这条非常关键：外推「已抵达」时若拿一个 200 km/h 的野值去外推，
                     #   会把十几公里外的回波算成「已覆盖本处」，而「已抵达」是绕过逼近总闸的 —— 直接误报。

# ---- 实况影响通道（2026-10-02 立，与页面 judgeTerm/termImpact 同源）----
# 移动方向可判「逼近/远离」，但「就地生成／准静止滞留」的回波不位移、却照样影响本地——
# 只认「移动逼近」必漏报（实例：新海达码头 50 dBZ、雨强 48.6 mm/h 已在头顶 2 km，却因「停滞」判正常）。
# 四重佐证缺一不可：① 10 km 内峰值 ≥ DBZ_ACT ② 最近 ≥DBZ_ACT 强回波 ≤ IMPACT_KM
# ③ 窗口内 ≥ IMPACT_MIN_FRAMES 帧同时满足①②且最新帧必须满足 ④ 径向趋势未达明确远离（v < V_AWAY）。
# 3 km ≈ 2 个像素：30 km/h 的风暴从 3 km 处抵达只需 6 分钟＝1 帧，已超出短临外推可分辨限度，
# 故视作「正在影响」而非「将要影响」——是观测事实，与「已抵达」同属既成事实放行通道。
IMPACT_KM = 3.0
IMPACT_MIN_FRAMES = 3

# ---- 雷达帧 ----
SLOT_MS = 6 * 60 * 1000   # 出帧节奏 6 分钟
N_FRAMES = 4              # 趋势窗口：最新帧 + 往前 3 帧（跨度 18 分钟）
LAG_MAX_MIN = 20.0        # 图龄上限：超过视为数据陈旧，不外推
SEARCH_KM = 80.0          # 单点位搜索半径
PEAK_KM = 10.0            # 「10 km 内峰值 dBZ」的统计半径

# ==================================================================
# 三、推送策略
# ==================================================================
REPEAT_MIN = 30          # 较高预警及以上：同档持续，每 N 分钟复推一次（0=不复推）
REPEAT_MIN_WATCH = 0     # 关注提醒：是否复推（0=不复推，只在触发时推一次）
NOTIFY_DOWNGRADE = True  # 降级（如特高→较高）是否推一条
NOTIFY_CLEAR = True      # 解除（回到正常）是否推一条
GLOBAL_PUSH = True       # 管理员全局通道（方糖→微信，按全部点位的最紧急档推）。
                         # 如果你自己也订阅了某几个点位，会同时收到全局与个人两条 —— 想只收个人订阅就改成 False。

# ==================================================================
# 三之二、自助订阅（订阅表存在 ntfy 主题上，无需任何后端）
# ==================================================================
# 页面把「我的 id / 昵称 / 推送主题 / 关注哪些点位 + 每点的起始档」POST 到订阅信箱主题，
# 哨兵每轮 poll 一次读回来（并镜像到 subscribers.json），再按人分头推送。
# ⚠️ 改这个主题名必须同步改页面里的 SUB_BUS，否则收不到任何订阅。
SUB_BUS_TOPIC = "xm-radar-subs-e6jpkktt8esz"
SUB_NTFY_SERVER = "https://ntfy.sh"

# 推送通道（可被同目录 push_config.json 覆盖，见文件末尾说明）
PUSH = {
    # 方糖（Server 酱）→ 微信。已有 key，默认开启。
    "ftqq": {"enable": True, "key": "SCT431207TfzoWwx8bEtO7zGoPphSg8nxx"},
    # Bark（iOS App）→ APNs，国内可达且支持「重要警告」突破静音。填 device key 后开启。
    # 注意：level 取 critical 需在 Bark App 内授予「重要警告」权限，否则会自动降级。
    "bark": {"enable": False, "server": "https://api.day.app", "keys": [],
             "level": "timeSensitive", "sound": "alarm"},
    # ntfy（iOS/Android App）→ 主题订阅模型：谁装了谁订同一主题，无需收集设备 key。
    # 主题名等同口令，请用足够长的随机串，不要用可猜的短词。
    "ntfy": {"enable": False, "server": "https://ntfy.sh", "topic": "", "token": ""},
    # 企业微信群机器人（免费、秒级，可 @所有人）
    "wecom": {"enable": False, "webhook": ""},
    # 钉钉群机器人（内容需含自定义关键词）
    "dingtalk": {"enable": False, "webhook": "", "keyword": "预警"},
}

# ==================================================================
# 四、雷达标定 / 色表（与预警台页面同源）
# ==================================================================
CAL = {"x0": -6904.11, "pxDegLon": 61.2, "y0": 2700.6, "pxDegLat": 70.0,
       "kmx": 1.655, "kmy": 1.580}

LUT = [
    (5, (65, 157, 241)), (10, (100, 231, 235)), (15, (109, 250, 61)),
    (20, (0, 216, 0)), (25, (1, 144, 0)), (30, (255, 255, 0)),
    (35, (231, 192, 0)), (40, (255, 144, 0)), (45, (255, 0, 0)),
    (50, (214, 0, 0)), (55, (192, 0, 0)), (60, (255, 0, 240)),
    (65, (150, 0, 180)), (70, (173, 144, 240)),
]
BEARINGS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
BEAR_OPP = {"北": "南", "东北": "西南", "东": "西", "东南": "西北",
            "南": "北", "西南": "东北", "西": "东", "西北": "东南"}
BEAR_DEG = {"北": 0.0, "东北": 45.0, "东": 90.0, "东南": 135.0,
            "南": 180.0, "西南": 225.0, "西": 270.0, "西北": 315.0}
SEV_EMOJI = {0: "✅", 1: "👀", 2: "⚠️", 3: "🔴", 4: "🟣", 5: "🚨"}

# 运行目录（默认 = 脚本所在目录，可用环境变量 XM_SENTINEL_DIR 覆盖）
BASE_DIR = os.environ.get("XM_SENTINEL_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, ".state.json")
SUB_FILE = os.path.join(BASE_DIR, "subscribers.json")
LOG_FILE = os.path.join(BASE_DIR, "sentinel.log")
LOCK_FILE = os.path.join(BASE_DIR, ".run.lock")
LOCK_STALE_SEC = 600      # 锁超过此时长视为陈旧，自动接管


# ==================================================================
# 五、基础工具
# ==================================================================
def log(line):
    """写运行日志（不打扰用户，仅本地排障用）"""
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (stamp, line))
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) > 800:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-800:])
    except Exception:
        pass


def http(url, data=None, headers=None, timeout=20):
    hd = {"User-Agent": "Mozilla/5.0 (compatible; xm-sentinel/3.0)"}
    if headers:
        hd.update(headers)
    req = urllib.request.Request(url, data=data, headers=hd)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get(url, timeout=20, referer=True):
    hd = {"Referer": "https://image.nmc.cn/"} if referer else None
    return http(url, None, hd, timeout)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(obj):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def acquire_lock():
    """防止两个调度器（Windows 计划任务 + WorkBuddy 定时）同时跑导致重复推送"""
    try:
        if os.path.exists(LOCK_FILE):
            if time.time() - os.path.getmtime(LOCK_FILE) < LOCK_STALE_SEC:
                return False
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


def bj(ms):
    """毫秒时间戳 → 北京时间字符串"""
    return datetime.fromtimestamp(ms / 1000.0,
                                  tz=timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


def load_push_config():
    """可选：同目录 push_config.json 覆盖 PUSH 中的通道配置（无需改脚本）"""
    p = os.path.join(BASE_DIR, "push_config.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in cfg.items():
            if k in PUSH and isinstance(v, dict):
                PUSH[k].update(v)
    except Exception:
        pass


# ==================================================================
# 六、雷达取帧 / 解码 / 逐点度量
# ==================================================================
def frame_url(ms):
    """NMC 华东雷达拼图帧地址（时间戳按 UTC）"""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    ts = "%04d%02d%02d%02d%02d00000" % (dt.year, dt.month, dt.day, dt.hour, dt.minute)
    return ("https://image.nmc.cn/product/%04d/%02d/%02d/RDCP/"
            "SEVP_AOC_RDCP_SLDAS3_ECREF_AECN_L88_PI_%s.PNG"
            % (dt.year, dt.month, dt.day, ts))


def fetch_latest_ts():
    """从当前时刻往前探测，返回最新可用帧的时间戳（ms），失败返回 None"""
    now_ms = time.time() * 1000.0
    base = int(now_ms // SLOT_MS) * SLOT_MS
    for i in range(12):
        t = base - i * SLOT_MS
        try:
            if len(http_get(frame_url(t), 20)) > 2000:
                return t
        except Exception:
            continue
    return None


def latlon_to_px(lat, lon):
    return (CAL["x0"] + CAL["pxDegLon"] * lon, CAL["y0"] - CAL["pxDegLat"] * lat)


def angle_of(dx, dy):
    """像素位移 → 方位角（正北 0°，顺时针，dy 向下为南）"""
    a = math.degrees(math.atan2(dx, -dy))
    return a % 360.0


def bearing_of(dx, dy):
    return BEARINGS[int(round(angle_of(dx, dy) / 45.0)) % 8]


def _decode_png(data):
    """纯 Python PNG 解码（8bit、非隔行）→ (w, h, channels, raw)
    零第三方依赖：云端沙箱不保证有 Pillow，故自带解码器兜底。"""
    import zlib
    import struct

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos = 8
    idat = bytearray()
    w = h = bd = ct = None
    while pos + 12 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        if typ == b"IHDR":
            w, h, bd, ct, _cm, _fm, il = struct.unpack(">IIBBBBB", data[pos + 8:pos + 21])
            if bd != 8 or il != 0:
                raise ValueError("unsupported png bd=%s il=%s" % (bd, il))
        elif typ == b"IDAT":
            idat += data[pos + 8:pos + 8 + ln]
        elif typ == b"IEND":
            break
        pos += 12 + ln

    raw = zlib.decompress(bytes(idat))
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    stride = w * ch
    out = bytearray(h * stride)
    prev = bytes(stride)
    p = 0
    for y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa = b - c
                pa = -pa if pa < 0 else pa
                pb = a - c
                pb = -pb if pb < 0 else pb
                pc = a + b - 2 * c
                pc = -pc if pc < 0 else pc
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 255
        off = y * stride
        out[off:off + stride] = line
        prev = line
    return w, h, ch, bytes(out)


def _dbz_of(r, g, b, cache, key):
    """像素颜色 → dBZ（最近邻色表匹配，70²=4900 为容差上限），带缓存"""
    md, mv = 1e18, 0
    for dbz, (cr, cg, cb) in LUT:
        dd = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if dd < md:
            md, mv = dd, dbz
    v = 0 if md > 4900 else mv
    cache[key] = v
    return v


def points_bbox(points, pad_km):
    """所有监测点的包围盒（像素坐标），外扩 pad_km。
    作用：把扫描限制在关注区域，同时天然排除雷达图右下角的图例色标条（其 dBZ 色块会污染判据）。"""
    pxs = [latlon_to_px(lat, lon) for _, lat, lon in points]
    dx = pad_km / CAL["kmx"]
    dy = pad_km / CAL["kmy"]
    return (min(p[0] for p in pxs) - dx, min(p[1] for p in pxs) - dy,
            max(p[0] for p in pxs) + dx, max(p[1] for p in pxs) + dy)


def strong_pixels(img_bytes, th, bbox=None):
    """解码 PNG，返回 bbox 内所有 ≥th 的像素 [(x, y, dbz), ...]
    优先用 Pillow（快）；不可用时回落到内置纯 Python 解码器。"""
    w = h = ch = None
    raw = None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        w, h = img.size
        raw = img.tobytes()
        ch = 4
    except Exception:
        raw = None
    if raw is None:
        w, h, ch, raw = _decode_png(img_bytes)

    if bbox:
        x0 = max(0, int(bbox[0]))
        y0 = max(0, int(bbox[1]))
        x1 = min(w, int(bbox[2]) + 1)
        y1 = min(h, int(bbox[3]) + 1)
    else:
        x0, y0, x1, y1 = 0, 0, w, h

    if ch < 3:
        return w, h, []

    cache = {}
    out = []
    ap = out.append
    for y in range(y0, y1):
        rowbase = y * w * ch
        for x in range(x0, x1):
            i = rowbase + x * ch
            key = (raw[i] << 16) | (raw[i + 1] << 8) | raw[i + 2]
            v = cache.get(key)
            if v is None:
                v = _dbz_of(raw[i], raw[i + 1], raw[i + 2], cache, key)
            if v >= th:
                ap((x, y, v))
    return w, h, out


def frame_metrics(points_px, pixels):
    """一帧内、逐监测点的度量 → [(dist, bear, deg, peak10, dist40), ...]
      dist   ：最近 ≥DBZ_SEARCH 像素的距离 km（SEARCH_KM 内无则 None）
      bear/deg：该最近像素的方位
      peak10 ：PEAK_KM 半径内的峰值 dBZ（无回波为 0）
      dist40 ：最近 ≥DBZ_ACT 像素的距离 km（与页面 st.dist40 同源）。

    ★ 为什么要多算一个 dist40（2026-10-01 补）：
      门槛 35 < DBZ_ACT 40 时，dist 会被「更近的弱回波」主导，可能远小于 dist40。
      页面 actGate 在「10 km 内峰值未达 40」时还有一条兜底：「≥40 强回波已进入 12 km
      警戒带 → 强度仍有发展空间，照强度档通报」。旧口径 th=45 > 40，dist ≥ dist40 恒成立、
      该分支永不触发；门槛降到 35 后它会被激活，哨兵必须同步具备，否则两边档位会分叉。"""
    search2 = SEARCH_KM * SEARCH_KM
    peak2 = PEAK_KM * PEAK_KM
    kmx, kmy = CAL["kmx"], CAL["kmy"]
    out = []
    for (px, py) in points_px:
        best = None      # (d2, bear, deg)
        best40 = None
        peak = 0
        for (x, y, dbz) in pixels:
            dx = (x - px) * kmx
            dy = (y - py) * kmy
            d2 = dx * dx + dy * dy
            if d2 > search2:
                continue
            if d2 <= peak2 and dbz > peak:
                peak = dbz
            if best is None or d2 < best[0]:
                best = (d2, bearing_of(dx, dy), angle_of(dx, dy))
            if dbz >= DBZ_ACT and (best40 is None or d2 < best40):
                best40 = d2
        d40 = None if best40 is None else math.sqrt(best40)
        if best is None:
            out.append((None, None, None, peak, d40))
        else:
            out.append((math.sqrt(best[0]), best[1], best[2], peak, d40))
    return out


# ==================================================================
# 七、趋势核算 + 明确逼近判定（与页面 approachOk / motion_gate 同源）
# ==================================================================
def motion_of(times, dists):
    """最小二乘速率核算。times: 帧时刻 ms（升序）；dists: 对应距离 km（可含 None）
    返回 (v, net, con, mad, stall)：v<0 逼近 / v>0 远离（km/h）；net 窗口净位移（km）；
    con 方位一致性（0~1）；mad 速率离散（km/h）；stall 停滞原因（None/'dead'/'flicker'）。
    样本不足返回 (None, None, None, None, None)。"""
    pts = [(times[i], dists[i]) for i in range(len(dists)) if dists[i] is not None]
    if len(pts) < 3:
        return None, None, None, None, None
    n = len(pts)
    tm = sum(t for t, _ in pts) / n
    dm = sum(d for _, d in pts) / n
    sx = sum((t - tm) ** 2 for t, _ in pts)
    if not sx:
        return None, None, None, None, None
    slope = sum((t - tm) * (d - dm) for t, d in pts) / sx   # km/ms
    v = slope * 3600000.0
    net = pts[-1][1] - pts[0][1]
    # 逐对斜率 → 离散度（km/h）；逐帧步进 → 抖动基准（km/步）
    pair = []
    step = []
    for i in range(1, n):
        dt = pts[i][0] - pts[i - 1][0]
        if dt > 0:
            pair.append((pts[i][1] - pts[i - 1][1]) / dt * 3600000.0)
            step.append(abs(pts[i][1] - pts[i - 1][1]))
    mad = None
    if len(pair) >= 2:
        pm = sorted(pair)
        med = pm[len(pm) // 2]
        dev = sorted(abs(x - med) for x in pair)
        mad = dev[len(dev) // 2]
    # ① 位移死区（与页面 trendFrom 同源，且在 V_MAX 之后、MAD 之前）：
    #    窗口净位移未达阈值 → 不管拟合出的斜率是多少，一律判「停滞」（v=0）。
    #    这一条同时管住两件事：(a) 档位判定的方向口径；(b) 外推「已抵达」——
    #    净位移近零时绝不能用噪声拟合出的速率外推，否则会把没动的回波推成「已覆盖本处」。
    stall = None
    if abs(net) < MOVE_MIN_KM:
        v = 0.0
        stall = "dead"
    else:
        # ①b 净位移置信（2026-10-01 补，与页面 trendFrom 同源）：
        #    每帧只取「最近一个 ≥阈值像素」，在原地生消、强弱闪烁的回波场里会逐帧跳目标，
        #    距离序列上下乱跳也能拟合出看似合理的斜率（真实案例：远海码头 2.5 km 处回波
        #    实际向西南远离，序列却拟合出「逼近 2 km/h」）。因此净位移还必须 ≥ 2×帧间
        #    抖动中位数——信号至少要是噪声的 2 倍才承认「同一团在整体位移」。
        s = sorted(step)
        noise = s[len(s) // 2] if s else 0.0
        if abs(net) < 2.0 * noise:
            v = 0.0
            stall = "flicker"
    return v, net, None, mad, stall


def bearing_consistency(bearings):
    """方位一致性：单位向量合成度 |Σe^{iθ}| / n（0~1）。样本不足返回 None。"""
    degs = [BEAR_DEG[b] for b in bearings if isinstance(b, str) and b in BEAR_DEG]
    if not degs:
        nums = [float(b) for b in bearings if isinstance(b, (int, float))]
        degs = nums
    n = len(degs)
    if n < 2:
        return None
    bx = sum(math.cos(math.radians(a)) for a in degs) / n
    by = sum(math.sin(math.radians(a)) for a in degs) / n
    return math.sqrt(bx * bx + by * by)


def approach_gate(v, net, con, mad, stall=None):
    """「明确逼近才预警」总闸 → (ok, 说明)

    判定顺序与页面 trendFrom → approachOk **严格对齐**（顺序不同会导致同一场景两边给不同的理由）：
      ① 移速可信范围（>V_MAX 视为趋势不可判）
      ② 位移死区（|净位移| < MOVE_MIN_KM → 停滞，既不逼近也不远离）
      ②b 净位移置信（|净位移| < 2×帧间抖动中位数 → 原地生消／逐帧跳目标，非整体位移）
      ③ 速率离散度 MAD（抖动过大不采信）
      ④ 速率方向（±V_NEAR / ±V_AWAY 三条线）
      ⑤ 方位一致性（确认是「同一团在移动」而非外沿形变／换团）
    """
    if v is not None and abs(v) > V_MAX:
        return False, "移速超出可信范围（%.0f km/h > %.0f），趋势暂不可判" % (abs(v), V_MAX)
    if net is not None and abs(net) < MOVE_MIN_KM:
        return False, ("回波移动停滞（窗口净位移 %.2f km 未达 %.1f km 死区阈值），"
                       "不构成本处逼近" % (net, MOVE_MIN_KM))
    if stall == "flicker":
        return False, ("回波原地生消／外沿闪烁（窗口净位移 %.1f km 小于帧间抖动中位数的 2 倍），"
                       "是「最近回波」逐帧跳目标造成的假位移，不构成明确逼近" % net)
    if mad is not None and mad > MAD_MAX:
        return False, "移速离散度 ±%.0f km/h 过大，趋势不可信" % mad
    if v is None:
        return False, ("逼近佐证不足（净位移 %s），不构成明确逼近"
                       % ("%.1f km" % net if net is not None else "样本不足"))
    if v > V_AWAY:
        return False, "回波正在远离（+%.1f km/h）" % v
    if v >= V_NEAR:
        return False, "回波移动停滞（%.1f km/h，未过 ±%.1f km/h 速率线）" % (v, abs(V_NEAR))
    if con is None:
        return False, "位移趋势指向逼近，但方位佐证尚未积累，不构成明确逼近"
    if con < BEAR_CON_MIN:
        return False, "最近回波方位摆动较大（一致性 %.1f%%），疑为外沿跳变／换团" % (con * 100)
    return True, "明确逼近（%.1f km/h，方位一致 %.0f%%）" % (v, con * 100)


def tier_of(peak):
    """强度 → 档位（2 较高 / 3 高等 / 4 特高 / 5 极高）"""
    if peak >= DBZ_XHIGH:
        return 5
    if peak >= DBZ_EHIGH:
        return 4
    if peak >= DBZ_HIGH:
        return 3
    return 2


def impact_gate(metrics, v):
    """实况影响闸门（2026-10-02 新增，与页面 judgeTerm/termImpact 同源）→ (ok, 持续帧数)

    就地生成／准静止滞留的强回波不位移却照样影响本地，单看位移趋势会漏报。
    「实况影响」是观测事实而非预测，但仍要防单帧杂波，四重佐证缺一不可：
      ① 最新帧 10 km 内峰值 ≥ DBZ_ACT
      ② 最新帧最近 ≥DBZ_ACT 强回波 ≤ IMPACT_KM
      ③ 窗口内 ≥ IMPACT_MIN_FRAMES 帧同时满足①②
      ④ 径向趋势未达明确远离（v < V_AWAY；正在离开＝影响趋于结束，不触发）
    """
    qual = 0
    for (_d, _b, _g, pk, d40) in metrics:
        if pk and pk >= DBZ_ACT and d40 is not None and d40 <= IMPACT_KM:
            qual += 1
    last = metrics[-1]
    last_ok = bool(last[3] and last[3] >= DBZ_ACT and last[4] is not None and last[4] <= IMPACT_KM)
    away = (v is not None and v >= V_AWAY)
    return (last_ok and qual >= IMPACT_MIN_FRAMES and not away), qual


def judge_point(times, metrics, lag_min):
    """单个监测点定级。metrics: 逐帧 (dist, bear, deg, peak10, dist40)，时间升序。
    返回 dict(sev, why, dist, bear, deg, v, con, peak, arrived)"""
    dists = [m[0] for m in metrics]
    bears = [m[1] for m in metrics]
    peak10 = metrics[-1][3] or 0
    d40 = metrics[-1][4]        # 最近 ≥DBZ_ACT 强回波距离（页面 st.dist40 口径）

    # 最新有效距离
    d_obs, bear, deg = None, None, None
    for i in range(len(metrics) - 1, -1, -1):
        if metrics[i][0] is not None:
            d_obs, bear, deg = metrics[i][0], metrics[i][1], metrics[i][2]
            break

    v_raw, net, _con, mad, stall = motion_of(times, dists)
    con = bearing_consistency(bears)
    ap_ok, ap_why = approach_gate(v_raw, net, con, mad, stall)
    # 超出可信范围的速率不能用来外推（否则会把远处的回波算成「已抵达」而绕过逼近总闸）
    v = v_raw if (v_raw is not None and abs(v_raw) <= V_MAX) else None

    d_now = d_obs
    if d_obs is not None and v is not None:
        d_now = d_obs + v * (lag_min / 60.0)     # v<0 逼近 → 距离减小
    arrived = (d_now is not None and d_now <= T_HIT_KM)

    base = {"dist": d_obs, "bear": bear, "deg": deg, "v": v, "con": con,
            "peak": peak10, "arrived": arrived, "d_now": d_now, "d40": d40}

    if d_obs is None and peak10 < DBZ_ACT:
        base.update(sev=0, why="%g km 内无 ≥%d dBZ 回波" % (SEARCH_KM, DBZ_SEARCH))
        return base
    if d_obs is None:
        base.update(sev=0, why="10 km 内峰值仅 %d dBZ，未达 %d dBZ 门槛" % (peak10, DBZ_ACT))
        return base
    if not ap_ok and not arrived:
        # 实况影响通道：就地生成／准静止滞留的强回波不位移却照样影响本地，单看位移趋势会漏报。
        imp_ok, imp_n = impact_gate(metrics, v)
        if not imp_ok:
            base.update(sev=0, why="最近 ≥%d dBZ 回波位于 %.1f km，但%s，不触发任何级别"
                        % (DBZ_SEARCH, d_obs, ap_why))
            return base
        impacted = True
        near = ("≥%d dBZ 强回波贴身滞留已持续 %d 帧（约 %d 分钟，就地生成或准静止），"
                "实况影响中（最近 %.1f km，%s方）" % (DBZ_ACT, imp_n, imp_n * 6, d_obs, bear or "—"))
    else:
        impacted = False
        if arrived and d_obs > T_HIT_KM:
            # 外推抵达：图上仍有距离，但按逼近速率折算图龄后已覆盖本处 —— 文案须如实区分
            near = ("回波外推至此刻已覆盖本处（图上 %.1f km，外推移速 %.0f km/h），实况影响中"
                    % (d_obs, -v if v else 0.0))
        else:
            near = "雷达回波已抵达（%.1f km）" % d_obs if arrived else "明确逼近至 %.1f km" % d_obs
    if d_obs <= T_ACT_KM:
        if peak10 >= DBZ_ACT:
            t = tier_of(peak10)
            how = "贴身强回波实况影响（非逼近预测）" if (impacted or arrived) else ap_why
            base.update(sev=t, why="%s，10 km 内峰值 %d dBZ，%s，满足「%s」触发条件"
                        % (near, peak10, how, SEV_NAMES[t]))
            return base
        # 与页面 actGate 兜底分支同源：10 km 内峰值未达 DBZ_ACT，但 ≥DBZ_ACT 的强回波
        # 已进入 12 km 警戒带且正在逼近 → 强度仍有发展空间，照强度档通报（而非降为关注提醒）
        if d40 is not None and d40 <= T_WATCH_KM:
            t = tier_of(peak10)
            base.update(sev=t, why="%s，10 km 内峰值 %d dBZ 未达 %d dBZ，"
                        "但 ≥%d dBZ 强回波已进入 %.1f km 警戒带且正在逼近，强度仍有发展空间，"
                        "按「%s」通报" % (near, peak10, DBZ_ACT, DBZ_ACT, d40, SEV_NAMES[t]))
            return base
        base.update(sev=1, why="%s，但 10 km 内峰值 %d dBZ 未达 %d dBZ，按关注提醒通报"
                    % (near, peak10, DBZ_ACT))
        return base
    if d_obs <= T_WATCH_KM:
        base.update(sev=1, why="外围 %.1f km 有 ≥%d dBZ 对流云团（%s方），%s"
                    % (d_obs, DBZ_SEARCH, bear or "—", ap_why))
        return base
    base.update(sev=0, why="最近 ≥%d dBZ 回波位于 %.1f km，尚未进入 %.0f km 警戒范围"
                % (DBZ_SEARCH, d_obs, T_WATCH_KM))
    return base


# ==================================================================
# 八、推送通道
# ==================================================================
def _post_json(url, obj, headers=None, timeout=15):
    hd = {"Content-Type": "application/json"}
    if headers:
        hd.update(headers)
    return http(url, json.dumps(obj, ensure_ascii=False).encode("utf-8"), hd, timeout).decode("utf-8", "ignore")


def push_ftqq(title, body, sev):
    cfg = PUSH["ftqq"]
    data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
    return http("https://sctapi.ftqq.com/%s.send" % cfg["key"], data,
                {"Content-Type": "application/x-www-form-urlencoded"}, 15).decode("utf-8", "ignore")


def push_bark(title, body, sev):
    cfg = PUSH["bark"]
    ok = []
    for key in cfg.get("keys") or []:
        url = "%s/%s" % (cfg.get("server", "https://api.day.app").rstrip("/"), key)
        obj = {"title": title, "body": body, "group": "厦门短临预警",
               "level": cfg.get("level", "timeSensitive")}
        if cfg.get("sound"):
            obj["sound"] = cfg["sound"]
        if sev >= 4:
            obj["level"] = cfg.get("level", "timeSensitive")
        ok.append(_post_json(url, obj, None, 15))
    return " | ".join(ok) or "无 device key"


def push_ntfy_topic(topic, title, body, sev, server=None, token=None):
    """往 ntfy 主题发一条通知。
    ★ 不设 Title 头：urllib 的 HTTP 头只能编码 latin-1，中文标题会直接抛 UnicodeEncodeError。
      改为把标题放进正文首行（ntfy 客户端渲染效果一致），只保留 Priority / Tags 这类 ASCII 头。"""
    if not topic:
        raise ValueError("空的 ntfy 主题")
    url = "%s/%s" % ((server or PUSH["ntfy"].get("server") or "https://ntfy.sh").rstrip("/"), topic)
    hd = {"Priority": "urgent" if sev >= 3 else "high",
          "Tags": "rotating_light" if sev >= 3 else "cloud_with_lightning"}
    tk = token or PUSH["ntfy"].get("token")
    if tk:
        hd["Authorization"] = "Bearer " + tk
    text = (title + "\n\n" + body) if title else body
    return http(url, text.encode("utf-8"), hd, 20).decode("utf-8", "ignore")


def push_ntfy(title, body, sev):
    return push_ntfy_topic(PUSH["ntfy"].get("topic"), title, body, sev)


def push_wecom(title, body, sev):
    return _post_json(PUSH["wecom"]["webhook"],
                      {"msgtype": "text", "text": {"content": "%s\n%s" % (title, body)}})


def push_dingtalk(title, body, sev):
    kw = PUSH["dingtalk"].get("keyword", "")
    return _post_json(PUSH["dingtalk"]["webhook"],
                      {"msgtype": "text", "text": {"content": "%s%s\n%s" % (kw, title, body)}})


CHANNELS = [("ftqq", push_ftqq), ("bark", push_bark), ("ntfy", push_ntfy),
            ("wecom", push_wecom), ("dingtalk", push_dingtalk)]


def fetch_subscribers():
    """读订阅信箱（ntfy 主题）→ {订阅者id: {nick, topic, rules}}，同时镜像到 subscribers.json。
    规则：同一 id 以 ts 最新的一条为准；带 revoke 的删除该订阅者；信箱读不到时退回本地镜像。"""
    try:
        with open(SUB_FILE, "r", encoding="utf-8") as f:
            mirror = json.load(f) or {}
    except Exception:
        mirror = {}

    if not SUB_BUS_TOPIC:
        return mirror

    try:
        raw = http_get("%s/%s/json?poll=1&since=all" % (SUB_NTFY_SERVER.rstrip("/"), SUB_BUS_TOPIC),
                       20, referer=False)
    except Exception as e:
        log("读订阅信箱失败，沿用本地镜像 %d 条：%s" % (len(mirror), e))
        return mirror

    seen = 0
    for line in raw.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("event") != "message":
            continue
        try:
            rec = json.loads(ev.get("message") or "{}")
        except Exception:
            continue
        sid = rec.get("id")
        if not sid:
            continue
        if rec.get("revoke"):
            mirror.pop(sid, None)
            seen += 1
            continue
        ts = float(rec.get("ts") or 0)
        old = mirror.get(sid) or {}
        if float(old.get("_ts") or 0) > ts:
            continue                     # 本地已有更新的版本，不被旧消息覆盖
        mirror[sid] = {"nick": rec.get("nick") or "",
                       "topic": rec.get("topic") or "",
                       "rules": rec.get("rules") or {},
                       "_ts": ts}
        seen += 1

    if seen:
        try:
            with open(SUB_FILE, "w", encoding="utf-8") as f:
                json.dump(mirror, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return mirror


def subscriber_sev(results, rules):
    """按订阅者自己的清单与起始档过滤，取最紧急的一档。
    results: {点位名: judge_point 结果}；rules: {点位名: 起始档}。
    低于该点位自己的起始档 → 该点位对这个订阅者视为 0。
    返回 (sev, best, active)：active = {点位名: 档位}，该订阅者名下当前所有处于预警的点位。"""
    sev, best = 0, None
    active = {}
    for name, minsev in (rules or {}).items():
        r = results.get(name)
        if not r:
            continue
        try:
            floor = int(minsev)
        except Exception:
            floor = 2
        if r["sev"] < floor:
            continue
        if r["sev"] > 0:
            active[name] = r["sev"]
        if r["sev"] > sev or (r["sev"] == sev and best is not None
                              and (r["dist"] if r["dist"] is not None else 1e9)
                              < (best["dist"] if best["dist"] is not None else 1e9)):
            sev, best = r["sev"], r
    return sev, best, active


def notify_all(title, body, sev, dry=False):
    """向所有已启用通道推送，返回 [(通道, 是否成功, 回执摘要)]"""
    res = []
    for name, fn in CHANNELS:
        cfg = PUSH.get(name) or {}
        if not cfg.get("enable"):
            continue
        if dry:
            res.append((name, True, "DRY"))
            continue
        try:
            r = fn(title, body, sev)
            res.append((name, True, (r or "")[:160].replace("\n", " ")))
        except Exception as e:
            res.append((name, False, str(e)[:160]))
    return res


def decide_kind(sev, last_sev, last_push, now):
    """推送时机决策 → None / '触发' / '升级' / '持续' / '降级' / '解除'
    规则：① 首次触发或升级 → 立即推 ② 同档持续每 REPEAT_MIN 分钟复推一次防漏看
         ③ 降级、解除各推一条（可关）"""
    if sev > last_sev:
        return "升级" if last_sev > 0 else "触发"
    if sev == last_sev == 1 and REPEAT_MIN_WATCH > 0 and now - last_push >= REPEAT_MIN_WATCH * 60:
        return "持续"
    if sev == last_sev and sev >= 2 and REPEAT_MIN > 0 and now - last_push >= REPEAT_MIN * 60:
        return "持续"
    if sev == 0 and last_sev > 0 and NOTIFY_CLEAR:
        return "解除"
    if 0 < sev < last_sev and NOTIFY_DOWNGRADE:
        return "降级"
    return None


def build_message(kind, sev, best, lag_min, frame_ms, last_sev, clear_pts=None):
    """生成推送标题与正文。clear_pts：解除时点名用——{点位名: 原档位}，缺省则退回泛化文案。"""
    name = SEV_NAMES[sev]
    emoji = SEV_EMOJI.get(sev, "ℹ️")
    if kind == "解除":
        pts = [(p, s) for p, s in sorted((clear_pts or {}).items(), key=lambda kv: -kv[1])]
        if pts:
            lst = "、".join("「%s」" % p for p, s in pts)
            tail = "已回到正常" if len(pts) == 1 else "已全部回到正常"
            body = "%s%s。\n\n帧时间（北京）：%s（约 %.0f 分钟前）" % (lst, tail, bj(frame_ms), lag_min)
            title = "✅ 厦门短临 · 预警解除" + ((" · " + pts[0][0]) if len(pts) == 1 else "")
        else:
            title = "✅ 厦门短临 · 预警解除"
            body = ("所有监测对象已回到正常。\n\n帧时间（北京）：%s（约 %.0f 分钟前）"
                    % (bj(frame_ms), lag_min))
        return title, body
    p = best
    dist = p.get("d_now") if p.get("arrived") else p.get("dist")
    head = {"触发": "首次触发", "升级": "🔴⬆️ 升级", "持续": "仍在持续",
            "降级": "⬇️ 降级"}.get(kind, kind)
    if kind == "降级":
        emoji = "🟢"          # 降级通知整体用绿色：档位圆点换绿点，避免与红色预警档混淆
    title = "%s %s · %s（%s）" % (emoji, name, p["point"], head)
    lines = [
        "**%s** 最近 ≥%d dBZ 回波 **%.1f km**（%s方），10 km 内峰值 **%d dBZ**"
        % (p["point"], DBZ_SEARCH, p.get("dist") or 0, p.get("bear") or "—", p.get("peak") or 0),
        "",
        "级别：%s → %s（%s）" % (SEV_NAMES[last_sev], name, head),
        "判据：%s" % p.get("why", ""),
    ]
    if p.get("v") is not None:
        mv = "逼近 %.1f km/h" % (-p["v"]) if p["v"] < 0 else (
            "远离 %.1f km/h" % p["v"] if p["v"] > 0 else "基本停滞")
        con = ("，方位一致 %.0f%%" % (p["con"] * 100)) if p.get("con") is not None else ""
        lines.append("运动：%s%s" % (mv, con))
    if p.get("arrived"):
        lines.append("状态：回波外推至此刻已覆盖本处（实况影响）")
    lines += ["", "帧时间（北京）：%s（约 %.0f 分钟前）" % (bj(frame_ms), lag_min),
              "触发时刻：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
    return title, "\n".join(lines)


def build_near_message(r, lag_min, frame_ms):
    """「5 km 临近确认」：预警（≥较高）存续期间，最近 ≥40 dBZ 强回波首次进入 5 km 的追加确认条。
    与首报的分工：首报（10 km）抢提前量，本条确认「不是预测、马上就到」。"""
    d_near = r.get("d40") if r.get("d40") is not None else r.get("dist")
    title = "⏱️ 厦门短临 · 临近确认 · %s" % r["point"]
    body = (
        "**%s** 的 ≥%d dBZ 强回波已进入 **5 km 临近圈**：最近 **%.1f km**（%s方），10 km 内峰值 **%d dBZ**。\n"
        "级别维持「%s」。5 km 对快速风暴约 6~10 分钟内到达，请即落实防护措施。\n\n"
        "帧时间（北京）：%s（约 %.0f 分钟前）"
        % (r["point"], DBZ_ACT, d_near, r.get("bear") or "—", r.get("peak") or 0,
           SEV_NAMES[r["sev"]], bj(frame_ms), lag_min))
    return title, body


def near_confirm_scan(results, prev_flags):
    """扫描 5 km 临近确认（纯函数，便于单测）。返回 (hits, new_flags)。
    hits: [(r, d_near)] 本轮需要补发「临近确认」的点位（首次进圈且当前 ≥较高）；
    new_flags: {点位: bool} 供写状态——进圈 True，出圈/降回关注及以下 False。
    prev_flags: 上一轮的 {点位: bool}。
    滞回：已确认进圈后，距离在 5~5.8 km 之间摆动仍视为圈内（不重复推），>5.8 km 才复位。"""
    hits, new_flags = [], {}
    for r in results:
        nm = r["point"]
        d_near = r.get("d40") if r.get("d40") is not None else r.get("dist")
        if r["sev"] >= 2 and d_near is not None and (
                d_near <= T_NEAR_KM or (prev_flags.get(nm) and d_near <= T_NEAR_EXIT_KM)):
            new_flags[nm] = True
            if not prev_flags.get(nm):
                hits.append((r, d_near))
        else:
            new_flags[nm] = False
    return hits, new_flags


# ==================================================================
# 九、主流程
# ==================================================================
def main():
    load_push_config()

    if "--subs" in sys.argv:
        subs = fetch_subscribers()
        print("订阅信箱主题：%s" % SUB_BUS_TOPIC)
        print("本地镜像：%s" % SUB_FILE)
        if not subs:
            print("暂无订阅。")
        for sid, s in subs.items():
            rules = s.get("rules") or {}
            want = "、".join("%s≥%s" % (k, SEV_NAMES[int(v)] if str(v).isdigit() else v)
                            for k, v in rules.items())
            print("  %-14s %-10s → %-34s %s"
                  % (sid, s.get("nick") or "—", s.get("topic") or "—", want or "（未选点位）"))
        return 0

    if "--test-push" in sys.argv:
        title = "🧪 厦门短临预警哨兵 · 连通性测试"
        body = ("这是一条测试通知，用于确认推送通道可用。\n\n"
                "哨兵版本：v3（五档对齐页面）\n"
                "当前时间：%s\n\n若你看到本条消息，说明通道畅通。"
                % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        for name, ok, msg in notify_all(title, body, 3):
            print("[%s] %s  %s" % ("OK" if ok else "FAIL", name, msg))
        return 0

    dry = "--dry" in sys.argv
    force = 0
    if "--simulate" in sys.argv:
        try:
            force = int(sys.argv[sys.argv.index("--simulate") + 1])
        except Exception:
            force = 3
        force = max(1, min(5, force))

    if not acquire_lock():
        log("上一轮仍在运行（锁未释放），本轮跳过")
        return 0
    try:
        return _run(dry, force)
    finally:
        release_lock()


def _run(dry, force_sev=0):
    points = TERMS + CITIES if MODE == "stations" else CUSTOM_POINTS

    latest = fetch_latest_ts()
    if latest is None:
        log("拉取雷达帧失败，静默退出")
        return 0

    # ---- 取趋势窗口内的各帧（时间升序；缺帧自动跳过）----
    bbox = points_bbox(points, SEARCH_KM)
    points_px = [latlon_to_px(lat, lon) for _, lat, lon in points]
    times, metrics_by_frame = [], []
    for k in range(N_FRAMES - 1, -1, -1):
        t = latest - k * SLOT_MS
        try:
            data = http_get(frame_url(t), 20)
            if not data or len(data) < 2000:
                continue
            _w, _h, pixels = strong_pixels(data, DBZ_SEARCH, bbox)
            metrics_by_frame.append(frame_metrics(points_px, pixels))
            times.append(t)
        except Exception as e:
            log("取帧失败 %s: %s" % (bj(t), e))
            continue

    if not times:
        log("窗口内无可用帧，静默退出")
        return 0

    lag_min = max(0.0, min(LAG_MAX_MIN, (time.time() * 1000.0 - times[-1]) / 60000.0))

    # ---- 逐点定级，取全局最紧急 ----
    results = []
    for i, (name, _lat, _lon) in enumerate(points):
        mets = [fm[i] for fm in metrics_by_frame]
        r = judge_point(times, mets, lag_min)
        r["point"] = name
        results.append(r)

    sev = 0
    best = None
    for r in results:
        if r["sev"] > sev or (r["sev"] == sev and r["sev"] > 0 and best is not None
                              and (r["dist"] or 1e9) < (best["dist"] or 1e9)):
            sev = r["sev"]
            best = r
    if sev == 0:
        best = best or {"why": "全部监测对象无预警", "dist": None, "peak": 0}

    # ---- 通道测试：把全部点位强制拉到指定档，跑通「按人推送」链路（不写状态）----
    if force_sev:
        for r in results:
            if r["sev"] < force_sev:
                r["sev"] = force_sev
                r["why"] = "【模拟】" + (r.get("why") or "")
        sev = force_sev
        best = None
        for r in results:
            if best is None or r["sev"] > best["sev"] or \
               (r["sev"] == best["sev"] and (r["dist"] or 1e9) < (best["dist"] or 1e9)):
                best = r
        log("【模拟模式】强制档位=%s，跑通推送链路（不会写入状态）" % SEV_NAMES[sev])

    # ---- 推送决策 ----
    st = load_state()
    last_sev = int(st.get("sev", 0) or 0)
    last_push = float(st.get("last_push", 0) or 0)
    since = float(st.get("since", 0) or 0)
    now = time.time()

    # 上一轮的预警点位快照：解除推送要点名「哪个点位解除了」。
    # 旧版状态文件没有 active 字段 → 退回用上一轮的 point（最紧急点）＋ last_sev 兜底。
    prev_active = st.get("active") or (
        {st["point"]: last_sev} if (st.get("point") and last_sev > 0) else {})

    if sev != last_sev:
        since = now

    kind = decide_kind(sev, last_sev, last_push, now)

    subs = fetch_subscribers()
    log("巡检：最紧急=%s（%s%s，%s）；图龄 %.0f 分钟；上一轮 %s；订阅者 %d 人" % (
        sev and SEV_NAMES[sev] or "正常",
        best.get("point") or "—",
        ("，%.1f km" % best["dist"]) if best.get("dist") else "",
        best.get("why", "")[:70], lag_min, SEV_NAMES[last_sev], len(subs)))

    # ---------- ① 管理员全局通道（可关）----------
    pushed = False
    if kind and GLOBAL_PUSH and not force_sev:      # 模拟模式只验个人通道，不惊动全局微信
        title, body = build_message(kind, sev, best, lag_min, times[-1], last_sev,
                                    clear_pts=prev_active if kind == "解除" else None)
        res = notify_all(title, body, sev, dry=dry)
        okn = sum(1 for _, ok, _ in res if ok)
        log("全局推送【%s】%s | %s" % (kind, title, "; ".join("%s:%s" % (n, "OK" if o else m)
                                                          for n, o, m in res)))
        pushed = okn > 0
        if pushed:
            last_push = now
        if dry:
            print("--- DRY RUN · 全局 ---")
            print(title)
            print(body)

    # ---------- ② 按订阅者分人推送 ----------
    # 判定是共享的（上面已把全部点位算完），这里只做「按各人的清单与起始档过滤 + 分头投递」。
    # 每个人的状态独立（.state.json 的 subs 分片），否则 A 的等级变化会把 B 的推送时机吃掉。
    res_by_name = {r["point"]: r for r in results}
    old_subs = st.get("subs") or {}
    new_subs = {}
    for sid, sub in subs.items():
        topic = sub.get("topic")
        if not topic:
            continue
        s_sev, s_best, s_active = subscriber_sev(res_by_name, sub.get("rules"))
        prev = old_subs.get(sid) or {}
        p_sev = int(prev.get("sev", 0) or 0)
        p_push = float(prev.get("last_push", 0) or 0)
        s_kind = decide_kind(s_sev, p_sev, p_push, now)
        who = sub.get("nick") or sid
        if s_kind:
            title, body = build_message(
                s_kind, s_sev, s_best or best, lag_min, times[-1], p_sev,
                clear_pts=(prev.get("active") or {}) if s_kind == "解除" else None)
            if dry:
                print("--- DRY RUN · %s（%s）---" % (who, topic))
                print(title)
                print(body)
            else:
                try:
                    push_ntfy_topic(topic, title, body, s_sev)
                    p_push = now
                except Exception as e:
                    log("订阅者 %s 推送失败：%s" % (who, e))
            log("订阅者推送【%s】%s → %s：%s"
                % (s_kind, who, topic, title))
        new_subs[sid] = {"sev": s_sev,
                         "last_push": p_push,
                         "since": (now if s_sev != p_sev else float(prev.get("since", 0) or now)),
                         "nick": who,
                         # 该订阅者名下当前处于预警的点位快照：下轮「解除」时点名用
                         "active": s_active}

    if force_sev:
        return 0                    # 模拟只验通道，绝不把假档位写进状态（否则下一轮会误推「解除」）

    # ---------- ③ 5 km 临近确认 ----------
    # 分工：10 km 首报抢提前量（15~25 分钟），5 km 确认告诉用户「不是预测、马上就到」（6~10 分钟）。
    # 只对 ≥较高存续中的点位生效，每个预警过程只确认一次（滞回 5.8 km 防抖）；
    # 本轮若刚因 触发/升级/持续 推送过（推送正文已带当前距离），不重复单发，只记标记。
    near_flags = st.get("near5") or {}
    near_hits, near_new = near_confirm_scan(results, near_flags)
    if near_hits and pushed and kind in ("触发", "升级", "持续"):
        log("临近确认 %s 因本轮首报/复推已携带实时距离，不单发" % "、".join(r["point"] for r, _ in near_hits))
        near_hits = []
    for r, _dn in near_hits:
        title, body = build_near_message(r, lag_min, times[-1])
        res = notify_all(title, body, r["sev"], dry=dry)
        log("临近确认【%s】%s | %s" % (r["point"], title, "; ".join("%s:%s" % (n, "OK" if o else m)
                                                                  for n, o, m in res)))
        if dry:
            print("--- DRY RUN · 临近确认 ---")
            print(title)
            print(body)
        for sid, sub in subs.items():     # 订阅了该点位且起始档够得着当前档的人，同样收到确认
            topic = sub.get("topic")
            if not topic:
                continue
            try:
                floor = int((sub.get("rules") or {}).get(r["point"], 99))
            except Exception:
                floor = 99
            if floor > r["sev"]:
                continue
            if dry:
                print("--- DRY RUN · 临近确认 · %s ---" % (sub.get("nick") or sid))
                print(title)
                print(body)
                continue
            try:
                push_ntfy_topic(topic, title, body, r["sev"])
            except Exception as e:
                log("订阅者 %s 临近确认推送失败：%s" % (sub.get("nick") or sid, e))
        log("订阅者临近确认【%s】已投递" % r["point"])

    save_state({
        "sev": sev, "name": SEV_NAMES[sev],
        "point": best.get("point") or "",
        "dist": best.get("dist"),
        # 全局预警点位快照：下一轮「解除」推送要点名「哪个/哪些点位解除了」
        "active": {r["point"]: r["sev"] for r in results if r["sev"] > 0},
        "since": since,
        "last_push": last_push,
        "frame": bj(times[-1]),
        "checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "subs": new_subs,
        # 5 km 临近确认标记：{点位: bool}，True＝本预警过程已确认过，出圈/降档后复位
        "near5": near_new,
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # 任何异常都不打扰用户，但必须留下痕迹（v2 的崩溃就是被这里吞掉的）
        try:
            import traceback
            log("异常: " + traceback.format_exc().replace("\n", " | "))
        except Exception:
            pass
        sys.exit(0)
