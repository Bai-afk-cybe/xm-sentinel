#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地面降水实况（ground truth）采集与查询。

为什么必须接
------------
`verify_closure` 原先只用「雷达自证」判分：10 km 内峰值 ≥40 dBZ 或最近 ≥40 dBZ 回波
≤10 km，就认定「发生了影响」。但**雷达看到回波 ≠ 地面真的下到雨**——
2026-10-02 10:18 牡丹国际大酒店被判「较高预警」，事后核对那一刻地面**基本无降水**
（Open-Meteo 模式小时雨量 0.0~0.1 mm，仅最微量级；用户亦确认真实未下雨）。
用雷达检验雷达，这种空报永远查不出来。要回答「报了到底准不准」，必须引入
**与雷达相互独立的地面降水证据**。

数据源（自动选择，优先级从高到低；也可用 XM_TRUTH_SOURCE 强制指定）
------------------------------------------------------------------
1. `caiyun`    彩云天气 realtime —— 雷达 + 地面站融合反演，**1 km / 分钟级**，
               最贴近「这个泊位到底下没下雨」。
               GET https://api.caiyunapp.com/v2.6/{token}/{lng},{lat}/realtime
               → result.realtime.precipitation.local.intensity（mm/h）
               启用：`XM_CAIYUN_TOKEN`
2. `qweather`  和风天气 v7 实时 —— now.precip（mm，当小时累计）。1 km，支持多坐标。
               启用：`XM_QWEATHER_KEY`（可选 `XM_QWEATHER_HOST`，默认 devapi.qweather.com）
3. `openmeteo` Open-Meteo —— **无需任何 key**，一次请求即可取全部点位，且可回溯过去数小时。
               ★ 局限（实测）：它返回的是**数值模式值，不是站点实测**。网格中心约 0.05°
               （~5~11 km，接口对格点做了插值，故相邻点位仍可给出不同值——10-01 09:00
               实测港区各点分别为 3.2 / 2.6 / 1.5 / 0.3 mm/h）。但它对小尺度对流会明显偏干、
               落区平滑，**不足以判定「这个泊位到底下没下雨」**。
               定位＝零配置兜底 + 历史补采，可作辅助证据，**不可单独作为最终真值**。

采样策略（额度自适应，把额度花在刀刃上）
--------------------------------------
· 无预警在身：每 IDLE_MIN 分钟采一轮 —— 「漏报」的对照组（没报的时候地面到底下没下）
· 有预警/关注在身：每 ACTIVE_MIN 分钟采一轮 —— 「空报」的验证组（报了的时候地面有没有下）
彩云免费额度约 1000 次/天，13 个点位按上表约 620~830 次/天，留有余量。

落盘
----
`truth.jsonl`，一行一条：
    {"ms":…, "ts":"YYYY-MM-DD HH:MM:SS", "pt":"港务大厦", "lat":…, "lng":…,
     "i":0.0, "src":"caiyun", "res_km":1.0, "kind":"now"|"hist"}
`i` 统一折算为 **mm/h 降水强度**（openmeteo 的 15 分钟累计 ×4 折算）。

用法
----
    python precip_truth.py                  # 按自适应策略采一轮（未到间隔则跳过）
    python precip_truth.py --force          # 强制采一轮（不看间隔）
    python precip_truth.py --backfill 48    # 用 openmeteo 回溯补最近 48 小时
    python precip_truth.py --status         # 显示当前源、覆盖率、最近采样
"""
import os
import sys
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.abspath(__file__))
TRUTH_JSONL = os.path.join(BASE, "truth.jsonl")
TRUTH_META = os.path.join(BASE, ".truth_meta.json")
TRUTH_KEEP_DAYS = 60          # 流水保留天数

IDLE_MIN = 30.0               # 无预警时采样间隔（分钟）
ACTIVE_MIN = 6.0              # 有预警/关注点位时采样间隔（分钟）
HIST_BACKFILL_HOURS = 48      # 默认回溯小时数

CAIYUN_MIN_INTERVAL = float(os.environ.get("XM_CAIYUN_MIN_INTERVAL", "1.2"))  # 彩云逐点请求最小间隔(s)
CAIYUN_RETRY = int(os.environ.get("XM_CAIYUN_RETRY", "3"))                    # 彩云 429 退避重试次数

CST = timezone(timedelta(hours=8))

# 各源空间分辨率（km），用于在报告里措辞：能不能代表「这个泊位」
RES_KM = {"caiyun": 1.0, "qweather": 1.0, "openmeteo": 5.0}
SRC_LABEL = {"caiyun": "彩云（雷达+地面站融合，1km，实测级）",
             "qweather": "和风（1km，实测级）",
             "openmeteo": "Open-Meteo（模式背景场，网格~5-11km，非实测）"}
# 是否属于「站点级实测真值」（用于报告措辞：能不能一口断定「这点位没下雨」）
IS_MEASURED = {"caiyun": True, "qweather": True, "openmeteo": False}

# 「这一时刻算有雨吗」的门槛（mm/h）。0.5 mm/h 约等于细密小雨。
RAIN_MIN_MM_H = 0.5

# 彩云 nearest 与哨兵自算最近回波距离的允许差（km）。见 nearest_vs_radar 的口径说明。
NEAREST_TOL_KM = float(os.environ.get("XM_NEAREST_TOL_KM", "5.0"))


# ------------------------------------------------------------------
# 源探测与 HTTP
# ------------------------------------------------------------------
def pick_source(explicit=None):
    """自动选择可用数据源。"""
    if explicit:
        return explicit
    env = (os.environ.get("XM_TRUTH_SOURCE") or "").strip().lower()
    if env in RES_KM:
        return env
    if os.environ.get("XM_CAIYUN_TOKEN"):
        return "caiyun"
    if os.environ.get("XM_QWEATHER_KEY"):
        return "qweather"
    return "openmeteo"


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; xm-sentinel/3.0)",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _now_bj():
    return datetime.now(CST)


# ------------------------------------------------------------------
# 各源取数：统一返回 {点索引: mm/h}
# ------------------------------------------------------------------
def _caiyun_one(token, lng, lat, timeout=20):
    """取单点彩云实况。返回 {i, nb, ni}：
         i  本点降水强度 mm/h（precipitation.local.intensity）
         nb 最近降水云团距离 km（precipitation.nearest.distance，可 None）
         ni 该最近云团的强度 mm/h（nearest.intensity，可 None）
       遇 HTTP 429 按 3/6/9 s 退避重试。

    ★ nb 的用途（2026-10-02 标定）：它是**独立于 NMC 的**「最近降雨有多远」，
      能与哨兵自算的 dist/dist40 互为旁证。实测 13 点位榜（`_probe_caiyun_dist.py`）：
      nb 与 dist40 差 −1.7~+1.8 km、均值 +0.75 km，与 dist(35 dBZ) 差约 +2.1 km ⇒
      彩云的降水门槛大致落在我们 35~40 dBZ 之间，**排序完全一致**（都认出九节礁最远、
      远海/新海达最近）。"""
    url = "https://api.caiyunapp.com/v2.6/%s/%s,%s/realtime" % (
        urllib.parse.quote(token), lng, lat)
    last = None
    for k in range(CAIYUN_RETRY):
        try:
            js = json.loads(_get(url, timeout))
            rt = (js.get("result") or {}).get("realtime") or {}
            pr = rt.get("precipitation") or {}
            lo = pr.get("local") or {}
            ne = pr.get("nearest") or {}
            v = lo.get("intensity")
            out = {"i": float(v) if v is not None else 0.0}
            for key, src in (("nb", "distance"), ("ni", "intensity")):
                x = ne.get(src)
                out[key] = round(float(x), 2) if x is not None else None
            return out
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(3.0 * (k + 1))
                continue
            raise
    raise last if last else RuntimeError("彩云取数失败")


def fetch_caiyun(points, token=None, timeout=20):
    """彩云 realtime：逐点请求（该接口不支持多坐标）。返回 [{pt,i,...}]。

    ⚠️ 免费版有**突发频率限制**：实测两次背靠背请求，第 2 次即 429
    （间隔 1.5 s 则连续 4 次全部 200）。故此处必须 ① 请求间最小间隔
    `CAIYUN_MIN_INTERVAL` ② 遇 429 退避重试 `CAIYUN_RETRY` 次。
    13 个点位按 1.2 s 间隔约 16 s，可接受。
    """
    token = token or os.environ.get("XM_CAIYUN_TOKEN", "")
    if not token:
        raise RuntimeError("缺 XM_CAIYUN_TOKEN")
    out = []
    for k, (name, lat, lng) in enumerate(points):
        if k:
            time.sleep(CAIYUN_MIN_INTERVAL)
        try:
            o = _caiyun_one(token, lng, lat, timeout)
        except Exception as e:
            out.append({"pt": name, "lat": lat, "lng": lng, "i": None, "err": str(e)[:120]})
            continue
        out.append({"pt": name, "lat": lat, "lng": lng,
                    "i": o["i"], "nb": o["nb"], "ni": o["ni"]})
    return out


def fetch_qweather(points, key=None, host=None, timeout=20):
    """和风实时：多坐标一次请求（location 用 | 分隔，最多 20 个）。返回 [{pt,i,...}]。"""
    key = key or os.environ.get("XM_QWEATHER_KEY", "")
    if not key:
        raise RuntimeError("缺 XM_QWEATHER_KEY")
    host = host or os.environ.get("XM_QWEATHER_HOST") or "devapi.qweather.com"
    locs = "|".join("%s,%s" % (lng, lat) for _, lat, lng in points[:20])
    url = "https://%s/v7/weather/now?location=%s&key=%s" % (
        host, urllib.parse.quote(locs, safe=",|"), urllib.parse.quote(key))
    js = json.loads(_get(url, timeout))
    if str(js.get("code")) != "200":
        raise RuntimeError("和风返回 code=%s" % js.get("code"))
    arr = js.get("now")
    arr = arr if isinstance(arr, list) else [arr]
    out = []
    for k, (name, lat, lng) in enumerate(points):
        v = None
        if k < len(arr) and isinstance(arr[k], dict):
            try:
                v = float(arr[k].get("precip") or 0.0)
            except Exception:
                v = 0.0
        out.append({"pt": name, "lat": lat, "lng": lng, "i": v})
    return out


def fetch_openmeteo(points, timeout=25):
    """Open-Meteo：全部点位一次请求。current.precipitation 是 15 分钟累计(mm) → ×4 折 mm/h。"""
    lat = ",".join("%.4f" % p[1] for p in points)
    lng = ",".join("%.4f" % p[2] for p in points)
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=precipitation&timezone=Asia%%2FShanghai" % (lat, lng))
    js = json.loads(_get(url, timeout))
    arr = js if isinstance(js, list) else [js]
    out = []
    for k, (name, la, lo) in enumerate(points):
        i = None
        if k < len(arr) and isinstance(arr[k], dict):
            cur = arr[k].get("current") or {}
            try:
                i = float(cur.get("precipitation") or 0.0) * 4.0
            except Exception:
                i = 0.0
        out.append({"pt": name, "lat": la, "lng": lo, "i": i})
    return out


def fetch_now(points, source=None, timeout=25):
    """按源取一轮当前实况。返回 (src, [{pt,lat,lng,i}])。"""
    src = pick_source(source)
    if src == "caiyun":
        return src, fetch_caiyun(points, timeout=timeout)
    if src == "qweather":
        return src, fetch_qweather(points, timeout=timeout)
    return src, fetch_openmeteo(points, timeout=timeout)


# ------------------------------------------------------------------
# 落盘 / 读取
# ------------------------------------------------------------------
def _append(rows):
    if not rows:
        return 0
    with open(TRUTH_JSONL, "a", encoding="utf-8") as f:
        for o in rows:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    _trim()
    return len(rows)


def _trim(days=TRUTH_KEEP_DAYS):
    try:
        cutoff = int((_now_bj() - timedelta(days=days)).timestamp() * 1000)
        with open(TRUTH_JSONL, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return
        try:
            if json.loads(lines[0]).get("ms", 0) >= cutoff:
                return          # 最老的还在保留期内 → 不必重写
        except Exception:
            return
        keep = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            try:
                if json.loads(s).get("ms", 0) >= cutoff:
                    keep.append(ln if ln.endswith("\n") else ln + "\n")
            except Exception:
                continue
        with open(TRUTH_JSONL, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except Exception:
        pass


def load_rows():
    """读全部地面实况记录（按 ms 升序）。"""
    if not os.path.exists(TRUTH_JSONL):
        return []
    rows = []
    with open(TRUTH_JSONL, "r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except Exception:
                continue
    rows.sort(key=lambda r: r.get("ms", 0))
    return rows


def _load_meta():
    try:
        with open(TRUTH_META, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta(o):
    try:
        with open(TRUTH_META, "w", encoding="utf-8") as f:
            json.dump(o, f, ensure_ascii=False)
    except Exception:
        pass


# ------------------------------------------------------------------
# 采样
# ------------------------------------------------------------------
def should_sample(active_count, now_ms=None, meta=None):
    """额度自适应：有预警点位 → ACTIVE_MIN，否则 IDLE_MIN。返回 (是否该采, 间隔分钟)。"""
    meta = _load_meta() if meta is None else meta
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    gap = ACTIVE_MIN if active_count > 0 else IDLE_MIN
    last = int(meta.get("last_ms", 0) or 0)
    return (now_ms - last) >= gap * 60000, gap


def sample(points, active_count=0, force=False, source=None, timeout=25):
    """按策略采样并落盘。返回写入条数（未到间隔 → 0）。"""
    meta = _load_meta()
    now_ms = int(_now_bj().timestamp() * 1000)
    due, gap = should_sample(active_count, now_ms, meta)
    if not (due or force):
        return 0
    try:
        src, vals = fetch_now(points, source=source, timeout=timeout)
    except Exception as e:
        _save_meta({"last_ms": meta.get("last_ms", 0), "last_try": now_ms,
                    "last_err": str(e)[:200], "src": pick_source(source)})
        return 0
    ts = _now_bj().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for v in vals:
        if v.get("i") is None:
            continue
        row = {"ms": now_ms, "ts": ts, "pt": v["pt"], "lat": v["lat"], "lng": v["lng"],
               "i": round(float(v["i"]), 2), "src": src,
               "res_km": RES_KM.get(src, 99.0), "kind": "now"}
        # 彩云额外带「最近降水云团」快照（nb 距离 km / ni 强度 mm/h）——
        # 与哨兵自算的 dist/dist40 互为独立旁证，事件时刻留下快照才能事后比对。
        # 老记录与其它源没有这两个字段，读取方一律按可选处理。
        if v.get("nb") is not None:
            row["nb"] = v["nb"]
        if v.get("ni") is not None:
            row["ni"] = v["ni"]
        rows.append(row)
    n = _append(rows)
    _save_meta({"last_ms": now_ms, "last_ts": ts, "src": src,
                "gap_min": gap, "active": active_count, "n": n})
    return n


def backfill(points, hours=HIST_BACKFILL_HOURS, timeout=30):
    """用 Open-Meteo 回溯补历史（无需 key，一次请求取全部点位）。
    用于：刚装上就能回验过去一天已发生的事件，不必干等数据积累。
    返回写入条数。"""
    hours = int(hours)
    lat = ",".join("%.4f" % p[1] for p in points)
    lng = ",".join("%.4f" % p[2] for p in points)
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&hourly=precipitation&past_hours=%d&forecast_hours=0"
           "&timezone=Asia%%2FShanghai" % (lat, lng, hours))
    js = json.loads(_get(url, timeout))
    arr = js if isinstance(js, list) else [js]
    rows = []
    for k, (name, la, lo) in enumerate(points):
        if k >= len(arr) or not isinstance(arr[k], dict):
            continue
        h = arr[k].get("hourly") or {}
        times, vals = h.get("time") or [], h.get("precipitation") or []
        for t, v in zip(times, vals):
            try:
                ms = int(datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=CST).timestamp() * 1000)
            except Exception:
                continue
            rows.append({"ms": ms, "ts": t.replace("T", " ") + ":00", "pt": name,
                         "lat": la, "lng": lo, "i": round(float(v or 0.0), 2),
                         "src": "openmeteo", "res_km": RES_KM["openmeteo"], "kind": "hist"})
    # 去重（同点位同小时重复回填没必要）
    seen = {(r["pt"], r["ms"]) for r in load_rows()}
    fresh = [r for r in rows if (r["pt"], r["ms"]) not in seen]
    return _append(fresh)


# ------------------------------------------------------------------
# 查询（供 verify_closure / daily_review 使用）
# ------------------------------------------------------------------
# ★ 每条记录代表的时间跨度。小时值（hist）代表「该整点起 1 小时」，
#   瞬时采样（now）代表「该时刻前后各约 5 分钟」。
#   查询必须按「覆盖区间是否与窗口相交」来判，不能按「时刻是否落在窗口内」——
#   否则事件发生在 10:18、而地面证据是 10:00 那条小时值时会漏配（实测踩过）。
SPAN_MS = {"hist": 3600_000, "now": 600_000}


def _cover(r):
    """记录 → 覆盖区间 [a, b)。"""
    ms = int(r.get("ms", 0) or 0)
    span = SPAN_MS.get(r.get("kind") or "now", 600_000)
    return ms, ms + span


def truth_max_in(pt, ms0, ms1):
    """窗口内该点位的地面降水最大值。返回 (max_i, n_samples, src) 或 (None, 0, None)。
    n_samples > 0 表示「有地面证据」——调用方据此决定用地面判分还是回落雷达。"""
    best, n, src = None, 0, None
    for r in load_rows():
        if r.get("pt") != pt:
            continue
        a, b = _cover(r)
        if b < ms0 or a > ms1:          # 与窗口无交集
            continue
        n += 1
        src = src or r.get("src")
        v = r.get("i")
        if v is not None and (best is None or v > best):
            best = v
    return best, n, src


def truth_cover(pt, ms0, ms1, min_samples=1):
    """窗口内该点位是否有地面证据（样本数 ≥ min_samples）。"""
    _, n, _ = truth_max_in(pt, ms0, ms1)
    return n >= min_samples


def nearest_cover(pt, ms0, ms1):
    """窗口内该点位的**彩云最近降水云团**快照。

    返回 (nb, ni, ts, lag_min)：nb＝最近云团距离 km、ni＝该云团强度 mm/h、
    ts＝快照时刻、lag_min＝快照距窗口起点的分钟数；无快照则 (None, None, None, None)。
    取「离窗口起点最近」的一条，而不是最大值——nb 是距离，取最大没有意义。
    """
    best, rows = None, load_rows()
    for r in rows:
        if r.get("pt") != pt or r.get("nb") is None:
            continue
        a, b = _cover(r)
        if b < ms0 or a > ms1:
            continue
        d = abs(int(r.get("ms", 0)) - ms0)
        if best is None or d < best[0]:
            best = (d, r)
    if best is None:
        return None, None, None, None
    r = best[1]
    return r.get("nb"), r.get("ni"), r.get("ts"), (int(r.get("ms", 0)) - ms0) / 60000.0


def nearest_vs_radar(pt, ms0, ms1, radar_dist, tol_km=NEAREST_TOL_KM):
    """彩云 nearest 与哨兵自算最近回波距离的旁证比对（**只提示、不参与定级**）。

    返回 dict：{nb, ni, ts, radar, diff, ok, note}；无快照或无雷达值时 ok=None。
    实测口径（`_probe_caiyun_dist.py`，13 点位）：|nb − 雷达dist40| 最大 1.8 km、均值 1.2 km，
    故 tol_km 默认取 3 倍余量（5 km）——只有明显背离才提示，避免用未标定的旁证误判。
    """
    nb, ni, ts, _lag = nearest_cover(pt, ms0, ms1)
    if nb is None or radar_dist is None:
        return {"nb": nb, "ni": ni, "ts": ts, "radar": radar_dist,
                "diff": None, "ok": None, "note": "无彩云快照或无雷达值，跳过旁证"}
    diff = round(nb - radar_dist, 1)
    ok = abs(diff) <= tol_km
    return {"nb": nb, "ni": ni, "ts": ts, "radar": radar_dist, "diff": diff, "ok": ok,
            "note": ("彩云最近降雨 %.1f km ↔ 自算 %.1f km（差 %+.1f km，一致）"
                     % (nb, radar_dist, diff)) if ok else
                    ("⚠ 彩云最近降雨 %.1f km ↔ 自算 %.1f km（差 %+.1f km，超 %.0f km —— 距离判据存疑，需人工核）"
                     % (nb, radar_dist, diff, tol_km))}


def verdict(i):
    """地面降水强度 → 人话。"""
    if i is None:
        return "无记录"
    if i < RAIN_MIN_MM_H:
        return "无有效降水"
    if i < 2.5:
        return "小雨"
    if i < 8:
        return "中雨"
    if i < 16:
        return "大雨"
    return "暴雨"


def stats():
    """覆盖率概览：{总条数, 源集合, 首末时间, 有点位数}。"""
    rows = load_rows()
    if not rows:
        return {"n": 0}
    return {"n": len(rows),
            "srcs": sorted({r.get("src", "?") for r in rows}),
            "first": rows[0].get("ts"), "last": rows[-1].get("ts"),
            "points": len({r.get("pt") for r in rows}),
            "rainy": sum(1 for r in rows if (r.get("i") or 0) >= RAIN_MIN_MM_H)}


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    sys.path.insert(0, BASE)
    import xm_sentinel as S
    points = list(S.TERMS) + list(S.CITIES)

    if "--status" in args:
        st = stats()
        print("地面实况源：%s" % SRC_LABEL.get(pick_source(), pick_source()))
        if not st.get("n"):
            print("truth.jsonl 为空 —— 尚未采集。可先 `--backfill 48` 补历史。")
            return 0
        print("记录 %d 条，覆盖 %d 个点位，源 %s" % (st["n"], st["points"], "、".join(st["srcs"])))
        print("时间范围：%s ~ %s；其中达到降水门槛的 %d 条" % (st["first"], st["last"], st["rainy"]))
        return 0

    if "--backfill" in args:
        h = args[args.index("--backfill") + 1]
        n = backfill(points, hours=float(h))
        print("回溯补采 %s 小时：写入 %d 条（源 openmeteo）" % (h, n))
        return 0

    force = "--force" in args
    src = pick_source()
    n = sample(points, active_count=13 if force else 0, force=force)
    if n:
        print("已采样 %d 条（源 %s）" % (n, SRC_LABEL.get(src, src)))
    else:
        print("未到采样间隔，跳过（源 %s）。用 --force 强制采样。" % SRC_LABEL.get(src, src))
    return 0


if __name__ == "__main__":
    sys.exit(main())
