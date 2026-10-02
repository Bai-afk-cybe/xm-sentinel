# -*- coding: utf-8 -*-
"""Q 组：地面降水实况（precip_truth）+ 判分接入（verify_closure.score）单测。

隔离策略：把 PT.TRUTH_JSONL 指向临时文件，绝不碰生产 truth.jsonl。
离线策略：monkeypatch VC.hit_frame，避免测试依赖雷达网络。
"""
import sys, os, json, tempfile
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import precip_truth as PT
import verify_closure as VC

CST = timezone(timedelta(hours=8))
_tmpdir = tempfile.mkdtemp(prefix="xmtruth_")
PT.TRUTH_JSONL = os.path.join(_tmpdir, "truth.jsonl")
PT.TRUTH_META = os.path.join(_tmpdir, ".truth_meta.json")

OK = FAIL = 0


def ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CST).timestamp() * 1000)


def check(name, got, exp):
    global OK, FAIL
    if got == exp:
        OK += 1
        print("  ✓  %s" % name)
    else:
        FAIL += 1
        print("  ✗  %s\n        实际=%r 期望=%r" % (name, got, exp))


def mkrow(pt, t, i, kind="hist", src="caiyun"):
    return {"ms": ms(t), "ts": t, "pt": pt, "lat": 24.48, "lng": 118.07,
            "i": i, "src": src, "res_km": PT.RES_KM.get(src, 1.0), "kind": kind}


def vrow(pt, t, sev=2, dist=2.9, d40=4.5, peak=40, v=-2.0):
    return {"frame_ms": ms(t), "frame": t[5:], "lag": 13.0, "ts": t + ":00",
            "point": pt, "sev": sev, "dist": dist, "d40": d40, "peak": peak,
            "v": v, "stall": None, "arrived": False, "push": ["触发"], "why": ""}


print("== Q1. 覆盖区间匹配（小时值代表「整点起 1 小时」）==")
PT._append([mkrow("甲点", "2026-10-02 10:00", 3.0, "hist")])
check("hist 10:00 覆盖 10:18~10:48 窗口", PT.truth_max_in("甲点", ms("2026-10-02 10:18"), ms("2026-10-02 10:48"))[0], 3.0)
check("hist 10:00 不覆盖 11:18~11:48 窗口", PT.truth_max_in("甲点", ms("2026-10-02 11:18"), ms("2026-10-02 11:48"))[0], None)
check("样本计数正确", PT.truth_max_in("甲点", ms("2026-10-02 10:18"), ms("2026-10-02 10:48"))[1], 1)
check("源带回来了", PT.truth_max_in("甲点", ms("2026-10-02 10:18"), ms("2026-10-02 10:48"))[2], "caiyun")

print("\n== Q2. 瞬时采样（now，前后各 5 分钟）==")
PT._append([mkrow("乙点", "2026-10-02 10:30", 7.0, "now")])
check("now 10:30 覆盖 10:28~10:40", PT.truth_max_in("乙点", ms("2026-10-02 10:28"), ms("2026-10-02 10:40"))[0], 7.0)
check("now 10:30 不覆盖 10:45~11:00", PT.truth_max_in("乙点", ms("2026-10-02 10:45"), ms("2026-10-02 11:00"))[0], None)

print("\n== Q3. 窗口内多点取最大值（不能取平均）==")
PT._append([mkrow("丙点", "2026-10-02 12:00", 0.2, "hist"),
            mkrow("丙点", "2026-10-02 13:00", 8.5, "hist")])
check("取最大而非首条", PT.truth_max_in("丙点", ms("2026-10-02 12:10"), ms("2026-10-02 13:30"))[0], 8.5)
check("样本两条", PT.truth_max_in("丙点", ms("2026-10-02 12:10"), ms("2026-10-02 13:30"))[1], 2)

print("\n== Q4. 分类映射 ==")
check("预警+有影响 → 命中", VC._classify("warn", True), "命中")
check("预警+无影响 → 空报", VC._classify("warn", False), "空报")
check("观察+有影响 → 漏报", VC._classify("watch", True), "漏报")
check("观察+无影响 → 正确未报", VC._classify("watch", False), "正确未报")

print("\n== Q5. 档位期望雨强（区分「报准」与「报得过头」）==")
check("较高预警期望 5 mm/h", VC.EXPECT_MM_H[2], 5.0)
check("高等预警期望 15 mm/h", VC.EXPECT_MM_H[3], 15.0)
check("极高预警期望 50 mm/h", VC.EXPECT_MM_H[5], 50.0)

print("\n== Q6. score 判分：地面实况优先 ==")
VC.hit_frame = lambda *a, **k: -1          # 雷达口径一律「无影响」，隔离网络
rows = [vrow("丁点", "2026-10-02 14:00")]
PT._append([mkrow("丁点", "2026-10-02 14:00", 3.0, "hist")])          # 地面有雨
r = VC.score(rows, now_ms=ms("2026-10-02 16:00"))
e = r["events"][0]
check("地面有雨 → 命中", e["tag"], "命中")
check("判据标记为「地面」", e["basis"], "地面")
check("地面峰值入档", e["truth_i"], 3.0)
check("3.0 < 期望 5.0 → 报得过头", e["strength_ok"], False)
check("样本归地面", r["n_ground"], 1)

print("\n== Q7. score 判分：地面无雨 → 空报（这正是雷达自证查不出的）==")
PT._append([mkrow("戊点", "2026-10-02 14:00", 0.1, "hist")])
r = VC.score([vrow("戊点", "2026-10-02 14:00")], now_ms=ms("2026-10-02 16:00"))
check("地面 0.1 mm/h → 空报", r["events"][0]["tag"], "空报")
check("FAR 计入", r["far"], 100.0)
check("纯雷达口径也是空报（一致）", r["events"][0]["radar_tag"], "空报")

print("\n== Q8. score 判分：无地面证据 → 回落雷达自证 ==")
r = VC.score([vrow("己点", "2026-10-02 14:00")], now_ms=ms("2026-10-02 16:00"))
check("无地面样本时判据＝雷达", r["events"][0]["basis"], "雷达")
check("样本归雷达", r["n_radar"], 1)
check("note 明确标注回落", "回落雷达自证" in r["events"][0]["note"], True)

print("\n== Q9. 判据分歧标记（雷达 vs 地面不一致要显形）==")
VC.hit_frame = lambda *a, **k: ms("2026-10-02 14:12")     # 雷达口径「有影响」
PT._append([mkrow("庚点", "2026-10-02 14:00", 0.0, "hist")])          # 地面无雨
r = VC.score([vrow("庚点", "2026-10-02 14:00")], now_ms=ms("2026-10-02 16:00"))
e = r["events"][0]
check("地面无雨 → 空报", e["tag"], "空报")
check("纯雷达口径会判「命中」", e["radar_tag"], "命中")
check("note 写出分歧", "纯雷达口径会判" in e["note"], True)

print("\n== Q10. 窗口未走完 → 待定（不参与统计）==")
VC.hit_frame = lambda *a, **k: None
r = VC.score([vrow("辛点", "2026-10-02 15:50")], now_ms=ms("2026-10-02 16:00"))
check("窗口未走完 → 待定", r["events"][0]["tag"], "待定")
check("待定计入 pend", r["pend"], 1)

print("\n== Q11. 源自动探测与标注 ==")
check("无 key 时回落 openmeteo", PT.pick_source(), "openmeteo")
check("显式指定优先", PT.pick_source("caiyun"), "caiyun")
check("openmeteo 标记为非实测", PT.IS_MEASURED["openmeteo"], False)
check("caiyun 标记为实测", PT.IS_MEASURED["caiyun"], True)

print("\n== Q12. verdict 人话分档 ==")
check("0.1 → 无有效降水", PT.verdict(0.1), "无有效降水")
check("1.0 → 小雨", PT.verdict(1.0), "小雨")
check("5.0 → 中雨", PT.verdict(5.0), "中雨")
check("12.0 → 大雨", PT.verdict(12.0), "大雨")
check("30.0 → 暴雨", PT.verdict(30.0), "暴雨")

print("\n== Q13. 彩云 nearest 快照与距离旁证（2026-10-02 新增）==")
def mkrow_nb(pt, t, i, nb, ni=0.19, kind="now", src="caiyun"):
    r = mkrow(pt, t, i, kind, src)
    r["nb"] = nb
    r["ni"] = ni
    return r

PT._append([
    mkrow_nb("壬点", "2026-10-02 16:00", 0.0, 12.0),
    mkrow_nb("壬点", "2026-10-02 16:06", 0.0, 9.0),
    mkrow("癸点", "2026-10-02 16:00", 0.0, "now", "openmeteo"),   # 老记录/其它源：无 nb
])
check("取「离窗口起点最近」的一条，而不是最大值",
      PT.nearest_cover("壬点", ms("2026-10-02 16:00"), ms("2026-10-02 16:30"))[0], 12.0)
check("窗口后移 → 取到更近的第二条",
      PT.nearest_cover("壬点", ms("2026-10-02 16:05"), ms("2026-10-02 16:30"))[0], 9.0)
check("无 nb 字段的记录不算快照（向后兼容）",
      PT.nearest_cover("癸点", ms("2026-10-02 16:00"), ms("2026-10-02 16:30"))[0], None)
check("nb 快照不影响降水最大值统计",
      PT.truth_max_in("壬点", ms("2026-10-02 16:00"), ms("2026-10-02 16:30"))[1], 2)

_v = PT.nearest_vs_radar("壬点", ms("2026-10-02 16:00"), ms("2026-10-02 16:30"), 11.0)
check("差 1.0 km ≤ 容差 → 判一致", _v["ok"], True)
check("一致时 note 写明双方数值", "一致" in _v["note"] and "12.0" in _v["note"], True)
_v2 = PT.nearest_vs_radar("壬点", ms("2026-10-02 16:00"), ms("2026-10-02 16:30"), 25.0)
check("差 13 km > 容差 → 判存疑", _v2["ok"], False)
check("存疑时 note 要求人工核", "存疑" in _v2["note"], True)
check("容差可配（XM_NEAREST_TOL_KM）", PT.NEAREST_TOL_KM, 5.0)
check("无雷达值 → 不比对（ok=None）",
      PT.nearest_vs_radar("壬点", ms("2026-10-02 16:00"), ms("2026-10-02 16:30"), None)["ok"], None)
check("无彩云快照 → 不比对（ok=None）",
      PT.nearest_vs_radar("癸点", ms("2026-10-02 16:00"), ms("2026-10-02 16:30"), 10.0)["ok"], None)

print("\n== Q14. 距离旁证接进判分（只提示、绝不改档）==")
VC.hit_frame = lambda *a, **k: None            # 关掉雷达自证，让地面实况说了算
PT._append([mkrow_nb("子点", "2026-10-02 17:00", 3.0, 10.0, ni=0.19)])
_e = VC.score([vrow("子点", "2026-10-02 17:00", dist=12.6, d40=9.5)],
              now_ms=ms("2026-10-02 18:00"))["events"][0]
check("彩云 10.0 ↔ 自算 d40 9.5 → 旁证判一致", _e["near"]["ok"], True)
check("一致时不打扰报告（note 不加 ◎）", "◎" in _e["note"], False)
check("旁证不改档（仍按地面判命中）", _e["tag"], "命中")

PT._append([mkrow_nb("丑点", "2026-10-02 17:00", 3.0, 25.0, ni=0.19)])
_e2 = VC.score([vrow("丑点", "2026-10-02 17:00", dist=12.6, d40=9.5)],
               now_ms=ms("2026-10-02 18:00"))["events"][0]
check("彩云 25.0 ↔ 自算 9.5 → 旁证判存疑", _e2["near"]["ok"], False)
check("存疑写进报告 note（带 ◎ 标记）", "◎" in _e2["note"] and "存疑" in _e2["note"], True)
check("背离也不改档（仍按地面判命中）", _e2["tag"], "命中")

print("\n" + ("=" * 46))
print("全部通过 ✅  （Q 组 %d 项）" % OK if not FAIL else "失败 %d 项 ❌" % FAIL)
print("=" * 46)
sys.exit(1 if FAIL else 0)
