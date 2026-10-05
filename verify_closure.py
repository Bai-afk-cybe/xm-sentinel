# -*- coding: utf-8 -*-
"""预测检验闭环：把哨兵的历史判定拿去和「后来真正发生了什么」对账。

用法：
    python verify_closure.py                 # 回验 verify.jsonl 全部记录
    python verify_closure.py --days 3        # 只回验最近 3 天
    python verify_closure.py --detail        # 逐事件打印明细

判分口径：
  事件聚合：同一点位、连续（间隔 ≤ 1 帧槽）同类的判定合并成一个「事件」，取首条。
    · 预警事件：sev ≥ 2
    · 观察事件：sev ≤ 1 但当时最近回波已 ≤ VERIFY_NEAR_KM
  影响认定（两级，优先级从高到低）：
    ① **地面实况**（2026-10-02 接入，见 precip_truth.py）：事件之后 HORIZON_MIN 分钟内，
       该点位地面降水峰值 ≥ RAIN_MIN_MM_H（0.5 mm/h）→ 认定「真的下到了地上」。
       这是**独立于雷达**的证据——雷达看到回波 ≠ 地面有雨（牡丹国际大酒店 10-02 10:06
       报「较高预警」，地面实测仅 0.1 mm/h＝滴雨未下）。
    ② 雷达自证（回落）：窗口内无任何地面采样时，退回「10 km 内峰值 ≥ DBZ_ACT」或
       「最近 ≥ DBZ_ACT 强回波 ≤ T_ACT_KM」。
  判分：
    预警事件 + 有影响  → 命中（HIT），记录提前量（首次触发帧 → 首次影响帧）
    预警事件 + 无影响  → 空报（FALSE）
    观察事件 + 有影响  → 漏报（MISS）
    观察事件 + 无影响  → 正确未报（CORRECT-NEG）
  POD = HIT/(HIT+MISS)   FAR = FALSE/(HIT+FALSE)
  事件时间太近、回验窗口还没走完 → PENDING（不参与统计）

  ★ 每条事件同时保留 radar_tag（纯雷达口径会怎么判），便于在报告里定位
    「雷达说命中、地面说没下」这类被雷达自证掩盖的空报。
"""
import sys, os, json, math
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xm_sentinel as S

try:                      # 地面实况（缺失也不致命：自动回落雷达自证）
    import precip_truth as PT
except Exception:
    PT = None

HORIZON_MIN = 30.0       # 回验窗口
BASE = os.path.dirname(os.path.abspath(__file__))
JSONL = os.path.join(BASE, S.VERIFY_JSONL)

# 各档位「应验」的期望小时雨量（mm/h）。
# 依据：dBZ→雨强按 Marshall-Palmer Z=200R^1.6 折算，40 dBZ≈12 mm/h 瞬时；
# 但短临预警检验看的是「小时内累计」，对流单体过境通常只持续 10~30 分钟，
# 故取折算值的 1/3~1/2 作为小时尺度期望：较高 ≥5、高等 ≥15、特高 ≥30、极高 ≥50。
# 用途：区分「报准了」和「报得过头」——0.5 mm/h 的毛毛雨不该等同于 40 dBZ 强对流的应验。
EXPECT_MM_H = {0: 0.0, 1: 0.5, 2: 5.0, 3: 15.0, 4: 30.0, 5: 50.0}


def load_rows(days=None, quiet=False):
    if not os.path.exists(JSONL):
        if not quiet:
            print("没有检验流水（%s 不存在）。先让哨兵正常跑几轮。" % JSONL)
        return []
    rows = []
    with open(JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    if days:
        cut = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r.get("ts", "")[:10] >= cut]
    return rows


def aggregate(rows):
    """同点位、连续同类判定合并为事件（取首条）。"""
    by_pt = {}
    for r in rows:
        by_pt.setdefault(r["point"], []).append(r)
    events = []
    for pt, rs in by_pt.items():
        rs.sort(key=lambda o: o["frame_ms"])
        cur = None
        for r in rs:
            cls = "warn" if r["sev"] >= 2 else "watch"
            if cur and cur["cls"] == cls and (r["frame_ms"] - cur["last"]) <= S.SLOT_MS * 1.5:
                cur["last"] = r["frame_ms"]
                cur["n"] += 1
                cur["rows"].append(r)
            else:
                if cur:
                    events.append(cur)
                cur = {"pt": pt, "cls": cls, "first": r["frame_ms"], "last": r["frame_ms"],
                       "n": 1, "rows": [r], "peak_sev": r["sev"]}
            cur["peak_sev"] = max(cur["peak_sev"], r["sev"])
        if cur:
            events.append(cur)
    events.sort(key=lambda e: e["first"])
    return events


# ---- 帧缓存：同一帧被多个事件共用 ----
ALL = list(S.TERMS) + list(S.CITIES)
PT_BY_NAME = {p[0]: p for p in ALL}
_px_cache = {}
_bbox = S.points_bbox(ALL, S.SEARCH_KM)


def metrics_at(frame_ms, pt_name):
    """取某帧上某点位的 (peak10, d40)。帧缺则返回 None。"""
    if frame_ms not in _px_cache:
        try:
            data = S.http_get(S.frame_url(frame_ms), 25)
        except Exception:
            data = b""
        if not data or len(data) < 2000:
            _px_cache[frame_ms] = None
        else:
            _w, _h, pxs = S.strong_pixels(data, S.DBZ_ACT, _bbox)   # 只要 ≥40 dBZ
            _px_cache[frame_ms] = pxs
    pxs = _px_cache[frame_ms]
    if pxs is None:
        return None
    p = PT_BY_NAME.get(pt_name)
    if p is None:
        return None
    px = S.latlon_to_px(p[1], p[2])
    m = S.frame_metrics([px], pxs)[0]
    return m[3], m[4]        # peak10, d40


def hit_frame(ev, horizon_min=None, now_ms=None):
    """事件之后首个「实况影响」帧（10 km 内峰值 ≥40 或 最近 ≥40 回波 ≤10 km）。
    返回帧时刻 ms；-1 ＝ 窗口内无影响（确定）；None ＝ 未来帧还没出（待定）。"""
    horizon_min = HORIZON_MIN if horizon_min is None else horizon_min
    if now_ms is None:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
    for k in range(0, int(horizon_min / 6) + 1):
        t = ev["first"] + k * S.SLOT_MS
        if (now_ms - t) < 0:
            return None                      # 未来帧还没出
        m = metrics_at(t, ev["pt"])
        if m is None:
            continue
        peak, d40 = m
        if (peak and peak >= S.DBZ_ACT) or (d40 is not None and d40 <= S.T_ACT_KM):
            return t
    return -1


TAG_BY = {("warn", True): "命中", ("warn", False): "空报",
          ("watch", True): "漏报", ("watch", False): "正确未报"}


def _classify(cls, hit):
    """(事件类, 是否有影响) → 标签。"""
    return TAG_BY[(cls, bool(hit))]


def truth_probe(pt, ms0, ms1):
    """地面实况核验：窗口内该点位是否真的下了雨。
    返回 (hit_bool|None, max_i, n_samples, src)。
    hit_bool=None ＝ 窗口内没有任何地面采样 → 调用方回落雷达自证。"""
    if PT is None:
        return None, None, 0, None
    try:
        i, n, src = PT.truth_max_in(pt, ms0, ms1)
    except Exception:
        return None, None, 0, None
    if n <= 0:
        return None, None, 0, None
    return (i is not None and i >= PT.RAIN_MIN_MM_H), i, n, src


def near_probe(pt, ms0, ms1, radar_dist):
    """★ 距离旁证（2026-10-02 立）：彩云 `nearest`（独立雷达网＋地面站融合）与
    哨兵自算的「最近回波距离」互校——两条链路若说法差得远，说明距离判据可疑。

    **只提示、不参与定级**：旁证与主判据口径并未完全标定（见 precip_truth._caiyun_one
    的标定说明：nb ↔ dist40 差 −1.7~+1.8 km），宁可少判也不据旁证改档。
    返回 dict（含 nb/ni/ts/radar/diff/ok/note），无快照或无雷达值 → None。
    """
    if PT is None or not hasattr(PT, "nearest_vs_radar"):
        return None
    try:
        r = PT.nearest_vs_radar(pt, ms0, ms1, radar_dist)
    except Exception:
        return None
    return r if r.get("diff") is not None else None


def score(rows, horizon_min=None, now_ms=None, use_truth=True):
    """★ 纯函数（无打印、无 argv 依赖）：把流水记录打分，返回结构化结果。
    daily_review.py / verify_closure CLI 共用同一套口径，避免两处判分不一致。

    use_truth=True → 优先用**地面实况**认定影响（窗口内无地面证据时回落雷达自证）。
    返回:
      {n_rows, events:[{pt, cls, peak_sev, n, first_ms, first_row, tag, note, lead,
                        basis, radar_tag, truth_tag, truth_i, truth_n, truth_src,
                        near}],          # near＝彩云 nearest 距离旁证（只提示不改档）
       hit, false, miss, corr, pend, pod, far, leads, horizon_min, use_truth,
       n_ground, n_radar}          # 各判据覆盖的事件数
    tag ∈ 命中/空报/漏报/正确未报/待定；basis ∈ 地面/雷达/—
    """
    horizon_min = HORIZON_MIN if horizon_min is None else horizon_min
    if now_ms is None:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
    events = aggregate(rows)
    hit = false = miss = corr = pend = 0
    hit_strong = 0
    n_ground = n_radar = 0
    leads, out = [], []
    for ev in events:
        hf = hit_frame(ev, horizon_min, now_ms)
        first = ev["rows"][0]
        win_end = ev["first"] + horizon_min * 60000.0
        lead = None
        expect = EXPECT_MM_H.get(ev["peak_sev"], 0.0)   # 该档期望雨强（待定分支也要用到）
        strength_ok = None

        radar_hit = (hf is not None and hf > 0)
        radar_tag = None if hf is None else _classify(ev["cls"], radar_hit)

        t_hit = t_i = t_src = None
        t_n = 0
        near = None                                   # 距离旁证（仅非待定事件填充）
        if use_truth and (now_ms - win_end) >= 0:     # 窗口走完才谈地面积分
            t_hit, t_i, t_n, t_src = truth_probe(ev["pt"], ev["first"], win_end)

        if hf is None and t_hit is None:
            tag, basis = "待定", "—"
            note = "回验窗口尚未走完"
            pend += 1
        else:
            use_ground = t_hit is not None
            basis = "地面" if use_ground else "雷达"
            impact = bool(t_hit) if use_ground else radar_hit
            if use_ground:
                n_ground += 1
            else:
                n_radar += 1
            tag = _classify(ev["cls"], impact)

            if ev["cls"] == "warn" and impact:
                hit += 1
            elif ev["cls"] == "warn":
                false += 1
            elif ev["cls"] == "watch" and impact:
                miss += 1
            else:
                corr += 1

            # 提前量只用雷达帧算（地面小时值粒度太粗，算不出分钟级提前量）
            if radar_hit and ev["cls"] == "warn":
                lead = (hf - ev["first"]) / 60000.0
                leads.append(lead)

            fu = "、".join(sorted({x for r in ev["rows"] for x in (r.get("push") or [])}))

            if use_ground:
                g = "地面峰值 %s mm/h（%d 样本，%s）" % (
                    ("%.1f" % t_i) if t_i is not None else "—", t_n, t_src or "?")
                if ev["cls"] == "warn" and impact:
                    note = "地面实测到降水：%s" % g
                elif ev["cls"] == "warn":
                    note = "%d 分钟内地面无有效降水（%s）%s" % (
                        int(horizon_min), g, ("；已推送：" + fu) if fu else "")
                elif impact:
                    note = "判 %s 但地面实测到降水：%s" % (S.SEV_NAMES[first["sev"]], g)
                else:
                    note = "判 %s，地面亦无有效降水（%s）" % (S.SEV_NAMES[first["sev"]], g)
            else:
                if ev["cls"] == "warn" and radar_hit:
                    note = "提前量 %.0f 分钟（帧 %s → 影响帧 %s）" % (
                        lead, first["frame"], S.bj(hf))
                elif ev["cls"] == "warn":
                    note = "预警 %s（%d 帧）但 %d 分钟内雷达亦无影响%s" % (
                        S.SEV_NAMES[ev["peak_sev"]], ev["n"], int(horizon_min),
                        ("；已推送：" + fu) if fu else "")
                elif radar_hit:
                    note = "判 %s 但 %d 分钟内出现雷达影响（%s）" % (
                        S.SEV_NAMES[first["sev"]], int(horizon_min), S.bj(hf))
                else:
                    note = "判 %s，%d 分钟内雷达亦无影响" % (
                        S.SEV_NAMES[first["sev"]], int(horizon_min))
                note += "；⚠ 无地面样本，回落雷达自证"

            if use_ground and tag == "命中":
                strength_ok = (t_i is not None and t_i >= expect)
                if strength_ok:
                    hit_strong += 1
                else:
                    note += "；⚠ 报得过头——该档期望 ≥%.1f mm/h" % expect

            if radar_tag and radar_tag != tag:
                note += "；★ 纯雷达口径会判「%s」" % radar_tag

            # ★ 距离旁证：优先用 d40（与彩云 nb 同口径——实测 nb↔d40 差 ≤1.8 km，
            # 而 nb↔dist(35 dBZ) 差约 2.1 km）；无 d40 再退用 dist。
            rd = first.get("d40")
            if rd is None:
                rd = first.get("dist")
            near = near_probe(ev["pt"], ev["first"], win_end, rd)
            if near and near.get("ok") is False:
                note += "；◎ " + near["note"]

        out.append({"pt": ev["pt"], "cls": ev["cls"], "peak_sev": ev["peak_sev"], "n": ev["n"],
                    "first_ms": ev["first"], "first_row": first, "tag": tag, "note": note,
                    "lead": lead, "last_ms": ev["last"], "basis": basis,
                    "radar_tag": radar_tag,
                    "truth_tag": (_classify(ev["cls"], t_hit) if t_hit is not None else None),
                    "truth_i": t_i, "truth_n": t_n, "truth_src": t_src,
                    "expect_i": expect, "strength_ok": strength_ok, "near": near})
    n_warn, n_obs = hit + false, miss + corr
    pod = (100.0 * hit / (hit + miss)) if (hit + miss) else None
    far = (100.0 * false / n_warn) if n_warn else None
    return {"n_rows": len(rows), "events": out, "hit": hit, "false": false, "miss": miss,
            "corr": corr, "pend": pend, "pod": pod, "far": far, "leads": leads,
            "horizon_min": horizon_min, "use_truth": use_truth,
            "n_ground": n_ground, "n_radar": n_radar, "hit_strong": hit_strong}


def main():
    detail = "--detail" in sys.argv
    days = None
    if "--days" in sys.argv:
        days = float(sys.argv[sys.argv.index("--days") + 1])

    rows = load_rows(days)
    if not rows:
        return 0
    res = score(rows)
    events = res["events"]
    print("=" * 74)
    print("检验流水：%d 条记录 → %d 个事件（%s ~ %s）"
          % (len(rows), len(events),
             min(r["ts"] for r in rows)[:16], max(r["ts"] for r in rows)[:16]))
    print("=" * 74)

    if detail:
        print("\n---- 明细 ----")
        for e in events:
            f = e["first_row"]
            print("  [%s] %-14s %-9s 首帧 %s  dist=%s d40=%s peak=%s stall=%s 判据=%s\n         %s"
                  % (e["tag"], e["pt"], S.SEV_NAMES[e["peak_sev"]], f["frame"],
                     f.get("dist"), f.get("d40"), f.get("peak"),
                     f.get("stall") or "—", e.get("basis", "—"), e["note"]))

    hit, false, miss, corr, pend = res["hit"], res["false"], res["miss"], res["corr"], res["pend"]
    print("\n---- 统计 ----")
    print("  预警事件 %d 个：命中 %d、空报 %d" % (hit + false, hit, false))
    print("  观察事件 %d 个：漏报 %d、正确未报 %d" % (miss + corr, miss, corr))
    if pend:
        print("  待定 %d 个（回验窗口尚未走完）" % pend)
    print("  POD（命中率）= %s" % ("%.0f%%" % res["pod"] if res["pod"] is not None else "样本不足"))
    print("  FAR（空报率）= %s" % ("%.0f%%" % res["far"] if res["far"] is not None else "样本不足"))
    if res["hit"]:
        print("  强度达标 %d/%d 次命中：地面雨强与该档位期望相符（未达标＝报得过头）"
              % (res["hit_strong"], res["hit"]))
    print("  判据覆盖：地面实况 %d 个事件、回落雷达自证 %d 个事件"
          % (res["n_ground"], res["n_radar"]))
    if res["leads"]:
        ls = sorted(res["leads"])
        print("  提前量：中位 %.0f 分钟，范围 %.0f~%.0f 分钟（%d 次命中）"
              % (ls[len(ls) // 2], ls[0], ls[-1], len(ls)))
    print("\n  说明：影响认定优先用**地面降水实况**（事件后 %d 分钟内该点位雨量 ≥%.1f mm/h），"
          % (int(HORIZON_MIN), (PT.RAIN_MIN_MM_H if PT else 0.5)))
    print("        窗口内无任何地面采样时才回落雷达自证（10 km 内峰值 ≥40 dBZ 或最近 ≥40 dBZ 回波 ≤10 km）。")
    print("        ★ 地面证据才是「有没有下到地上」的答案；雷达看到回波不等于地面有雨。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
