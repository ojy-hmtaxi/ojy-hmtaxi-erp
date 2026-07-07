#!/usr/bin/env python
"""4월 수입금: uploads/dat + uploads CSV로 sales_data.json 대조."""
from __future__ import annotations

import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    allowed_dat_file,
    build_dat_upload_sales_rows,
    merge_sales_records,
    parse_dat_bytes,
    save_sales_data,
    _read_sales_data_raw,
)
from sales_reconcile import reconcile_sales_with_csv_path  # noqa: E402
from tmoney_parser import find_tmoney_csv_file  # noqa: E402

MONTH_KEY = '04월'
DAT_GLOB = '202604*.dat'


def reprocess_april_from_dat(existing, dat_dir: Path):
    april_files = sorted(dat_dir.glob(DAT_GLOB))
    other_months = {k: v for k, v in existing.items() if k != MONTH_KEY}
    parsed_list = [
        parse_dat_bytes(p.read_bytes(), p.name)
        for p in april_files if allowed_dat_file(p.name)
    ]
    base = OrderedDict(other_months)
    base[MONTH_KEY] = {'data': [], 'summary': {}}
    return merge_sales_records(base, build_dat_upload_sales_rows(parsed_list, lookup_cache={}))


def main():
    with __import__('app').app.app_context():
        csv_path = find_tmoney_csv_file(ROOT / 'uploads')
        if not csv_path:
            raise SystemExit('uploads에 수입금 CSV가 없습니다.')
        existing = _read_sales_data_raw() or OrderedDict()
        merged = reprocess_april_from_dat(existing, ROOT / 'uploads' / 'dat')
        merged, report = reconcile_sales_with_csv_path(merged, csv_path, month_keys=[MONTH_KEY])
        save_sales_data(merged, normalize=False)
        print('=== 4월 수입금 CSV 대조 완료 ===')
        for key, value in report.items():
            print(f'{key}: {value}')


if __name__ == '__main__':
    main()
