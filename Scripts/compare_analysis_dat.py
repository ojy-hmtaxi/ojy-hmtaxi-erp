#!/usr/bin/env python
"""운행분석 CSV vs uploads/dat 대조."""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    allowed_dat_file,
    build_vehicle_lookup,
    match_vehicle_record,
    normalize_emp_id,
    parse_dat_bytes,
    sales_dispatch_month_key,
)
from dat_parser import compute_closing_sales_metrics, resolve_closing_business_date  # noqa: E402

ANALYSIS_CSV = ROOT / 'uploads' / '운행분석_2026-04-01_2026-04-30.csv'
DAT_DIR = ROOT / 'uploads' / 'dat'


def car_suffix(plate: str) -> str:
    m = re.search(r'(\d{4})', str(plate or ''))
    return m.group(1) if m else ''


def parse_hms_to_minutes(text: str) -> int:
    s = str(text or '').strip()
    if not s or s.lower() == 'nan':
        return 0
    parts = s.split(':')
    if len(parts) == 3:
        h, m, sec = (int(p) for p in parts)
        return h * 60 + m + (1 if sec >= 30 else 0)
    if len(parts) == 2:
        h, m = (int(p) for p in parts)
        return h * 60 + m
    return 0


def parse_income(text) -> int:
    return int(str(text or '0').replace(',', '').strip() or 0)


def load_analysis() -> pd.DataFrame:
    df = pd.read_csv(ANALYSIS_CSV, encoding='utf-8-sig')
    df['date'] = df['날짜'].astype(str).str.strip()
    df['emp'] = df['사번'].astype(str).str.replace(r'\.0$', '', regex=True)
    df['car'] = df['담당차량'].map(car_suffix)
    df['trips'] = pd.to_numeric(df['운임건수'], errors='coerce').fillna(0).astype(int)
    df['income'] = df['수입'].map(parse_income)
    df['work_min'] = df['영업시간'].map(parse_hms_to_minutes)
    df['distance'] = (
        df['주행거리'].astype(str).str.replace('km', '', regex=False).str.strip()
    )
    df['distance'] = pd.to_numeric(df['distance'], errors='coerce').fillna(0.0)
    df['out_dt'] = pd.to_datetime(df['출고일시'], errors='coerce')
    df['in_dt'] = pd.to_datetime(df['입고일시'], errors='coerce')
    return df[df['date'].str.match(r'^\d{4}-\d{2}-\d{2}$', na=False)].copy()


def load_dat_index() -> dict:
    """(날짜, 차번, 사번) -> list of dat metrics (동일 키 다중 마감 가능)."""
    lookup_cache = {}
    index: dict[tuple, list] = defaultdict(list)
    files = sorted(DAT_DIR.glob('202604*.dat'))
    for path in files:
        if not allowed_dat_file(path.name):
            continue
        parsed = parse_dat_bytes(path.read_bytes(), path.name)
        header = parsed.get('header') or {}
        car = (
            parsed.get('file_car_suffix')
            or header.get('car_suffix')
            or car_suffix(header.get('plate', ''))
        )
        closing = (
            parsed.get('closing_date')
            or resolve_closing_business_date(parsed)
            or str(header.get('end', ''))[:10]
        )
        if not car or not closing:
            continue
        month_key = sales_dispatch_month_key(closing)
        if month_key not in lookup_cache:
            lookup_cache[month_key] = build_vehicle_lookup(dispatch_month=month_key)
        match = match_vehicle_record(
            car, header.get('plate', ''), lookup=lookup_cache[month_key],
            business_date=closing, parsed=parsed,
        )
        emp = normalize_emp_id(match.get('사번', ''))
        metrics = compute_closing_sales_metrics(parsed)
        index[(closing, car, emp)].append({
            'file': path.name,
            'emp': emp,
            'name': match.get('이름', ''),
            'start': str(header.get('start', ''))[:16],
            'end': str(header.get('end', ''))[:16],
            'income': int(metrics['income_won']),
            'trips': int(metrics['fare_count']),
            'work_min': int(metrics['work_minutes']),
            'distance': float(metrics['distance_km']),
        })
    return dict(index)


def merge_dat_entries(entries: list[dict]) -> dict:
    if len(entries) == 1:
        return entries[0]
    return {
        'file': ','.join(e['file'] for e in entries),
        'emp': entries[0]['emp'],
        'name': entries[0]['name'],
        'start': min(e['start'] for e in entries if e['start']),
        'end': max(e['end'] for e in entries if e['end']),
        'income': sum(e['income'] for e in entries),
        'trips': sum(e['trips'] for e in entries),
        'work_min': sum(e['work_min'] for e in entries),
        'distance': round(sum(e['distance'] for e in entries), 2),
    }


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    analysis = load_analysis()
    dat_index = load_dat_index()

    print('=== 데이터 규모 ===')
    print(f'운행분석 행: {len(analysis)}')
    print(f'  고유 (날짜·차번·사번): {analysis.groupby(["date","car","emp"]).ngroups}')
    print(f'  고유 차번: {analysis["car"].nunique()}, 고유 사번: {analysis["emp"].nunique()}')
    print(f'.dat 파일: {len(list(DAT_DIR.glob("202604*.dat")))}')
    print(f'  .dat 인덱스 키 (날짜·차번·사번): {len(dat_index)}')

    # also index by (date, car) only for dat
    dat_by_car: dict[tuple, list] = defaultdict(list)
    for (d, c, _e), entries in dat_index.items():
        dat_by_car[(d, c)].extend(entries)

    matched_exact = 0
    matched_car_only = 0
    no_dat = 0
    income_ok = income_close = income_bad = 0
    trips_ok = trips_close = trips_bad = 0
    work_ok = work_close = work_bad = 0
    dist_ok = dist_close = dist_bad = 0
    time_start_match = 0
    mismatches = []

    for row in analysis.itertuples():
        key = (row.date, row.car, row.emp)
        dat_entries = dat_index.get(key)
        if dat_entries:
            matched_exact += 1
            dat = merge_dat_entries(dat_entries)
        else:
            car_entries = dat_by_car.get((row.date, row.car))
            if car_entries:
                matched_car_only += 1
                dat = merge_dat_entries(car_entries)
            else:
                no_dat += 1
                mismatches.append({
                    'type': 'no_dat',
                    'date': row.date, 'car': row.car, 'emp': row.emp,
                    'name': row.이름, 'a_inc': row.income, 'a_trips': row.trips,
                })
                continue

        inc_diff = dat['income'] - row.income
        trip_diff = dat['trips'] - row.trips
        work_diff = dat['work_min'] - row.work_min
        dist_diff = dat['distance'] - row.distance

        def bucket(diff, ok_th=0, close_th=0.05):
            nonlocal income_ok, income_close, income_bad
            nonlocal trips_ok, trips_close, trips_bad
            nonlocal work_ok, work_close, work_bad
            nonlocal dist_ok, dist_close, dist_bad
            return

        # income
        if inc_diff == 0:
            income_ok += 1
        elif row.income and abs(inc_diff) / row.income <= 0.05:
            income_close += 1
        else:
            income_bad += 1
        # trips
        if trip_diff == 0:
            trips_ok += 1
        elif row.trips and abs(trip_diff) / row.trips <= 0.1:
            trips_close += 1
        else:
            trips_bad += 1
        # work minutes
        if work_diff == 0:
            work_ok += 1
        elif row.work_min and abs(work_diff) / row.work_min <= 0.1:
            work_close += 1
        else:
            work_bad += 1
        # distance
        if abs(dist_diff) < 0.5:
            dist_ok += 1
        elif row.distance and abs(dist_diff) / row.distance <= 0.1:
            dist_close += 1
        else:
            dist_bad += 1

        # time window: analysis 출고~입고 vs dat start~end (exact key only)
        if dat_entries and pd.notna(row.out_dt):
            try:
                dat_start = pd.to_datetime(dat['start'])
                dat_end = pd.to_datetime(dat['end'])
                if abs((dat_start - row.out_dt).total_seconds()) < 3600:
                    time_start_match += 1
            except Exception:
                pass

        if dat_entries and (
            abs(inc_diff) > max(5000, row.income * 0.1)
            or abs(trip_diff) > max(2, row.trips * 0.2)
            or (row.work_min and abs(work_diff) > max(30, row.work_min * 0.2))
        ):
            mismatches.append({
                'type': 'metric_diff',
                'date': row.date, 'car': row.car, 'emp': row.emp,
                'name': row.이름,
                'a_inc': row.income, 'd_inc': dat['income'],
                'a_trips': row.trips, 'd_trips': dat['trips'],
                'a_work': row.work_min, 'd_work': dat['work_min'],
                'a_dist': round(row.distance, 2), 'd_dist': dat['distance'],
                'dat_file': dat['file'][:40],
                'dat_emp': dat['emp'], 'dat_name': dat['name'],
            })

    n = len(analysis)
    print('\n=== 키 매칭 (운행분석 행 기준) ===')
    print(f'  (날짜·차번·사번) 정확 일치 .dat: {matched_exact}/{n} ({matched_exact/n*100:.1f}%)')
    print(f'  (날짜·차번)만 일치 (.dat 사번 다름): {matched_car_only}/{n} ({matched_car_only/n*100:.1f}%)')
    print(f'  .dat 없음: {no_dat}/{n} ({no_dat/n*100:.1f}%)')

    print('\n=== 지표 일치 (매칭된 {0}건 중) ==='.format(n - no_dat))
    m = n - no_dat
    print(f'  수입:   일치 {income_ok} | 5%이내 {income_close} | 불일치 {income_bad}')
    print(f'  건수:   일치 {trips_ok} | 10%이내 {trips_close} | 불일치 {trips_bad}')
    print(f'  영업시간(분): 일치 {work_ok} | 10%이내 {work_close} | 불일치 {work_bad}')
    print(f'  주행거리: 일치 {dist_ok} | 10%이내 {dist_close} | 불일치 {dist_bad}')
    print(f'  출고시각 vs .dat 시작 1시간 이내 (사번일치 건): {time_start_match}/{matched_exact}')

    print('\n=== .dat 없음 샘플 (최대 8건) ===')
    for item in [x for x in mismatches if x['type'] == 'no_dat'][:8]:
        print(f"  {item['date']} 차번{item['car']} 사번{item['emp']} {item['name']} 수입{item['a_inc']:,} 건수{item['a_trips']}")

    print('\n=== 지표 차이 큰 사번일치 샘플 (최대 10건) ===')
    big = [x for x in mismatches if x['type'] == 'metric_diff']
    big.sort(key=lambda x: abs(x['a_inc'] - x['d_inc']), reverse=True)
    for item in big[:10]:
        print(
            f"  {item['date']} {item['car']} 사번{item['emp']}({item['name']}) "
            f"수입 분석{item['a_inc']:,} dat{item['d_inc']:,} | "
            f"건수 {item['a_trips']}/{item['d_trips']} | "
            f"영업분 {item['a_work']}/{item['d_work']} | "
            f"거리 {item['a_dist']}/{item['d_dist']} | dat={item['dat_file']}"
        )

    # car-only mismatch: different driver on dat
    print('\n=== (날짜·차번)만 일치 — .dat 배차 사번 샘플 ===')
    shown = 0
    for row in analysis.itertuples():
        key = (row.date, row.car, row.emp)
        if key in dat_index:
            continue
        car_entries = dat_by_car.get((row.date, row.car))
        if not car_entries:
            continue
        dat = merge_dat_entries(car_entries)
        if dat['emp'] != row.emp:
            print(
                f"  {row.date} 차번{row.car}: 분석 사번{row.emp} {row.이름} "
                f"↔ .dat 사번{dat['emp']} {dat['name']} ({dat['file'][:30]})"
            )
            shown += 1
            if shown >= 8:
                break

    print('\n=== 결론 ===')
    if matched_exact / n > 0.5 and work_ok + work_close > m * 0.5:
        print('운행분석과 .dat는 (날짜·차번·사번) 기준으로 상당 부분 매칭되며,')
        print('영업시간·거리는 비교적 잘 맞는 편입니다. 수입·건수는 정의 차이로 다를 수 있습니다.')
    else:
        print('키 매칭 또는 지표 일치율이 낮습니다. 아래 샘플을 확인하세요.')


if __name__ == '__main__':
    main()
