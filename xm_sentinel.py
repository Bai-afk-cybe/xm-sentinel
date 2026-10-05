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

# ---- 回波团成色（2026-10-02 立，与页面 analyzeTerminals 的连通域过滤同源）----
# ★ 为什么必须有：单片位拼图上的孤立强像素多数是杂波（地物／海杂波／超折射／生物回波），
#   不是降水。实测事故（10-02 10:18 牡丹国际大酒店）：最新帧 15 km 内仅 1 个孤立 40 dBZ 像素，
#   被当成「最近回波 2.9 km」，配上无上限图龄外推 → 判「已抵达」→ 误报较高预警；
#   而该点位该时刻实际无降水。真实对流回波在 3 km 分辨率上必然成片。
#   单格点面积 ≈ kmx×kmy ≈ 2.6 km²，MIN_CELLS=4 ⇒ 有效团面积 ≥ ~10 km²。
MIN_CELLS = 4

# ---- 实况影响通道（2026-10-02 立，与页面 judgeTerm/termImpact 同源）----
# 移动方向可判「逼近/远离」，但「就地生成／准静止滞留」的回波不位移、却照样影响本地——
# 只认「移动逼近」必漏报（实例：新海达码头 50 dBZ、雨强 48.6 mm/h 已在头顶 2 km，却因「停滞」判正常）。
# 四重佐证缺一不可：① 10 km 内峰值 ≥ DBZ_ACT ② 最近 ≥DBZ_ACT 强回波 ≤ IMPACT_KM
# ③ 窗口内 ≥ IMPACT_MIN_FRAMES 帧同时满足①②且最新帧必须满足 ④ 径向趋势未达明确远离（v < V_AWAY）。
# 5 km ≈ 3~4 个像素：30 km/h 的风暴从 5 km 处抵达约 10 分钟＝1~2 帧，仍在短临外推可分辨限度内，
# 视作「正在影响」——是观测事实，与「已抵达」同属既成事实放行通道。
# 2026-10-05 斌哥：3→5 km，覆盖「40 dBZ 强回波停在 4~5 km 既未逼近也未贴身」的漏报场景（我的位置已暴雨却判正常）。
IMPACT_KM = 5.0
IMPACT_MIN_FRAMES = 2   # 2026-10-05 斌哥：3→2 帧，缩短确认窗口（约 ≥6 分钟），更快触发实况影响，仍 ≥2 帧防单帧杂波

# ---- 「就地生成·单帧即时」通道（2026-10-05 斌哥立，与页面 GEN_* 逐字同源）----
# 在点位 5 km 内**原生成**的强对流没有位移可判，走「实况影响」又要等 ≥2 帧（≈12 分钟），
# 叠加雷达图 6~12 分钟滞后 —— 头顶爆出来的雨幕等确认已太迟。故本帧 5 km 内出现成片 ≥GEN_DBZ
# 且**上一帧同处无该强度回波**（真新生成，非移入）即单帧发布初判；逐帧复评，连续
# GEN_RETRACT_FRAMES 帧不再达标或径向明确远离 → 自动解除（解除走既有「解除」推送链路）。
# 静默规则：35~39 dBZ 只上页面（不发任何推送）、同档持续不重复响铃、冷却 GEN_COOLDOWN_S 内不响。
GEN_KM = 5.0             # 判定半径（km）：与 IMPACT_KM 同尺
GEN_DBZ = 35             # 判定强度下限（dBZ）：国际 SCIT 单体识别阈值
GEN_PUSH_DBZ = 40        # 推送门槛：≥40 dBZ（雷雨·暴雨门槛）才推送；35~39 只上页面
GEN_MIN_CELLS = 4        # 成片门槛：5 km 内 ≥GEN_DBZ 像素数下限（同 MIN_CELLS）
GEN_RETRACT_FRAMES = 2   # 连续不达标帧数 → 自动解除（≈12 分钟）
GEN_COOLDOWN_S = 30 * 60 # 同点位同档推送冷却（秒）

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
    # 方糖（Server 酱）→ 微信。key 只从环境变量 XM_FTQQ_KEY 读（云端在仓库 Secrets 配置），
    # 绝不明文写进代码 —— 本文件要上传公开仓库。
    "ftqq": {"enable": bool(os.environ.get("XM_FTQQ_KEY")), "key": os.environ.get("XM_FTQQ_KEY", "")},
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
# 三之三、息屏提醒（Web Push，v203）
# ==================================================================
# 手机息屏后浏览器冻结页面，页面前台的 AudioContext 声音必然发不出。要「息屏也能提醒」，
# 唯一通道是 Web Push：哨兵（本脚本，本就独立于浏览器跑在云端）发现预警后，用 VAPID 私钥
# 向浏览器推送服务发一条加密通知，手机锁屏弹系统横幅+响铃。
# 订阅来源：页面「息屏提醒」开关开启时，把 pushManager.subscribe 拿到的订阅对象
#   {endpoint, keys:{p256dh,auth}} 随订阅一起 POST 到 SUB_BUS_TOPIC，本脚本 fetch_subscribers
#   读回时存进订阅者的 push 字段，推送时对每个订阅者额外发一条 Web Push。
# VAPID：公钥写进页面（前端 subscribe 必需），私钥只从环境变量 XM_VAPID_PRIVATE_KEY 读
#   （云端在仓库 Secrets 配置，本地在 .env.local），绝不明文写进代码（本文件要上传公开仓库）。
VAPID_SUBJECT = "mailto:xm-nowcast@example.com"     # VAPID 声明主体（RFC8292 要求 mailto:/https: URI）
VAPID_PUB_KEY = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEK_dIGhEDB-zDEjUSr3-NOFpxrHmwkryP2QTBjefshsYKQ_VVXsCYa1KUgAFo6erPTSTenhDWGp1Mf2qBZRhDEQ"
VAPID_PRIV_KEY = os.environ.get("XM_VAPID_PRIVATE_KEY", "")

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
        stamp = now_bj().strftime("%Y-%m-%d %H:%M:%S")
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


def now_bj():
    """当前北京时间（datetime 对象）。本地跑=北京时区没问题，但云端 GitHub Actions
    服务器是 UTC，曾把推送里的「触发时刻」写成 09:01（实为 17:01，差 8 小时，2026-10-02 实证）。
    所有落盘 / 推送时间一律走这里，全链路统一北京口径。"""
    return datetime.now(timezone(timedelta(hours=8)))


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


def drop_small_components(pixels, min_cells=None):
    """连通域过滤：8 邻域标记，丢弃格点数 < min_cells 的孤立团 → 保留的像素列表。

    这是「多重佐证」的第一重：**先确认看到的是不是一片真实的回波**。
    孤立 1~3 个像素的强值在拼图上基本是杂波（地物／海杂波／超折射／生物回波）或解码噪点，
    把它们当作「最近回波」会让距离序列出现物理上不可能的跳变，进而拟合出假逼近。
    真实对流单体（哪怕是初生阶段）在 3 km 分辨率上都是几十个格点起步。"""
    mc = MIN_CELLS if min_cells is None else min_cells
    if mc <= 1 or not pixels:
        return pixels
    grid = {}
    for p in pixels:
        grid[(int(p[0]), int(p[1]))] = p[2]
    seen = set()
    keep = []
    for start in list(grid):
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        comp = []
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy, grid[(cx, cy)]))
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    q = (cx + ox, cy + oy)
                    if q in grid and q not in seen:
                        seen.add(q)
                        stack.append(q)
        if len(comp) >= mc:
            keep.extend(comp)
    keep.sort(key=lambda t: (t[1], t[0]))
    return keep


def strong_pixels(img_bytes, th, bbox=None, min_cells=None):
    """解码 PNG，返回 bbox 内所有 ≥th 的像素 [(x, y, dbz), ...]
    优先用 Pillow（快）；不可用时回落到内置纯 Python 解码器。
    min_cells：连通域最小格点数（默认 MIN_CELLS），过滤孤立杂波像素。"""
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
    out = drop_small_components(out, min_cells)      # ★ 剔除孤立杂波像素（见 MIN_CELLS 说明）
    return w, h, out


def frame_metrics(points_px, pixels):
    """一帧内、逐监测点的度量 → [(dist, bear, deg, peak10, dist40), ...]
      dist   ：最近 ≥DBZ_SEARCH 像素的距离 km（SEARCH_KM 内无则 None）
      bear/deg：该最近像素的方位
      peak10 ：PEAK_KM 半径内的峰值 dBZ（无回波为 0）
      dist40 ：最近 ≥DBZ_ACT 像素的距离 km（与页面 st.dist40 同源）。
      peak5  ：GEN_KM 半径内 ≥GEN_DBZ 的峰值 dBZ（就地生成通道用，无则 0）
      cnt5   ：GEN_KM 半径内 ≥GEN_DBZ 的像素数（成片判据）
      dist35 ：最近 ≥GEN_DBZ 像素的距离 km（就地生成文案用）

    ★ 为什么要多算一个 dist40（2026-10-01 补）：
      门槛 35 < DBZ_ACT 40 时，dist 会被「更近的弱回波」主导，可能远小于 dist40。
      页面 actGate 在「10 km 内峰值未达 40」时还有一条兜底：「≥40 强回波已进入 12 km
      警戒带 → 强度仍有发展空间，照强度档通报」。旧口径 th=45 > 40，dist ≥ dist40 恒成立、
      该分支永不触发；门槛降到 35 后它会被激活，哨兵必须同步具备，否则两边档位会分叉。"""
    search2 = SEARCH_KM * SEARCH_KM
    peak2 = PEAK_KM * PEAK_KM
    gen2 = GEN_KM * GEN_KM
    kmx, kmy = CAL["kmx"], CAL["kmy"]
    out = []
    for (px, py) in points_px:
        best = None      # (d2, bear, deg)
        best40 = None
        best35 = None
        peak = 0
        peak5 = 0
        cnt5 = 0
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
            # v210：就地生成通道——5 km 内 ≥GEN_DBZ 的峰值与像元数；最近 ≥GEN_DBZ（全窗口）
            if dbz >= GEN_DBZ:
                if best35 is None or d2 < best35:
                    best35 = d2
                if d2 <= gen2:
                    if dbz > peak5:
                        peak5 = dbz
                    cnt5 += 1
        d40 = None if best40 is None else math.sqrt(best40)
        d35 = None if best35 is None else math.sqrt(best35)
        if best is None:
            out.append((None, None, None, peak, d40, peak5, cnt5, d35))
        else:
            out.append((math.sqrt(best[0]), best[1], best[2], peak, d40, peak5, cnt5, d35))
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
    jump = False
    for i in range(1, n):
        dt = pts[i][0] - pts[i - 1][0]
        if dt > 0:
            rate = (pts[i][1] - pts[i - 1][1]) / dt * 3600000.0
            pair.append(rate)
            step.append(abs(pts[i][1] - pts[i - 1][1]))
            # 帧间跳变上限（2026-10-02 补，与页面 trendFrom 同源）：
            # 单帧间隔内距离变化对应的速率超过 V_MAX ⇒ 物理上不可能是同一片回波在移动，
            # 只能是「换了观测目标」（杂波闪现、回波生消换团、最近团切换）。
            # 实测事故：牡丹国际大酒店 25.61 → 2.90 km（6 分钟内 22.71 km ≈ 227 km/h），
            # 最小二乘把它摊薄成 -60 km/h 的「明确逼近」并跑去外推「已抵达」→ 误报。
            if abs(rate) > V_MAX:
                jump = True
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
    if jump:
        # 物理上不可能的跳变 ⇒ 趋势不可判（v=None，绝不用于外推）
        v = None
        stall = "jump"
    elif abs(net) < MOVE_MIN_KM:
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
      ①b 帧间跳变（单帧距离变化对应速率 > V_MAX → 物理上不可能，换了观测目标）
      ② 位移死区（|净位移| < MOVE_MIN_KM → 停滞，既不逼近也不远离）
      ②b 净位移置信（|净位移| < 2×帧间抖动中位数 → 原地生消／逐帧跳目标，非整体位移）
      ③ 速率离散度 MAD（抖动过大不采信）
      ④ 速率方向（±V_NEAR / ±V_AWAY 三条线）
      ⑤ 方位一致性（确认是「同一团在移动」而非外沿形变／换团）
    """
    if v is not None and abs(v) > V_MAX:
        return False, "移速超出可信范围（%.0f km/h > %.0f），趋势暂不可判" % (abs(v), V_MAX)
    if stall == "jump":
        return False, ("窗口内出现物理上不可能的帧间跳变（单帧距离变化对应速率超过 %.0f km/h），"
                       "系杂波闪现或最近回波换目标，趋势不可判" % V_MAX)
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
      ① 最新帧（或次新帧，v184 容错）10 km 内峰值 ≥ DBZ_ACT
      ② 最新帧（或次新帧，v184 容错）最近 ≥DBZ_ACT 强回波 ≤ IMPACT_KM
      ③ 窗口内 ≥ IMPACT_MIN_FRAMES 帧同时满足①②
      ④ 径向趋势未达明确远离（v < V_AWAY；正在离开＝影响趋于结束，不触发）
    """
    qual = 0
    for _m in metrics:          # v210：元组扩为 8 元（尾部加 peak5/cnt5/dist35），改为按位取
        pk, d40 = _m[3], _m[4]
        if pk and pk >= DBZ_ACT and d40 is not None and d40 <= IMPACT_KM:
            qual += 1
    # v184 漏报修复（2026-10-05 斌哥，与页面 termImpact 同源）：单帧抖动不清零——
    # 暴雨正下时雷达拼图任一帧数据抖动（40dBZ 核瞬间掉出 5km），原「最新帧必须满足」
    # 口径会整段清零、推送从预警跳回正常。改为：最新帧不满足但次新帧满足仍视为影响中；
    # 连续两帧（≈12 分钟）不贴身才判影响结束。
    last = metrics[-1]
    last_ok = bool(last[3] and last[3] >= DBZ_ACT and last[4] is not None and last[4] <= IMPACT_KM)
    prev_ok = False
    if len(metrics) >= 2:
        prev = metrics[-2]
        prev_ok = bool(prev[3] and prev[3] >= DBZ_ACT and prev[4] is not None and prev[4] <= IMPACT_KM)
    away = (v is not None and v >= V_AWAY)
    return ((last_ok or prev_ok) and qual >= IMPACT_MIN_FRAMES and not away), qual


def genesis_scan(point_mets, st, now, times):
    """「就地生成·单帧即时」事件状态机（v210，与页面 genScan 同源）。

    point_mets: {点位名: [逐帧 metrics 元组...]}（时间升序）
    返回 {点位名: 证据 dict}；同时维护 st['gen']（进行中事件）与 st['gen_cool']（冷却打点）。

    证据 ok 的三个必要条件（缺一不可）：
      ① 本帧 GEN_KM 内 ≥GEN_DBZ 成片（cnt5 ≥ GEN_MIN_CELLS）——防孤立杂波／生物回波／超折射；
      ② 与**上一帧**比对确认「新生」（上一帧同处 5 km 内无 ≥GEN_DBZ 回波）；
         移进来的（上一帧已有）归「明确逼近／实况影响」通道，本通道不发，避免重复计；
         无上一帧可比（首轮/缺帧）→ 不发（宁漏不错）。
      ③ 未被判「明确远离」（由 judge_point 的径向趋势另行把关，这里只在解除时用）。
    静默（quiet）：35~39 dBZ 一律静默（只上页面）；同档持续不重复响；冷却期内不响。
    """
    evs = st.get("gen") or {}
    cool = st.get("gen_cool") or {}
    evidence = {}
    for name, mets in point_mets.items():
        if not mets:
            continue
        last = mets[-1]
        prev = mets[-2] if len(mets) >= 2 else None
        peak5 = last[5] or 0
        cnt5 = last[6] or 0
        d35 = last[7]
        cur = (peak5 >= GEN_DBZ and cnt5 >= GEN_MIN_CELLS)
        ev = evs.get(name)
        if cur:
            lv = tier_of(peak5) if peak5 >= GEN_PUSH_DBZ else 1
            kind = "ongoing"
            if ev:
                ev["frames"] = int(ev.get("frames", 1)) + 1
                ev["peak"] = max(int(ev.get("peak", 0)), int(peak5))
                ev["lv"] = max(int(ev.get("lv", 1)), lv)
                ev["miss"] = 0
            else:
                prev_peak5 = (prev[5] or 0) if prev else None
                if prev is None:
                    kind = "nohist"
                elif prev_peak5 >= GEN_DBZ:
                    kind = "moved"
                else:
                    kind = "new"
                if kind == "new":
                    ev = {"t0": times[-1], "peak": int(peak5), "lv": lv, "lv_pushed": 0,
                          "frames": 1, "miss": 0}
                    evs[name] = ev
            if ev:
                quiet = (peak5 < GEN_PUSH_DBZ
                         or (kind == "ongoing" and lv <= int(ev.get("lv_pushed", 0)))
                         or (now - float(cool.get(name, 0)) < GEN_COOLDOWN_S))
                if peak5 >= GEN_PUSH_DBZ and not quiet:
                    ev["lv_pushed"] = max(int(ev.get("lv_pushed", 0)), lv)
                evidence[name] = {"ok": True, "kind": kind, "peak": int(peak5), "cells": cnt5,
                                  "lv": int(ev.get("lv", lv)), "quiet": bool(quiet),
                                  "cool": bool(now - float(cool.get(name, 0)) < GEN_COOLDOWN_S),
                                  "d": d35, "frames": int(ev.get("frames", 1)),
                                  "dur": int(max(0, (times[-1] - ev["t0"]) / 60000.0))}
            else:
                # 有回波但不属本通道（移进来 / 无上一帧可比）：留痕便于排查，ok=False 不放行
                evidence[name] = {"ok": False, "kind": kind, "peak": int(peak5), "cells": cnt5,
                                  "cool": bool(now - float(cool.get(name, 0)) < GEN_COOLDOWN_S)}
        elif ev:
            ev["miss"] = int(ev.get("miss", 0)) + 1
            # 「明确远离」提前解除（与页面 genScan 同源）：径向趋势 ≥ V_AWAY 即认为威胁已解除，
            # 不必等满 GEN_RETRACT_FRAMES 帧（回波真的走了就该立刻收档）
            try:
                _v, _net, _con, _mad, _stall = motion_of(times, [m[0] for m in mets])
                away = (_v is not None and _v >= V_AWAY)
            except Exception:
                away = False
            if ev["miss"] >= GEN_RETRACT_FRAMES or away:
                cool[name] = now          # 冷却打点：同点位 30 分钟内不再因就地生成响铃
                dur = int(max(1, (times[-1] - ev["t0"]) / 60000.0))
                log("就地生成解除：%s 周边 5 km 内新生的强回波未持续（历时约 %d 分钟，已推送=%s）"
                    % (name, dur, "是" if int(ev.get("lv_pushed", 0)) > 0 else "否"))
                del evs[name]
    st["gen"] = evs
    st["gen_cool"] = cool
    return evidence


def judge_point(times, metrics, lag_min, gen=None):
    """单个监测点定级。metrics: 逐帧 (dist, bear, deg, peak10, dist40, peak5, cnt5, dist35)，时间升序。
    gen：本点位的「就地生成」证据（genesis_scan 产出，可为 None）。
    返回 dict(sev, why, dist, bear, deg, v, con, peak, arrived, gen, quiet)"""
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

    # v210：冷却期内（同点位 30 分钟内刚解除过就地生成事件）→ 本轮无论由哪条通道放行，
    # 一律静默（只上页面、不推送），防止「解除后马上又响」把用户吵烦。升级/新过程不受影响。
    base = {"dist": d_obs, "bear": bear, "deg": deg, "v": v, "con": con, "stall": stall,
            "peak": peak10, "arrived": arrived, "d_now": d_now, "d40": d40,
            "gen": False, "quiet": bool(gen and gen.get("cool"))}

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
            # ★★ 就地生成·单帧即时通道（v210，与页面 judgeTerm 同源）：5 km 内原生成的成片回波，
            #    没有位移可判、也等不到 2 帧确认（雷达图自身还滞后 6~12 分钟）→ 单帧即发布初判。
            if gen and gen.get("ok"):
                gd = ("%.1f km" % gen["d"]) if gen.get("d") is not None else ("≤%.0f km" % GEN_KM)
                base.update(sev=int(gen["lv"]), gen=True, quiet=bool(gen.get("quiet")),
                            why="监测对象周边 %.0f km 内「就地生成」 ≥%d dBZ 强回波（最近 %s，峰值 %d dBZ，成片 %d 格点，%s）"
                                "——周边环境已具威胁，按「先发布后校准」立即发布初判；若后续帧未持续或明确远离会自动解除"
                                % (GEN_KM, GEN_DBZ, gd, gen["peak"], gen["cells"],
                                   ("已持续 %d 帧" % gen["frames"]) if gen["frames"] > 1 else "单帧初判"))
                return base
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
            how = ("按滞留实况判级（未证实明确逼近、亦非远离）" if (impacted or arrived) else ap_why)
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


# ==================================================================
# 息屏提醒 · Web Push（RFC8291 加密 + RFC8292 VAPID，纯标准库，无第三方依赖）
# ==================================================================
def _b64u_decode(s):
    import base64
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s)


def _b64u_encode(b):
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _vapid_jwt(aud):
    """RFC8292 VAPID 签名：header.claims 用 ES256（P-256）签名。返回 Authorization 头值。
    v203.2 修正（推送服务会拒的三个坑）：
      ① JWT 头必须带 jwk（推送服务靠它取公钥验签，缺了 401/403）；
      ② cryptography 的 sign() 返回 DER 编码签名，必须 decode_dss_signature 转裸 r||s（32+32B），
         v203 直接 sig[:32]/sig[32:] 切 DER 是错的；
      ③ jwk 的 x/y 用裸 32 字节 base64url（不带 0x04 前缀）。"""
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    now = int(time.time())
    def _enc(o):
        return _b64u_encode(json.dumps(o, separators=(",", ":")).encode("utf-8"))
    key = serialization.load_der_private_key(_b64u_decode(VAPID_PRIV_KEY), password=None)
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    hdr = {"typ": "JWT", "alg": "ES256",
           "jwk": {"kty": "EC", "crv": "P-256",
                   "x": _b64u_encode(pub_raw[1:33]), "y": _b64u_encode(pub_raw[33:65])}}
    claims = {"aud": aud, "exp": now + 12 * 3600, "sub": VAPID_SUBJECT}
    signing_input = (_enc(hdr) + "." + _enc(claims)).encode("ascii")
    der_sig = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)      # DER → 裸 r||s（JOSE 要求）
    sig_b = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    # JWT 三段式 header.payload.signature；k= 为裸 65 字节公钥（0x04 前缀）base64url（RFC8292）
    return "vapid t=%s.%s.%s,k=%s" % (_enc(hdr), _enc(claims), _b64u_encode(sig_b), _b64u_encode(pub_raw))


def _webpush_encrypt(payload_b, sub):
    """RFC8291/aes128gcm 加密。sub: {endpoint, keys:{p256dh, auth}}。返回 (body, headers)。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import serialization
    import hashlib

    # 一次性本地 ECDH 密钥对（P-256）
    server_key = ec.generate_private_key(ec.SECP256R1())
    ua_pub_b = _b64u_decode(sub["keys"]["p256dh"])
    # RFC8291 §2：p256dh 应为「不含 0x04 前缀」的 64 字节，但 Apple（web.push.apple.com）的 Safari
    # 给的是含 0x04 前缀的 65 字节。这里统一归一到「无前缀 64 字节」再补前缀，兼容两端。
    if len(ua_pub_b) == 65 and ua_pub_b[0] == 0x04:
        ua_pub_b = ua_pub_b[1:]
    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b"\x04" + ua_pub_b)          # 用户公钥是未压缩点，需补 0x04 前缀
    server_pub_b = server_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)[1:]
    shared = server_key.exchange(ec.ECDH(), ua_public)

    auth_secret = _b64u_decode(sub["keys"]["auth"])
    # HKDF 派生（RFC8291 §3.3）
    def hkdf(salt, ikm, info):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(ikm)

    info = b"WebPush: info\x00" + ua_pub_b + b"\x04" + server_pub_b
    ikm = hkdf(auth_secret, shared, info)             # IKM
    prk = hkdf(auth_secret, ikm, b"Content-Encoding: auth\x00")
    cek = hkdf(prk, b"", b"Content-Encoding: aes128gcm\x00")
    nonce = hkdf(prk, b"", b"Content-Encoding: nonce\x00")[:12]

    salt = os.urandom(16)
    aesgcm = AESGCM(cek)
    record = aesgcm.encrypt(nonce, payload_b, salt + b"\x00\x00\x00\x00")
    headers = {
        "TTL": "86400",
        "Content-Encoding": "aes128gcm",
        "Encryption": "salt=%s" % _b64u_encode(salt),
        "Crypto-Key": "dh=%s" % _b64u_encode(server_pub_b),
        "Content-Type": "application/octet-stream",
    }
    return record, headers


def push_webpush(sub, title, body, sev):
    """向某订阅者的 Web Push 端点发一条系统通知。sub 需含 push 字段。
    返回 'ok' 或抛异常；未配置私钥/无 push 订阅时静默跳过（返回 None）。"""
    if not VAPID_PRIV_KEY:
        return None
    ps = sub.get("push")
    if not ps or not ps.get("endpoint") or not ps.get("keys"):
        return None
    try:
        # v207（斌哥 2026-10-05）：iOS 上 SW push 事件不可靠 → 改用声明式 Web Push（web_push:8030，
        # iOS 18.4+ 系统直接显示通知，不启动 SW，彻底绕开 push 事件不触发的坑；silent:false 播默认提示音）。
        # 同时保留顶层旧字段三重兜底：老浏览器/安卓走 SW push 事件读 notification.*；更老的 SW 读顶层 title/body。
        _url = "https://xiamen-nowcast-warning.app.workbuddy.host/"
        _tag = str(int(time.time() // 60))
        payload = {
            "web_push": 8030,
            "notification": {
                "title": title, "body": body, "navigate": _url,
                "tag": _tag, "silent": False,
                "icon": "https://xiamen-nowcast-warning.app.workbuddy.host/icon-192.png",
            },
            "title": title, "body": body, "url": _url, "sev": sev, "tag": _tag,
        }
        data, hd = _webpush_encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"), ps)
        aud = "https://" + urllib.parse.urlparse(ps["endpoint"]).netloc
        hd["Authorization"] = _vapid_jwt(aud)
        r = http(ps["endpoint"], data, hd, 15)
        return r
    except Exception as e:
        # 端点失效（如用户关闭通知/卸载）时上报 404/410，交由上层决定是否清理
        log("Web Push 发送失败（%s）：%s" % (sub.get("id") or "?", e))
        return None


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
                       "push": rec.get("push") or None,
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
        if r.get("quiet"):      # v210：就地生成静默档（35~39 dBZ）只上页面，不进个人推送
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
            lst = "、".join("「%s」（原 %s）" % (p, SEV_NAMES[s]) for p, s in pts)
            tail = "已回到正常" if len(pts) == 1 else "已全部回到正常"
            body = "%s%s。\n\n帧时间（北京）：%s（约 %.0f 分钟前）" % (lst, tail, bj(frame_ms), lag_min)
            title = "✅ 厦门短临 · 预警解除" + ((" · " + pts[0][0]) if len(pts) == 1 else "")
        else:
            title = "✅ 厦门短临 · 预警解除"
            body = ("所有监测对象已回到正常（原 %s）。\n\n帧时间（北京）：%s（约 %.0f 分钟前）"
                    % (SEV_NAMES[last_sev], bj(frame_ms), lag_min))
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
    if lag_min > LAG_MAX_MIN:
        lines.append("⚠️ 雷达拼图延迟较大（约 %.0f 分钟），本轮未做时间外推，请结合实况判断" % lag_min)
    lines += ["", "帧时间（北京）：%s（约 %.0f 分钟前）" % (bj(frame_ms), lag_min),
              "触发时刻（北京）：%s" % now_bj().strftime("%Y-%m-%d %H:%M:%S")]
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
    if lag_min > LAG_MAX_MIN:
        body = (body + "\n⚠️ 雷达拼图延迟较大（约 %.0f 分钟），实际距离可能比图上更近" % lag_min)
    return title, body


# ==================================================================
# 九、检验闭环（预测 vs 实况）
# ==================================================================
VERIFY_JSONL = "verify.jsonl"     # 判定流水（一行一条 JSON），供 verify_closure.py 回验打分
VERIFY_NEAR_KM = 10.0             # 记录门槛：最近回波进入此距离即纳入回验（无论是否触发）
VERIFY_KEEP_DAYS = 30             # 流水保留天数（超期自动裁剪）


def verify_record(results, frame_ms, lag_min, push_kinds=None, path=None):
    """★ 检验闭环（2026-10-02 立）：把「值得回验」的判定逐条落盘。

    记什么（这是「最少但够用」的集合，不记则无法回答准确率，多记则日志爆炸）：
      · 任何 sev ≥ 1 的点位 —— 报了就要验：后来真影响了吗？提前量多少？
      · 任何 sev = 0 但最近回波已进入 VERIFY_NEAR_KM 的点位 —— 没报也要验：是不是漏报？
    「连回波都没靠近」的轮次一律不记（那不可能构成漏报）。

    一行一条 JSON；(frame_ms, point) 唯一，重复运行不会重复计数。
    """
    rows = []
    kinds = push_kinds or {}
    for r in results:
        nm = r.get("point")
        d_near = r.get("d40") if r.get("d40") is not None else r.get("dist")
        if r["sev"] < 1 and not (d_near is not None and d_near <= VERIFY_NEAR_KM):
            continue
        rows.append({
            "frame_ms": frame_ms, "frame": bj(frame_ms), "lag": round(lag_min, 1),
            "ts": now_bj().strftime("%Y-%m-%d %H:%M:%S"),
            "point": nm, "sev": r["sev"],
            "dist": round(r["dist"], 2) if r.get("dist") is not None else None,
            "d40": round(r["d40"], 2) if r.get("d40") is not None else None,
            "peak": r.get("peak"),
            "v": round(r["v"], 2) if r.get("v") is not None else None,
            "stall": r.get("stall"), "arrived": bool(r.get("arrived")),
            "push": kinds.get(nm) or [],
            "why": (r.get("why") or "")[:200],
        })
    if not rows:
        return 0
    p = path or os.path.join(BASE_DIR, VERIFY_JSONL)
    seen = set()
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                seen.add((o.get("frame_ms"), o.get("point")))
    except FileNotFoundError:
        pass
    n = 0
    with open(p, "a", encoding="utf-8") as f:
        for o in rows:
            if (o["frame_ms"], o["point"]) in seen:
                continue
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
            n += 1
    _trim_verify(p)
    return n


def _trim_verify(path, days=VERIFY_KEEP_DAYS):
    """裁剪流水：只保留最近 days 天。文件不大时开销可忽略，避免无限膨胀。"""
    try:
        cutoff = (now_bj() - timedelta(days=days)).strftime("%Y-%m-%d")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return
        first = lines[0].strip()
        if not first:
            return
        try:
            if json.loads(first).get("ts", "")[:10] >= cutoff:
                return          # 最老的一条都还在保留期内 → 不必重写
        except Exception:
            return
        keep = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            try:
                if json.loads(s).get("ts", "")[:10] >= cutoff:
                    keep.append(line if line.endswith("\n") else line + "\n")
            except Exception:
                continue
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except Exception:
        pass


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
                % now_bj().strftime("%Y-%m-%d %H:%M:%S"))
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

    # 图龄＝最新可用帧时刻与此刻之差（NMC 拼图发布延迟，通常 10~15 分钟）。
    # ★ 展示用真实图龄（lag_min）；外推用 ex_lag：超过 LAG_MAX_MIN 视为数据陈旧 → 归零（绝不外推）。
    #   旧实现把 lag_min 直接截断到 20 再拿去外推，等于「用 20 分钟的旧帧推当前位置」，与注释相反。
    age_min = max(0.0, (time.time() * 1000.0 - times[-1]) / 60000.0)
    lag_min = age_min
    ex_lag = age_min if age_min <= LAG_MAX_MIN else 0.0
    if age_min > LAG_MAX_MIN:
        log("雷达图龄 %.0f 分钟偏大（> %.0f），本轮不做时间外推（抵达判定只看实况帧）"
            % (age_min, LAG_MAX_MIN))

    # ---- 就地生成通道（v210）：状态提前读出（事件表要跨轮存续），逐点算证据，再逐点定级 ----
    st = load_state()
    now = time.time()
    point_mets = {}
    for i, (name, _lat, _lon) in enumerate(points):
        point_mets.setdefault(name, [fm[i] for fm in metrics_by_frame])
    gen_by_point = genesis_scan(point_mets, st, now, times)
    if gen_by_point:
        log("就地生成证据：%s" % "；".join(
            "%s %s dBZ/成片 %d 格点/%s" % (n, g["peak"], g["cells"],
                                            ("静默" if g["quiet"] else "发布"))
            for n, g in gen_by_point.items()))

    # ---- 逐点定级，取全局最紧急 ----
    results = []
    for i, (name, _lat, _lon) in enumerate(points):
        mets = [fm[i] for fm in metrics_by_frame]
        r = judge_point(times, mets, ex_lag, gen_by_point.get(name))   # 冷却静默由 judge_point 内部处理
        r["point"] = name
        results.append(r)

    sev = 0
    best = None
    for r in results:
        # v210：就地生成的静默档（35~39 dBZ）只上页面、不驱动任何推送 —— 推送判级一律跳过
        if r.get("quiet"):
            continue
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

    # ---- 推送决策 ----（st / now 已在上面就地生成扫描前读好，此处不再重复 load_state）
    last_sev = int(st.get("sev", 0) or 0)
    last_push = float(st.get("last_push", 0) or 0)
    since = float(st.get("since", 0) or 0)

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
    push_kinds = {}          # {点位: [推送类型...]} —— 供检验流水标注「这条判定当时推了什么」
    pushed = False
    if kind and GLOBAL_PUSH and not force_sev:      # 模拟模式只验个人通道，不惊动全局微信
        title, body = build_message(kind, sev, best, lag_min, times[-1], last_sev,
                                    clear_pts=prev_active if kind == "解除" else None)
        res = notify_all(title, body, sev, dry=dry)
        okn = sum(1 for _, ok, _ in res if ok)
        log("全局推送【%s】%s | %s%s" % (kind, title, "; ".join("%s:%s" % (n, "OK" if o else m)
                                                              for n, o, m in res),
                                        "（DRY，未实发）" if dry else ""))
        pushed = okn > 0
        if pushed:
            last_push = now
            if best.get("point"):
                push_kinds.setdefault(best["point"], []).append(kind)
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
        has_push = bool(sub.get("push") and sub["push"].get("endpoint"))
        if not topic and not has_push:
            continue
        # v205（斌哥 2026-10-05）：只开「息屏提醒」、没订阅任何点位的用户（rules 为空但有 push），
        # 让他跟随「全体观测点取最紧急」的同一口径（sev/best），否则 subscriber_sev 恒返 0、
        # decide_kind 恒 None，息屏提醒对不订阅的人（如不懂操作的父母）等于摆设。
        if not sub.get("rules") and has_push:
            s_sev, s_best, s_active = sev, best, prev_active
        else:
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
                print("--- DRY RUN · %s（%s）---" % (who, topic or "仅息屏"))
                print(title)
                print(body)
            else:
                try:
                    if topic:
                        push_ntfy_topic(topic, title, body, s_sev)
                        p_push = now
                except Exception as e:
                    log("订阅者 %s 推送失败：%s" % (who, e))
                # v203：息屏提醒——该订阅者若开了 Web Push，额外发一条系统通知（锁屏弹横幅+响铃）
                try:
                    push_webpush(sub, title, body, s_sev)
                except Exception as e:
                    log("订阅者 %s Web Push 失败：%s" % (who, e))
                if has_push:
                    p_push = now
            _pb = (s_best or best).get("point")
            if _pb:
                push_kinds.setdefault(_pb, []).append("订阅-" + s_kind)
            log("订阅者推送【%s】%s → %s：%s%s"
                % (s_kind, who, topic or "仅息屏", title, "（DRY，未实发）" if dry else ""))
        new_subs[sid] = {"sev": s_sev,
                         "last_push": p_push,
                         "since": (now if s_sev != p_sev else float(prev.get("since", 0) or now)),
                         "nick": who,
                         # 该订阅者名下当前处于预警的点位快照：下轮「解除」时点名用
                         "active": s_active}

    if force_sev:
        return 0                    # 模拟只验通道，绝不把假档位写进状态（否则下一轮会误推「解除」）

    # ---------- ③ 5 km 临近确认 ----------    # 分工：10 km 首报抢提前量（15~25 分钟），5 km 确认告诉用户「不是预测、马上就到」（6~10 分钟）。
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
        push_kinds.setdefault(r["point"], []).append("临近确认")
        log("临近确认【%s】%s | %s" % (r["point"], title, "; ".join("%s:%s" % (n, "OK" if o else m)
                                                                  for n, o, m in res)))
        if dry:
            print("--- DRY RUN · 临近确认 ---")
            print(title)
            print(body)
        for sid, sub in subs.items():     # 订阅了该点位且起始档够得着当前档的人，同样收到确认
            topic = sub.get("topic")
            has_push = bool(sub.get("push") and sub["push"].get("endpoint"))
            if not topic and not has_push:
                continue
            try:
                floor = int((sub.get("rules") or {}).get(r["point"], 99))
            except Exception:
                floor = 99
            # v205：只开息屏提醒、没订阅点位的人（rules 为空）跟随全体口径，临近确认同样推送
            if not sub.get("rules") and has_push:
                floor = 0
            if floor > r["sev"]:
                continue
            if dry:
                print("--- DRY RUN · 临近确认 · %s ---" % (sub.get("nick") or sid))
                print(title)
                print(body)
                continue
            try:
                if topic:
                    push_ntfy_topic(topic, title, body, r["sev"])
            except Exception as e:
                log("订阅者 %s 临近确认推送失败：%s" % (sub.get("nick") or sid, e))
            try:
                push_webpush(sub, title, body, r["sev"])
            except Exception as e:
                log("订阅者 %s 临近确认 Web Push 失败：%s" % (sub.get("nick") or sid, e))
        log("订阅者临近确认【%s】已投递%s" % (r["point"], "（DRY，未实发）" if dry else ""))

    if dry:
        # ★ --dry 是「只看会发生什么」，绝不能落盘：状态一旦被写，下一轮就认为
        #   「解除/档位变化已经推过了」，真实推送被静默吞掉（2026-10-02 实际踩到过一次）。
        log("DRY 运行：不发送、不写状态（冻结推送节奏，避免吞掉下一轮真实推送）")
        return 0

    # ---------- ④ 检验流水（预测 vs 实况，供 verify_closure.py 打分）----------
    try:
        nv = verify_record(results, times[-1], lag_min, push_kinds)
        if nv:
            log("检验流水：本轮记录 %d 条（累计可回验）" % nv)
    except Exception as e:
        log("检验流水写入失败：%s" % e)

    # ---------- ⑤ 地面降水实况采样（回验判分的 ground truth）----------
    # 为什么要采：verify_closure 原先只能「用雷达检验雷达」，查不出「雷达看到回波、
    # 地面却滴雨未下」的空报（10-02 牡丹国际大酒店就是实例）。地面实况是独立真值。
    # 额度自适应：有预警点位时 6 分钟一采（验证空报），平时 30 分钟一采（抓漏报）。
    # 必须放在 dry 的 return 之后 —— 干跑不该消耗第三方 API 额度。
    try:
        import precip_truth as PT
        active_n = sum(1 for r in results if r["sev"] > 0)
        npt = PT.sample(list(TERMS) + list(CITIES), active_count=active_n)
        if npt:
            log("地面实况：采样 %d 条（源 %s，点位活跃 %d）" % (npt, PT.pick_source(), active_n))
    except Exception as e:
        log("地面实况采样失败（不影响预警）：%s" % e)

    save_state({
        "sev": sev, "name": SEV_NAMES[sev],
        "point": best.get("point") or "",
        "dist": best.get("dist"),
        # 全局预警点位快照：下一轮「解除」推送要点名「哪个/哪些点位解除了」
        "active": {r["point"]: r["sev"] for r in results if r["sev"] > 0},
        "since": since,
        "last_push": last_push,
        "frame": bj(times[-1]),
        "checked": now_bj().strftime("%Y-%m-%d %H:%M:%S"),
        "subs": new_subs,
        # 5 km 临近确认标记：{点位: bool}，True＝本预警过程已确认过，出圈/降档后复位
        "near5": near_new,
        # v210：就地生成事件表＋冷却表（不落盘＝每轮重修，事件「持续帧数」与冷却都会失效）
        "gen": st.get("gen") or {},
        "gen_cool": st.get("gen_cool") or {},
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
