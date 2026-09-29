"""Command line: `python -m retailpulse build --out public`"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, generate, report


def build(out: Path, seed: int, orders: int, db_path: str | None) -> int:
    t0 = time.perf_counter()
    data = generate.generate(generate.Config(seed=seed, orders=orders))
    conn = db.connect(db_path or ":memory:")
    db.create_schema(conn)
    db.load(conn, data)

    results = {name: db.run_query(conn, name) for name in db.list_queries()}
    sql_text = {name: (db.SQL_DIR / f"{name}.sql").read_text() for name in results}
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "elapsed_ms": elapsed_ms,
        "customers": len(data["customers"]),
        "orders": len(data["orders"]),
        "line_items": len(data["order_items"]),
        "products": len(data["products"]),
        "stores": len(data["stores"]),
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(report.build_html(results, sql_text, meta), encoding="utf-8")
    (out / "data.json").write_text(json.dumps({"meta": meta, "results": results}, indent=1), encoding="utf-8")

    violations = sum(r["violations"] for r in results["08_data_quality"])
    print(f"RetailPulse built -> {out/'index.html'}  ({meta['orders']:,} orders, {meta['line_items']:,} items, {elapsed_ms} ms)")
    if violations:
        print(f"DATA QUALITY FAILED: {violations} violation(s)", file=sys.stderr)
        return 1
    print("Data quality: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="retailpulse")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="generate data, run SQL, render dashboard")
    b.add_argument("--out", type=Path, default=Path("public"))
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("--orders", type=int, default=30_000)
    b.add_argument("--db", default=None, help="optional path to persist the SQLite database")
    a = p.parse_args(argv)
    if a.cmd == "build":
        return build(a.out, a.seed, a.orders, a.db)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
