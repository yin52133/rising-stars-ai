# Rising Stars AI

GitHub AI 项目趋势监控：多来源发现候选，统一计算 Star 增长、速度与加速度，从约 1,000 个监控仓库生成各周期 Top 100。

- 系统设计：[docs/design.md](docs/design.md)
- 公开结果：`data/rankings/`（由流水线生成，可重算）

## 运行

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=...          # Windows: $env:GITHUB_TOKEN="..."

python -m rising_stars_ai daily   # 每日：发现 → 分类 → 候补 → 采集 → 指标 → 排名 → 发布
python -m rising_stars_ai weekly  # 每周：重排监控池后出榜
```

分阶段执行：

```bash
python -m rising_stars_ai discover
python -m rising_stars_ai classify
python -m rising_stars_ai pool
python -m rising_stars_ai stars --mode backfill   # 新入池 90 天回填
python -m rising_stars_ai stars --mode daily
python -m rising_stars_ai metrics --end-day 2026-10-31
python -m rising_stars_ai rank --end-day 2026-10-31
python -m rising_stars_ai publish --end-day 2026-10-31
```

`--db` 换数据库路径，`stars --limit N` 限制仓库数，便于本地调试。

## 输出

`data/rankings/`：`daily|weekly|monthly_{growth,acceleration,score}_top100.json`、`newcomers_top100.json`、`rankings_<day>.csv`、`meta.json`、`index.html`。

JSON 与 CSV 保留原始指标（本期/上期新增、速度、加速度、百分位、综合分），页面可离线按周期、领域、技术形态、项目类型筛选；页头标注监控池规模、统计窗口、更新时间与缺测状态。

## 测试

```bash
python -m pytest
```

固定样本覆盖增长、加速度、并列、窗口边界、历史不足与失败不写零。

## 已知边界

- Star 历史接口按 design.md 约定为 `GET /repos/{owner}/{repo}/stargazers/history`。该接口在公开 GitHub 上不可用时，采集把每次调用记为缺测，绝不写 0；此时榜单不会生成。
- Search API 单查询最多 1,000 条，查询按 Topic、Star 区间、创建时间分段；被截断的查询记在 `collection_runs`。
- 官方历史日/周边界不保证按 UTC 对齐，页面展示的是官方新增量，不与自己算的 UTC 快照相减。
- `data/*.db` 不入库；GitHub Actions 用 `actions/cache` 保温，缓存失效后按 90 天重新回填。
