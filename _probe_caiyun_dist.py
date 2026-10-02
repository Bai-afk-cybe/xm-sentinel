#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：彩云 `precipitation.nearest` 距离 vs 哨兵从 NMC 雷达自算的「最近回波距离」。

目的
----
彩云 realtime 免费带一个 `precipitation.nearest = {distance, intensity}`，语义是
「离该点最近的降水云团有多远」。我们自己的判据里也有一个同量纲的数：从 NMC 华东拼图
（ECREF AECN）里找 `dist`＝最近 ≥35 dBZ 像素距离、`dist40`＝最近 ≥40 dBZ 像素距离。

两者是**两条互相独立的链路**（彩云自建雷达拼图＋地面站融合 vs 中央气象台 ECREF），
若量级与排序一致，就能当「距离判据」的旁证；差得离谱即为判据存疑。

⚠️ 但两者**口径不同**（门槛、探测灵敏度、距离定义），所以本探针只做**标定测量**，
不预设结论、不写生产文件。测出关系后再决定是否接进采集/判分。

用法：python _probe_caiyun_dist.py
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xm_sentinel.py")
spec = importlib.util.spec_from_file_location("sent", P)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

POINTS = m.TERMS + m.CITIES if m.MODE == "stations" else m.CUSTOM_POINTS
TOKEN = os.environ.get("XM_CAIYUN_TOKEN", "")


def caiyun_one(lng, lat, timeout=20):
    url = "https://api.caiyunapp.com/v2.6/%s/%s,%s/realtime" % (TOKEN, lng, lat)
    js = json.loads(urllib.request.urlopen(url, timeout=timeout).read().decode("utf-8"))
    rt = (js.get("result") or {}).get("realtime") or {}
    pr = rt.get("precipitation") or {}
    lo = pr.get("local") or {}
    ne = pr.get("nearest") or {}
    return {
        "local_i": lo.get("intensity"),
        "local_src": lo.get("datasource"),
        "nb_dist": ne.get("distance"),
        "nb_i": ne.get("intensity"),
        "skycon": rt.get("skycon"),
    }


def main():
    if not TOKEN:
        print("!! 先设 XM_CAIYUN_TOKEN")
        return 1

    # ---- 1) 雷达侧：最新帧的逐点距离 ----
    latest = m.fetch_latest_ts()
    if latest is None:
        print("!! 拉帧失败")
        return 1
    lag_min = (time.time() * 1000 - latest) / 60000.0
    print("最新帧：%s（图龄 %.1f 分钟）" % (m.bj(latest), lag_min))
    d = m.http_get(m.frame_url(latest), 25)
    if not d or len(d) < 2000:
        print("!! 帧下载失败")
        return 1
    bbox = m.points_bbox(POINTS, m.SEARCH_KM)
    _w, _h, px = m.strong_pixels(d, m.DBZ_SEARCH, bbox)
    ppx = [m.latlon_to_px(la, lo) for _, la, lo in POINTS]
    fm = m.frame_metrics(ppx, px)
    print("雷达：≥%d dBZ 像素 %d 个（已做连通域过滤 MIN_CELLS=%d）\n"
          % (m.DBZ_SEARCH, len(px), m.MIN_CELLS))

    # ---- 2) 彩云侧：同点位同刻 ----
    print("%-22s %8s %8s | %8s %8s | %s"
          % ("监测对象", "雷达dist", "雷达d40", "彩云nb", "彩云nb雨", "彩云local"))
    print("-" * 96)
    rows = []
    for k, (name, la, lo) in enumerate(POINTS):
        if k:
            time.sleep(m.__dict__.get("CAIYUN_MIN_INTERVAL", 1.2) if False else 1.2)
        try:
            cy = caiyun_one(lo, la)
        except Exception as e:
            print("%-22s  取数失败 %s" % (name, e))
            continue
        dist, bear, deg, peak, d40 = fm[k]
        rows.append((name, dist, d40, cy["nb_dist"], cy["nb_i"], cy["local_i"], cy["local_src"]))
        f = lambda v: "—" if v is None else "%.1f" % v
        print("%-22s %8s %8s | %8s %8s | %s"
              % (name, f(dist), f(d40), f(cy["nb_dist"]),
                 ("%.2f" % cy["nb_i"]) if cy["nb_i"] is not None else "—",
                 "%s / %s" % (cy["local_i"], cy["local_src"])))

    # ---- 3) 关系汇总 ----
    both = [r for r in rows if r[3] is not None and (r[1] is not None or r[2] is not None)]
    print("\n有雨侧样本 %d / 全部 %d" % (len(both), len(rows)))
    if both:
        print("逐条差（彩云nb − 雷达d40）：")
        for r in both:
            base = r[2] if r[2] is not None else r[1]
            print("  %-22s 彩云%7.1f  雷达d40 %7s  差 %+7.1f"
                  % (r[0], r[3], "—" if r[2] is None else "%.1f" % r[2], r[3] - base))
    zeros = [r for r in rows if r[3] == 0]
    if zeros:
        print("\n彩云报『降水就在本点(nb=0)』的点位：%d 个 → %s"
              % (len(zeros), "、".join(r[0] for r in zeros)))
    print("\n提示：彩云 nearest 门槛低于 35 dBZ，nb 通常 ≤ 雷达 dist；"
          "看的是**同增同减的排序一致性**，而不是绝对值相等。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
