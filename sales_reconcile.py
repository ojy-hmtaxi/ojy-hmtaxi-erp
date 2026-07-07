"""수입금 표 — .dat 운행 지표 + T머니/CSV 실입금·건수 대조."""
from __future__ import annotations

from collections import OrderedDict, defaultdict

from app import (
    build_vehicle_lookup,
    compute_sales_summary,
    normalize_emp_id,
    sales_dispatch_month_key,
)
from tmoney_parser import build_tmoney_lookups, build_tmoney_lookups_from_bytes

NUMERIC_SUM_FIELDS = ('영업시간', '연료비', '총시간', '빈차시간')
FLOAT_SUM_FIELDS = ('충전량', '운행거리', '총거리', '빈차거리')


def _as_int(value, default=0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _merge_row_metrics(target: dict, source: dict) -> None:
    target['실입금'] = str(_as_int(target.get('실입금')) + _as_int(source.get('실입금')))
    target['건수'] = str(_as_int(target.get('건수')) + _as_int(source.get('건수')))
    for field in NUMERIC_SUM_FIELDS:
        target[field] = str(_as_int(target.get(field)) + _as_int(source.get(field)))
    for field in FLOAT_SUM_FIELDS:
        total = _as_float(target.get(field)) + _as_float(source.get(field))
        target[field] = str(round(total, 2))
    files = []
    for row in (target, source):
        raw = str(row.get('원본파일') or '')
        files.extend(p.strip() for p in raw.split(',') if p.strip())
    target['원본파일'] = ','.join(sorted(set(files)))
    for field in ('영업시작', '영업종료'):
        if not str(target.get(field) or '').strip():
            target[field] = source.get(field, '')


def dedupe_sales_rows(rows: list[dict]) -> list[dict]:
    """같은 날짜·차번·사번 중복 행(dat 다중 마감) 병합."""
    merged: dict[tuple, dict] = {}
    for row in rows:
        emp = normalize_emp_id(row.get('사번', ''))
        key = (str(row.get('날짜') or '').strip(), str(row.get('차번') or '').strip(), emp)
        if key not in merged:
            merged[key] = dict(row)
            continue
        _merge_row_metrics(merged[key], row)
    return list(merged.values())


def _lookup_driver_info(car: str, emp: str, lookup, car_type: str = '') -> dict:
    emp = normalize_emp_id(emp)
    suffixes = (lookup or {}).get('suffixes', {})
    for entry in suffixes.get(car, []):
        if normalize_emp_id(entry.get('사번', '')) == emp:
            return {
                '사번': emp,
                '이름': entry.get('이름', ''),
                '차종': entry.get('차종', '') or car_type,
                '근무유형': entry.get('근무유형', ''),
                '차량번호': entry.get('차량번호', car),
                '매칭': '완료',
            }
    fallback = suffixes.get(car, [{}])[0] if suffixes.get(car) else {}
    return {
        '사번': emp,
        '이름': '',
        '차종': car_type or fallback.get('차종', ''),
        '근무유형': fallback.get('근무유형', ''),
        '차량번호': fallback.get('차량번호', car),
        '매칭': '미매칭',
    }


def _enrich_reconcile_row(row: dict, lookup) -> None:
    """CSV 대조 행: 사번·이름 유지, 배차는 차종·근무·차량번호만 보조."""
    emp = normalize_emp_id(row.get('사번', ''))
    csv_name = str(row.get('이름') or '').strip()
    info = _lookup_driver_info(
        str(row.get('차번') or '').strip(),
        emp,
        lookup,
        car_type=str(row.get('차종') or ''),
    )
    if emp:
        row['사번'] = emp
    if csv_name:
        row['이름'] = csv_name
    elif info.get('이름'):
        row['이름'] = info['이름']
    if not str(row.get('차종') or '').strip():
        row['차종'] = info.get('차종', '')
    if not str(row.get('근무유형') or '').strip():
        row['근무유형'] = info.get('근무유형', '')
    if not str(row.get('차량번호') or '').strip():
        row['차량번호'] = info.get('차량번호', row.get('차번', ''))
    row['매칭'] = info.get('매칭', row.get('매칭', ''))


def apply_tmoney_to_rows(rows: list[dict], tmoney: dict, lookup_cache: dict) -> tuple[list[dict], dict]:
    """CSV/T머니 기준 실입금·건수 보정."""
    by_car = tmoney['by_car']
    by_driver = tmoney['by_driver']

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        key = (row.get('날짜', ''), row.get('차번', ''))
        groups[key].append(row)

    updated_rows: list[dict] = []
    stats = {
        'groups': 0,
        'driver_match': 0,
        'car_fallback': 0,
        'added_rows': 0,
        'no_tmoney': 0,
        'removed_orphans': 0,
    }

    for (date, car), group in groups.items():
        stats['groups'] += 1
        car_key = (date, car)
        car_metrics = by_car.get(car_key)
        if not car_metrics:
            for row in group:
                row['실입금'] = '0'
                row['건수'] = '0'
                row['T머니출처'] = 'no_tmoney'
                updated_rows.append(row)
            stats['no_tmoney'] += len(group)
            continue

        driver_keys = [k for k in by_driver if k[0] == date and k[1] == car]
        driver_map = {k[2]: by_driver[k] for k in driver_keys}

        used_emps: set[str] = set()
        surviving: list[dict] = []

        for row in group:
            emp = normalize_emp_id(row.get('사번', ''))
            metrics = driver_map.get(emp) if emp else None
            if metrics:
                row['실입금'] = str(metrics['income'])
                row['건수'] = str(metrics['trip_count'])
                row['사번'] = emp
                if metrics.get('driver_name'):
                    row['이름'] = metrics['driver_name']
                if metrics.get('car_type') and not str(row.get('차종') or '').strip():
                    row['차종'] = metrics['car_type']
                row['T머니출처'] = 'driver'
                stats['driver_match'] += 1
                used_emps.add(emp)
                surviving.append(row)
                continue

            if len(group) == 1 and not driver_map:
                row['실입금'] = str(car_metrics['income'])
                row['건수'] = str(car_metrics['trip_count'])
                row['T머니출처'] = 'daily'
                stats['car_fallback'] += 1
                surviving.append(row)
                continue

            stats['removed_orphans'] += 1

        for emp, metrics in driver_map.items():
            if emp in used_emps:
                continue
            if emp == '000000' and len(surviving) >= 1:
                target = max(surviving, key=lambda r: (_as_int(r.get('영업시간')), _as_int(r.get('실입금'))))
                target['실입금'] = str(_as_int(target.get('실입금')) + int(metrics['income']))
                target['건수'] = str(_as_int(target.get('건수')) + int(metrics['trip_count']))
                used_emps.add(emp)
                continue
            month_key = sales_dispatch_month_key(date)
            if month_key not in lookup_cache:
                lookup_cache[month_key] = build_vehicle_lookup(dispatch_month=month_key)
            info = _lookup_driver_info(
                car, emp, lookup_cache[month_key], car_type=metrics.get('car_type', ''),
            )
            new_row = {
                '날짜': date,
                '차번': car,
                '차량번호': info['차량번호'],
                '사번': emp,
                '이름': metrics.get('driver_name') or '',
                '차종': info['차종'] or metrics.get('car_type', ''),
                '근무유형': info['근무유형'],
                '매칭': info['매칭'],
                '실입금': str(metrics['income']),
                '건수': str(metrics['trip_count']),
                '영업시간': '0',
                '총시간': '0',
                '빈차시간': '0',
                '연료비': '0',
                '충전량': '0',
                '운행거리': '0',
                '총거리': '0',
                '빈차거리': '0',
                '원본파일': '',
                'T머니출처': 'driver_added',
                '집계기준': 'tmoney',
            }
            _enrich_reconcile_row(new_row, lookup_cache[month_key])
            surviving.append(new_row)
            stats['added_rows'] += 1

        if not surviving and len(group) == 1:
            row = dict(group[0])
            row['실입금'] = str(car_metrics['income'])
            row['건수'] = str(car_metrics['trip_count'])
            row['T머니출처'] = 'daily'
            stats['car_fallback'] += 1
            surviving.append(row)
        elif len(surviving) == 1 and not surviving[0].get('T머니출처'):
            surviving[0]['실입금'] = str(car_metrics['income'])
            surviving[0]['건수'] = str(car_metrics['trip_count'])
            surviving[0]['T머니출처'] = 'daily'
            stats['car_fallback'] += 1

        if surviving:
            assigned_inc = sum(_as_int(r.get('실입금')) for r in surviving)
            assigned_trips = sum(_as_int(r.get('건수')) for r in surviving)
            inc_gap = int(car_metrics['income']) - assigned_inc
            trip_gap = int(car_metrics['trip_count']) - assigned_trips
            if inc_gap or trip_gap:
                target = max(surviving, key=lambda r: (_as_int(r.get('영업시간')), _as_int(r.get('실입금'))))
                target['실입금'] = str(_as_int(target.get('실입금')) + inc_gap)
                target['건수'] = str(_as_int(target.get('건수')) + trip_gap)

        updated_rows.extend(surviving)

    return updated_rows, stats


def add_missing_tmoney_cars(rows: list[dict], tmoney: dict, lookup_cache: dict) -> tuple[list[dict], int]:
    """CSV에 있으나 표에 없는 (날짜·차번) 행 추가."""
    by_car = tmoney['by_car']
    by_driver = tmoney['by_driver']
    present = {(r.get('날짜', ''), r.get('차번', '')) for r in rows}
    added = 0

    for (date, car), car_metrics in by_car.items():
        if (date, car) in present:
            continue
        driver_keys = [k for k in by_driver if k[0] == date and k[1] == car]
        month_key = sales_dispatch_month_key(date)
        if month_key not in lookup_cache:
            lookup_cache[month_key] = build_vehicle_lookup(dispatch_month=month_key)

        if driver_keys:
            for key in driver_keys:
                emp = key[2]
                metrics = by_driver[key]
                info = _lookup_driver_info(
                    car, emp, lookup_cache[month_key], car_type=metrics.get('car_type', ''),
                )
                new_row = {
                    '날짜': date,
                    '차번': car,
                    '차량번호': info['차량번호'],
                    '사번': emp,
                    '이름': metrics.get('driver_name') or '',
                    '차종': info['차종'] or metrics.get('car_type', ''),
                    '근무유형': info['근무유형'],
                    '매칭': info['매칭'],
                    '실입금': str(metrics['income']),
                    '건수': str(metrics['trip_count']),
                    '영업시간': '0',
                    '총시간': '0',
                    '빈차시간': '0',
                    '연료비': '0',
                    '충전량': '0',
                    '운행거리': '0',
                    '총거리': '0',
                    '빈차거리': '0',
                    '원본파일': '',
                    'T머니출처': 'car_added',
                    '집계기준': 'tmoney',
                }
                _enrich_reconcile_row(new_row, lookup_cache[month_key])
                rows.append(new_row)
                added += 1
        else:
            info = _lookup_driver_info(car, '', lookup_cache[month_key])
            new_row = {
                '날짜': date,
                '차번': car,
                '차량번호': info['차량번호'],
                '사번': info['사번'],
                '이름': info['이름'],
                '차종': info['차종'],
                '근무유형': info['근무유형'],
                '매칭': info['매칭'],
                '실입금': str(car_metrics['income']),
                '건수': str(car_metrics['trip_count']),
                '영업시간': '0',
                '총시간': '0',
                '빈차시간': '0',
                '연료비': '0',
                '충전량': '0',
                '운행거리': '0',
                '총거리': '0',
                '빈차거리': '0',
                '원본파일': '',
                'T머니출처': 'car_added',
                '집계기준': 'tmoney',
            }
            _enrich_reconcile_row(new_row, lookup_cache[month_key])
            rows.append(new_row)
            added += 1

    return rows, added


def reconcile_month_rows(rows: list[dict], tmoney: dict, lookup_cache: dict | None = None) -> tuple[list[dict], dict]:
    """한 달 분량 행에 CSV 실입금·건수 반영."""
    if lookup_cache is None:
        lookup_cache = {}
    rows = dedupe_sales_rows(rows)
    rows, stats = apply_tmoney_to_rows(rows, tmoney, lookup_cache)
    rows = dedupe_sales_rows(rows)
    rows, car_added = add_missing_tmoney_cars(rows, tmoney, lookup_cache)
    stats['car_added'] = car_added
    rows.sort(key=lambda r: (
        r.get('날짜', ''),
        r.get('차번', ''),
        r.get('영업시작', ''),
        r.get('근무유형', ''),
        r.get('사번', ''),
    ))
    return rows, stats


def reconcile_sales_data(
    data: OrderedDict,
    tmoney: dict,
    month_keys: list[str] | None = None,
) -> tuple[OrderedDict, dict]:
    """sales_data.json 전체에서 지정 월 CSV 대조."""
    if month_keys is None:
        month_keys = tmoney.get('month_keys') or []
    lookup_cache = {}
    report = {'months': {}, 'tmoney_total_income': tmoney['daily_total_income'], 'tmoney_total_trips': tmoney['daily_total_trips']}

    for month_key in month_keys:
        if month_key not in data:
            continue
        month_rows = list(data[month_key].get('data', []))
        updated_rows, stats = reconcile_month_rows(month_rows, tmoney, lookup_cache)
        data[month_key]['data'] = updated_rows
        data[month_key]['summary'] = compute_sales_summary(updated_rows)
        sales_income = sum(_as_int(r.get('실입금')) for r in updated_rows)
        sales_trips = sum(_as_int(r.get('건수')) for r in updated_rows)
        report['months'][month_key] = {
            **stats,
            'rows_after': len(updated_rows),
            'sales_income': sales_income,
            'sales_trips': sales_trips,
        }

    return data, report


def reconcile_sales_with_csv_bytes(
    data: OrderedDict,
    csv_bytes: bytes,
    filename: str = '',
    month_keys: list[str] | None = None,
) -> tuple[OrderedDict, dict]:
    """업로드된 CSV 바이트로 sales_data 대조."""
    tmoney = build_tmoney_lookups_from_bytes(csv_bytes, filename=filename)
    return reconcile_sales_data(data, tmoney, month_keys=month_keys)


def reconcile_sales_with_csv_path(
    data: OrderedDict,
    csv_path: str,
    month_keys: list[str] | None = None,
) -> tuple[OrderedDict, dict]:
    tmoney = build_tmoney_lookups(detail_path=csv_path)
    return reconcile_sales_data(data, tmoney, month_keys=month_keys)
