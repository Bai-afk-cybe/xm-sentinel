# 厦门短临预警哨兵（云端版）

跑在 GitHub Actions 上的厦门雷达哨兵：每 6 分钟拉 NMC 华东雷达拼图，按《厦门短临预警台》
同源五档判据定级，触发即推送（方糖 → 微信；ntfy → 各订阅者专属主题）。
电脑关机也照常运行。

## 首次部署（只做一次）

1. 登录 github.com → 右上角 + → New repository
   - 名字：`xm-sentinel`（随便取，下同）
   - 选 **Public**（公开仓库 Actions 免费不限量；本仓库不含任何密钥，状态文件也无隐私）
   - **不要**勾选 README / .gitignore / license，直接 Create
2. 配置方糖密钥：仓库页 → Settings → Secrets and variables → Actions
   → New repository secret → Name 填 `XM_FTQQ_KEY`，Secret 填方糖 SendKey（SCT 开头那串）
3. 上传代码：仓库主页 → "uploading an existing file" 链接
   → 把本目录里的全部内容拖进去（包含 .github 文件夹）→ Commit changes
4. 启用 Actions：仓库 Actions 标签页 → 如提示信任，点 "I understand…enable"
5. 试跑：Actions → xm-sentinel → Run workflow → Run
   → 绿勾 = 通了；红叉 = 点进去把报错日志发出来

## 与本地版的关系

- 判据同源：本文件由本地 `xm_sentinel.py` 复制而来，唯一改动是
  方糖 SendKey 不再写死在代码里，改从环境变量 `XM_FTQQ_KEY` 读取（GitHub Secrets 提供）。
- **改判据时两边要同步改**（本地版和本目录），或者干脆只改这边、把本地计划任务停掉。
- `.state.json` 会随每轮运行自动提交回来，作为下一轮的"记忆"
  （last_push 节流、near5 临近确认标记、各订阅者状态分片）。
- 首次运行前仓库里没有 `.state.json` 属正常：第一轮会自动生成。
  （若切换时正处于预警过程中，想避免重复/漏推，可把本地 `xm_sentinel/.state.json` 手动传一份上去。）

## 已知差异（相对本地计划任务版）

- GitHub 定时器标称 6 分钟，实际可能推迟 3~10 分钟（高峰期更明显）——
  比"电脑关机完全没推送"强得多，但比本地准点 6 分钟略钝。
- 每轮运行会留下一条 state 提交，仓库历史会长，属正常。
- GitHub 定时工作流在仓库 60 天无活动后会被平台自动停用——本哨兵每轮都提交状态，
  不会触发；若长期停用后再启用，去 Actions 页面手动 enable 一次即可。
