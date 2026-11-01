"""命令行入口：分阶段运行，或一键 daily / weekly。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date

from . import api, classify, config, db, discover, metrics, publish, rank, select, stars


def _report(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _end_day(con, override: str | None) -> date | None:
    if override:
        return date.fromisoformat(override)
    return metrics.resolve_end_day(con)


def _pipeline_tail(con, cfg, now: str, end: date) -> dict:
    return {
        "metrics": metrics.compute_all(con, end, now),
        "rank": _rank_summary(con, cfg, end, now),
        "publish": publish.publish(con, cfg, end.isoformat(), generated_at=now),
    }


def _rank_summary(con, cfg, end: date, now: str) -> dict:
    result = rank.build(con, cfg, end.isoformat())
    return {"end_day": end.isoformat(), "pool_size": result["pool_size"],
            "eligible": result["eligible"], "boards": {name: len(rows)
                                                       for name, rows in result["boards"].items()}}


def run_daily(con, client, cfg, now: str) -> dict:
    """每日：discover → classify → pool 候补 → stars 增量 → metrics → rank → publish。"""
    run_id = db.start_run(con, "daily", now)
    result = {
        "discover": discover.run_search(con, client, cfg, run_id, now),
        "classify": classify.classify_pending(con, config.taxonomy(), now),
    }
    result["pool"] = select.build_pool(con, cfg, date.fromisoformat(now[:10]), now)
    result["backfill"] = stars.backfill(con, client, cfg, run_id, now)
    result["stars"] = stars.update_daily(con, client, cfg, run_id, now)
    end = metrics.resolve_end_day(con)
    if end is None:
        db.finish_run(con, run_id, "failed", client.requests, client.errors, None, "无 Star 历史")
        con.commit()
        return {**result, "error": "没有 Star 历史，榜单未生成"}
    result.update(_pipeline_tail(con, cfg, now, end))
    con.commit()
    db.finish_run(con, run_id, "ok" if client.errors == 0 else "partial",
                  client.requests, client.errors, end.isoformat())
    return result


def run_weekly(con, client, cfg, now: str) -> dict:
    """每周：先补数据，再重排监控池并出榜。"""
    run_id = db.start_run(con, "weekly", now)
    result = {
        "backfill": stars.backfill(con, client, cfg, run_id, now),
        "stars": stars.update_daily(con, client, cfg, run_id, now),
        "classify": classify.classify_pending(con, config.taxonomy(), now),
        "pool": select.build_pool(con, cfg, date.fromisoformat(now[:10]), now),
    }
    end = metrics.resolve_end_day(con)
    if end is None:
        db.finish_run(con, run_id, "failed", client.requests, client.errors, None, "无 Star 历史")
        con.commit()
        return {**result, "error": "没有 Star 历史，榜单未生成"}
    result.update(_pipeline_tail(con, cfg, now, end))
    con.commit()
    db.finish_run(con, run_id, "ok" if client.errors == 0 else "partial",
                  client.requests, client.errors, end.isoformat())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rising-stars-ai", description="GitHub AI 项目趋势监控")
    parser.add_argument("--db", default="data/rising_stars.db")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="Search API 发现候选")
    sub.add_parser("classify", help="AI 相关性与多标签分类")
    sub.add_parser("pool", help="重排监控池")
    stars_cmd = sub.add_parser("stars", help="Star 历史采集")
    stars_cmd.add_argument("--mode", choices=["backfill", "daily"], default="daily")
    stars_cmd.add_argument("--limit", type=int, default=None)
    for name, text in (("metrics", "计算窗口指标"), ("rank", "生成 Top 100"),
                       ("publish", "导出 JSON、CSV 与静态页")):
        sub.add_parser(name, help=text).add_argument("--end-day", default=None)
    sub.add_parser("daily", help="每日流水线")
    sub.add_parser("weekly", help="每周重排与出榜")
    args = parser.parse_args(argv)

    con = db.connect(args.db)
    cfg = config.sources()
    client = api.Client(token=args.token)
    now = db.now_iso()

    if args.command == "discover":
        run_id = db.start_run(con, "discover", now)
        _report(discover.run_search(con, client, cfg, run_id, now))
        db.finish_run(con, run_id, "ok" if client.errors == 0 else "partial",
                      client.requests, client.errors)
    elif args.command == "classify":
        _report(classify.classify_pending(con, config.taxonomy(), now))
    elif args.command == "pool":
        _report(select.build_pool(con, cfg, date.fromisoformat(now[:10]), now))
    elif args.command == "stars":
        run_id = db.start_run(con, f"stars-{args.mode}", now)
        collect = stars.backfill if args.mode == "backfill" else stars.update_daily
        _report(collect(con, client, cfg, run_id, now, limit=args.limit))
        db.finish_run(con, run_id, "ok" if client.errors == 0 else "partial",
                      client.requests, client.errors)
    elif args.command == "daily":
        _report(run_daily(con, client, cfg, now))
    elif args.command == "weekly":
        _report(run_weekly(con, client, cfg, now))
    else:
        end = _end_day(con, args.end_day)
        if end is None:
            parser.error("没有 Star 历史，先运行 stars --mode backfill")
        if args.command == "metrics":
            _report(metrics.compute_all(con, end, now))
        elif args.command == "rank":
            _report(_rank_summary(con, cfg, end, now))
        else:
            _report(publish.publish(con, cfg, end.isoformat(), generated_at=now))

    con.commit()
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
