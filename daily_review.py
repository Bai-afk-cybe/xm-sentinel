# -*- coding: utf-8 -*-
"""每日复盘校验：把「前一天报了什么」和「后来实际发生了什么」对账。

设计要点
--------
· **复盘对象＝前一日**（北京时间 00:00~24:00 内首帧的判定事件）。之所以不复盘当日，
  是因为影响认定需要在事件后 30 分钟内看实况帧；早上 9 点跑时，前一日最后一个事件
  （最晚 23:54 帧 + 30 分钟 = 次日 00:24）早已闭环，不会出现「待定」。
· **判分口径与 `verify_closure.score()` 完全同一函数**，绝不各写一套——两处判分不一致
  是准确率统计里最隐蔽的坑。
· 只用实况雷达帧，不引入任何数值预报产品。**雷达自证 ≠ 地面有雨**，报告里必须写明这条局限。
· 产出 markdown 入库（`review/<日期>.md` + `review/latest.md`），并把摘要推到方糖手机。

用法
----
    python daily_review.py                    # 复盘昨天
    python daily_review.py --date 2026-10-02  # 复盘指定日
    python daily_review.py --no-push          # 只出报告，不推送
"""
import sys, os, json
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import xm_sentinel as S
import verify_closure as VC

try:                      # 地面降水实况（缺失不致命：判分会自动回落雷达自证）
    import precip_truth as PT
except Exception:
    PT = None

CST = timezone(timedelta(hours=8))
REVIEW_DIR = os.path.join(BASE, "review")
LATEST_MD = os.path.join(REVIEW_DIR, "latest.md")


def day_key(frame_ms):
    """帧时刻 → 北京日期字符串。"""
    return datetime.fromtimestamp(frame_ms / 1000.0, CST).strftime("%Y-%m-%d")


def recent_days(n, end_day):
    """返回最近 n 个自然日（含 end_day），升序。"""
    d0 = datetime.strptime(end_day, "%Y-%m-%d")
    return [(d0 - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def fmt_rate(v, n_den):
    """比率格式化：分母为 0 时诚实写「样本不足」。"""
    if v is None or n_den <= 0:
        return "样本不足"
    return "%.0f%%" % v


def events_on_day(all_events, day):
    """取「首帧落在指定日」的事件。"""
    return [e for e in all_events if day_key(e["first_ms"]) == day]


def summarize(events):
    """对一组事件做计数汇总（口径与 score 一致）。"""
    hit = sum(1 for e in events if e["tag"] == "命中")
    false = sum(1 for e in events if e["tag"] == "空报")
    miss = sum(1 for e in events if e["tag"] == "漏报")
    corr = sum(1 for e in events if e["tag"] == "正确未报")
    pend = sum(1 for e in events if e["tag"] == "待定")
    leads = [e["lead"] for e in events if e["lead"] is not None]
    strong = sum(1 for e in events if e.get("strength_ok") is True)
    over = sum(1 for e in events if e.get("strength_ok") is False)
    ground = sum(1 for e in events if e.get("basis") == "地面")
    radar = sum(1 for e in events if e.get("basis") == "雷达")
    diverge = [e for e in events if e.get("radar_tag") and e.get("radar_tag") != e["tag"]]
    n_warn, n_obs = hit + false, miss + corr
    return {"hit": hit, "false": false, "miss": miss, "corr": corr, "pend": pend,
            "warn": n_warn, "obs": n_obs, "leads": leads,
            "pod": (100.0 * hit / (hit + miss)) if (hit + miss) else None,
            "far": (100.0 * false / n_warn) if n_warn else None,
            "strong": strong, "over": over, "ground": ground, "radar": radar,
            "diverge": diverge, "n": len(events)}


def lead_text(leads):
    if not leads:
        return "—"
    ls = sorted(leads)
    return "中位 %.0f 分钟（%.0f~%.0f 分钟，%d 次命中）" % (
        ls[len(ls) // 2], ls[0], ls[-1], len(ls))


def style_of(tag):
    return {"命中": "✅", "空报": "❌", "漏报": "⚠️", "正确未报": "🟢", "待定": "⏳"}.get(tag, "·")


def build_report(day, day_events, day_sum, week_sum, week_from, rows=None):
    """生成 markdown 报告。"""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    L = []
    L.append("# 厦门短临预警 · 每日复盘")
    L.append("")
    L.append("> **复盘对象**：%s（北京时间 00:00~24:00）　|　**生成**：%s　|　**系统**：XM Sentinel" % (day, now))
    src = PT.pick_source() if PT else None
    L.append("> **判分依据**：① 地面降水实况（优先）② 雷达帧自证（无地面样本时回落）")
    L.append("> **影响认定口径**：事件后 30 分钟内，该点位**地面雨量 ≥%.1f mm/h**（源：%s）"
             % ((PT.RAIN_MIN_MM_H if PT else 0.5),
                (PT.SRC_LABEL.get(src, src) if PT else "未接入")))
    L.append("> **回落口径**：无地面样本时用雷达（10 km 内峰值 ≥%d dBZ 或最近 ≥%d dBZ 回波 ≤%.0f km）"
             % (S.DBZ_ACT, S.DBZ_ACT, S.T_ACT_KM))
    if rows:
        L.append("> **系统心跳**：检验流水 %d 条，最近记录 %s（本行存在＝云端哨兵正常运行）"
                 % (len(rows), max(r.get("ts", "") for r in rows)))
    else:
        L.append("> **系统心跳**：⚠️ 检验流水为空——哨兵尚未积累判定记录（云端首次部署时为正常现象）。")
    L.append("")
    L.append("## 一、成绩单")
    L.append("")
    L.append("| 项目 | 昨日（%s） | 近 7 天（%s 起） |" % (day, week_from))
    L.append("|---|---|---|")
    L.append("| 预警事件 | %d 个（命中 %d / 空报 %d） | %d 个（命中 %d / 空报 %d） |"
             % (day_sum["warn"], day_sum["hit"], day_sum["false"],
                week_sum["warn"], week_sum["hit"], week_sum["false"]))
    L.append("| 观察事件 | %d 个（漏报 %d / 正确未报 %d） | %d 个（漏报 %d / 正确未报 %d） |"
             % (day_sum["obs"], day_sum["miss"], day_sum["corr"],
                week_sum["obs"], week_sum["miss"], week_sum["corr"]))
    L.append("| POD 命中率 | %s | %s |"
             % (fmt_rate(day_sum["pod"], day_sum["hit"] + day_sum["miss"]),
                fmt_rate(week_sum["pod"], week_sum["hit"] + week_sum["miss"])))
    L.append("| FAR 空报率 | %s | %s |"
             % (fmt_rate(day_sum["far"], day_sum["warn"]),
                fmt_rate(week_sum["far"], week_sum["warn"])))
    L.append("| 提前量 | %s | %s |" % (lead_text(day_sum["leads"]), lead_text(week_sum["leads"])))
    L.append("")
    L.append("> 说明：POD ＝ 该报的报出来了吗（越低＝漏报越多）；FAR ＝ 报了的是不是白报（越低＝误报越少）。"
             "「观察事件」指未达预警档、但最近回波已进入 %.0f km 的轮次——用来抓漏报。" % S.VERIFY_NEAR_KM)
    L.append("")
    L.append("## 二、事件明细（%s）" % day)
    L.append("")
    if not day_events:
        L.append("本日无纳入回验的判定事件（无预警、且无回波进入 %.0f km 警戒圈）。" % S.VERIFY_NEAR_KM)
    else:
        L.append("| 结果 | 点位 | 判定档位 | 首帧 | 图上最近 | ≥40 距离 | 峰值 | 趋势 | 说明 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for e in day_events:
            f = e["first_row"]
            stall = f.get("stall")
            v = f.get("v")
            if stall == "dead":
                trend = "停滞"
            elif stall == "flicker":
                trend = "原地生消"
            elif stall == "jump":
                trend = "跳变弃用"
            elif v is None:
                trend = "不可判"
            elif v < -1.5:
                trend = "↓逼近"
            elif v > 1.5:
                trend = "↑远离"
            else:
                trend = "移向不稳"
            L.append("| %s %s | %s | %s | %s | %s km | %s | %s | %s | %s |"
                     % (style_of(e["tag"]), e["tag"], e["pt"], S.SEV_NAMES[e["peak_sev"]],
                        f.get("frame", "—"),
                        f.get("dist") if f.get("dist") is not None else "—",
                        f.get("d40") if f.get("d40") is not None else "—",
                        (str(f.get("peak")) + " dBZ") if f.get("peak") else "—",
                        trend, e["note"]))
        L.append("")
        pushed = sorted({x for e in day_events for x in (e["first_row"].get("push") or [])})
        L.append("**已推送**：%s" % ("、".join(pushed) if pushed else "无"))
    L.append("")
    L.append("## 三、地面实况核验（雷达 vs 地面）")
    L.append("")
    gr = [e for e in day_events if e.get("truth_n")]
    if not gr:
        L.append("本日无带地面样本的事件。")
    else:
        L.append("| 点位 | 判定档位 | 地面峰值 | 该档期望 | 强度是否相符 | 纯雷达口径 |")
        L.append("|---|---|---|---|---|---|")
        for e in gr:
            ti = e.get("truth_i")
            so = e.get("strength_ok")
            so_txt = "✅ 相符" if so is True else ("⚠ 报得过头" if so is False else "—（未命中，不计强度）")
            L.append("| %s | %s | %s | ≥%.1f mm/h | %s | %s |" % (
                e["pt"], S.SEV_NAMES[e["peak_sev"]],
                ("%.1f mm/h" % ti) if ti is not None else "—",
                e.get("expect_i", 0.0), so_txt, e.get("radar_tag") or "—"))
        L.append("")
        L.append("> 「地面峰值」＝事件后 %d 分钟内该点位地面降水的最大值（源：%s）；"
                 % (int(VC.HORIZON_MIN), PT.SRC_LABEL.get(PT.pick_source(), "—") if PT else "未接入"))
        L.append("> 「该档期望」＝该档位对流强度折算到小时尺度应有的雨量。地面有雨但远低于期望＝**报得过头**。")
        dv = day_sum["diverge"]
        if dv:
            L.append("")
            L.append("**⚠ 口径分歧**（纯雷达口径与地面结论不一致——这正是接入地面实况的价值）：")
            for e in dv:
                L.append("- **%s**：地面判「%s」，而纯雷达口径会判「%s」" % (e["pt"], e["tag"], e["radar_tag"]))
    L.append("")
    L.append("## 四、结论")
    L.append("")
    if not day_events:
        L.append("- 昨日无需要回验的判定事件，系统正常运行。")
    else:
        if day_sum["false"] and not day_sum["hit"]:
            L.append("- ⚠️ 昨日 %d 次预警**全部空报**：须逐条查根因（孤立杂波？外推过头？阈值过松？）。" % day_sum["false"])
        elif day_sum["false"]:
            L.append("- 昨日 %d 次预警中有 %d 次空报（FAR %s），重点关注空报事件的回波成色与移向佐证。"
                     % (day_sum["warn"], day_sum["false"], fmt_rate(day_sum["far"], day_sum["warn"])))
        if day_sum["over"]:
            L.append("- ⚠️ 昨日 %d 次预警**报得过头**：地面确有降水但雨强远低于该档期望——"
                     "须核对档位门槛（尤其 dBZ 分档）是否过松。" % day_sum["over"])
        if day_sum["miss"]:
            L.append("- ⚠️ 昨日 %d 次**漏报**：回波已造成实况影响但未触发，须检查距离门槛与闸门是否过紧。" % day_sum["miss"])
        if week_sum["warn"] >= 5:
            L.append("- 近 7 天累计样本 %d 个，已可支撑初步的参数标定（前 5 天样本量下的 POD/FAR 才具统计意义）。" % week_sum["warn"])
        else:
            L.append("- 近 7 天预警样本仅 %d 个，**统计意义有限**，暂不宜据单日结果调参。" % week_sum["warn"])
    L.append("")
    L.append("## 五、口径与局限（务必知晓）")
    L.append("")
    _src = PT.pick_source() if PT else None
    if _src in ("caiyun", "qweather"):
        L.append("1. **地面实况（已接入，%s）**：雷达与地面站融合，1 km 级，"
                 "因此判「这一点位没下雨」可信度高。" % PT.SRC_LABEL.get(_src, _src))
    elif _src == "openmeteo":
        L.append("1. **地面实况（已接入，%s）**：⚠ 它是**数值模式值，不是站点实测**——"
                 "对小尺度对流偏干、落区被平滑。" % PT.SRC_LABEL.get(_src, _src))
        L.append("   故它判「有雨」较可信，判「无雨」存在**低估风险**（可能把真下过雨的算成空报）。")
        L.append("   要拿 1km 实测级真值，填环境变量 `XM_CAIYUN_TOKEN` 即自动切换为彩云（推荐）。")
    else:
        L.append("1. **地面实况**：未接入，本轮判分全部回落雷达自证。")
    L.append("2. **观测天花板**：雷达拼图 6 分钟一帧，实测图龄中位约 13 分钟（11~19 分钟）。")
    L.append("   对 15 分钟内到达的强对流，物理上只剩 0~1 帧反应时间，提前量无法再靠算法拉长。")
    L.append("3. **样本量**：单日事件常在 0~3 个，单日数字波动极大，**应以近 7 天累计为主要参考**。")
    L.append("")
    L.append("---")
    L.append("*本报告由 XM Sentinel 自动生成；判分函数 verify_closure.score()，与手动 `python verify_closure.py` 完全同源。*")
    return "\n".join(L)


def build_push(day, day_sum, week_sum):
    """手机推送摘要（方糖 desp，保持精简）。"""
    def line(tag, s):
        return "**%s**：预警 %d · 命中 %d · 空报 %d · 漏报 %d" % (tag, s["warn"], s["hit"], s["false"], s["miss"])
    L = []
    L.append(line(day, day_sum))
    L.append(line("近 7 天", week_sum))
    L.append("**FAR（空报率）**：昨日 %s ／ 近 7 天 %s"
             % (fmt_rate(day_sum["far"], day_sum["warn"]), fmt_rate(week_sum["far"], week_sum["warn"])))
    L.append("**POD（命中率）**：%s" % fmt_rate(week_sum["pod"], week_sum["hit"] + week_sum["miss"]))
    L.append("**提前量**：%s" % lead_text(week_sum["leads"]))
    L.append("**判据**：地面实况 %d · 雷达回落 %d" % (week_sum["ground"], week_sum["radar"]))
    if week_sum["over"]:
        L.append("**报得过头**：%d 次（地面雨强远低于档位期望）" % week_sum["over"])
    if day_sum["false"] and not day_sum["hit"]:
        L.append("")
        L.append("⚠️ 昨日预警全部空报，已列入待查。")
    if day_sum["miss"]:
        L.append("")
        L.append("⚠️ 昨日有 %d 次漏报，已列入待查。" % day_sum["miss"])
    L.append("")
    L.append("> 判分依据：**地面降水实况优先**（事件后 30 分钟内雨量 ≥%.1f mm/h 认定影响），"
             "无地面样本才回落雷达帧自证。" % (PT.RAIN_MIN_MM_H if PT else 0.5))
    return "\n".join(L)


def main():
    args = sys.argv[1:]
    no_push = "--no-push" in args
    day = args[args.index("--date") + 1] if "--date" in args else \
        (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")

    rows = VC.load_rows(quiet=True)            # 空也继续：每日报告本身是「云端还活着」的心跳
    if not rows:
        print("检验流水为空（%s 不存在）——仍出报告与心跳推送。" % VC.JSONL)

    res = VC.score(rows)                       # 全量打分（影响认定需要事件后的帧，可能跨日）

    # ★ 地面实况自愈（2026-10-02）：若预警事件缺地面证据（本地关机 / 云端故障导致漏采），
    #   立刻用 Open-Meteo 回溯补采再重新判分——它支持回溯过去数天，能把缺口补上。
    #   这正是「没开电脑也能复盘」的关键：地面真值不必实时攒，事后可回溯。
    if PT is not None:
        missing = [e for e in res["events"] if e["cls"] == "warn" and e.get("basis") == "雷达"]
        if missing:
            try:
                nb = PT.backfill(list(S.TERMS) + list(S.CITIES), hours=96)
                if nb:
                    print("地面实况自愈：回溯补采 %d 条，覆盖 %d 个缺证预警事件" % (nb, len(missing)))
                    res = VC.score(rows)
            except Exception as e:
                print("地面实况回溯补采失败（不影响复盘）：%s" % e)

    all_events = res["events"]
    day_events = events_on_day(all_events, day)

    wk_days = recent_days(7, day)
    week_events = [e for e in all_events if day_key(e["first_ms"]) in wk_days]

    day_sum = summarize(day_events)
    week_sum = summarize(week_events)

    report = build_report(day, day_events, day_sum, week_sum, wk_days[0], rows)
    os.makedirs(REVIEW_DIR, exist_ok=True)
    day_md = os.path.join(REVIEW_DIR, "%s.md" % day)
    for p in (day_md, LATEST_MD):
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(report + "\n")
    print("报告已写入：%s（及 latest.md）" % day_md)
    print("  复盘对象 %s：事件 %d 个（命中 %d 空报 %d 漏报 %d）"
          % (day, day_sum["n"], day_sum["hit"], day_sum["false"], day_sum["miss"]))
    print("  近 7 天：预警 %d、命中 %d、空报 %d、漏报 %d；FAR %s、POD %s"
          % (week_sum["warn"], week_sum["hit"], week_sum["false"], week_sum["miss"],
             fmt_rate(week_sum["far"], week_sum["warn"]), fmt_rate(week_sum["pod"], week_sum["hit"] + week_sum["miss"])))

    title = "📊 厦门短临 · 每日复盘 %s" % day[5:]
    body = build_push(day, day_sum, week_sum)
    if no_push:
        print("\n--no-push：跳过推送。\n---- 推送正文预览 ----\n%s" % body)
        return 0
    if not S.PUSH.get("ftqq", {}).get("enable"):
        print("\n方糖通道未启用（缺 XM_FTQQ_KEY），跳过推送。\n---- 正文预览 ----\n%s" % body)
        return 0
    try:
        r = S.push_ftqq(title, body, 0)
        print("\n方糖推送：%s" % (r[:200] if r else "(空响应)"))
    except Exception as e:
        print("\n方糖推送失败：%s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
