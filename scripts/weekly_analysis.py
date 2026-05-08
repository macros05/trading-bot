"""Generate the weekly CSV+JSON analysis bundle.

Usage:
    python -m scripts.weekly_analysis [--out-dir data/reports]

Outputs:
    data/reports/weekly_YYYY-MM-DD.csv     — all live trades with metadata
    data/reports/weekly_YYYY-MM-DD.json    — evaluation + per-condition + readiness
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.live_db import list_live_trades
from analytics.validation import (
    evaluate, per_condition_analysis, readiness_check, underperforming_buckets,
)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)-8s %(message)s')
logger = logging.getLogger(__name__)


def export_csv(trades: list[dict], path: Path) -> None:
    if not trades:
        path.write_text('')
        logger.info('no trades — wrote empty %s', path)
        return
    fieldnames = list(trades[0].keys())
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(trades)
    logger.info('wrote %s rows=%d', path, len(trades))


def days_running_estimate() -> int:
    db = Path('data/live_trades.db')
    if not db.exists():
        return 0
    age = (datetime.now(timezone.utc).timestamp() - db.stat().st_mtime) / 86_400
    return max(1, int(age))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--out-dir', default='data/reports')
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    trades = list_live_trades()
    csv_path = out_dir / f'weekly_{today}.csv'
    json_path = out_dir / f'weekly_{today}.json'

    export_csv(trades, csv_path)

    days = days_running_estimate()
    eval_ = evaluate(trades, days)
    conditions = per_condition_analysis(trades)
    weak = underperforming_buckets(conditions)
    readiness = readiness_check(trades, days)

    bundle = {
        'date': today,
        'days_running': days,
        'evaluation': eval_,
        'conditions': conditions,
        'underperforming_buckets': weak,
        'readiness': readiness,
    }
    json_path.write_text(json.dumps(bundle, indent=2, default=str))
    logger.info('wrote %s', json_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
