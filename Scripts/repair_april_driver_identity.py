#!/usr/bin/env python
"""4월 수입금: CSV 재대조 (운전자별수입금상세내역 CSV 필요).

코드 수정(사번 우선 매칭·T머니 이름 보존) 후 CSV를 다시 업로드하면
1814 등 주·야 2인 차량의 실입금·건수가 운전자별로 분리됩니다.
"""
from __future__ import annotations

import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import invalidate_dispatch_caches, save_sales_data, _read_sales_data_raw  # noqa: E402
from sales_reconcile import reconcile_sales_with_csv_path  # noqa: E402
from tmoney_parser import find_tmoney_csv_file  # noqa: E402

MONTH_KEY = '04월'


def main():
    invalidate_dispatch_caches()
    csv_path = find_tmoney_csv_file(ROOT / 'uploads')
    if not csv_path:
        raise SystemExit(
            'uploads 폴더에 운전자별수입금상세내역 CSV를 넣은 뒤 다시 실행하세요.\n'
            '또는 수입금 화면에서 CSV + DAT을 일괄 업로드하세요.'
        )
    data = _read_sales_data_raw() or OrderedDict()
    with __import__('app').app.app_context():
        data, report = reconcile_sales_with_csv_path(data, str(csv_path), month_keys=[MONTH_KEY])
        save_sales_data(data, normalize=False)
    print('=== 4월 CSV 재대조 완료 ===')
    for key, value in report.items():
        print(f'{key}: {value}')


if __name__ == '__main__':
    main()
