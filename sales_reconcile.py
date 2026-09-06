"""수입금 표 — T머니(CSV) 실입금·건수 + .dat 운행 지표를 한 행으로 병합."""
from __future__ import annotations

from collections import OrderedDict, defaultdict

from app import (
    build_vehicle_lookup,
    compute_sales_match_status,
    compute_sales_summary,
    match_vehicle_record,
    normalize_emp_id,
    sales_dispatch_month_key,
    sales_record_key,
    _align_dat_rows_to_month_csv,
    _is_tmoney_csv_matched,
    _sales_match_identity_valid,
    _shift_band_for_sales_row,
    _lookup_car_entries,
    _snapshot_dat_match_identity,
    _DISPATCH_DAY_WORK_TYPES,
    _DISPATCH_NIGHT_WORK_TYPES,
)
from tmoney_parser import build_tmoney_lookups, build_tmoney_lookups_from_bytes

NUMERIC_SUM_FIELDS = ('영업시간', '연료비', '총시간', '빈차시간')
FLOAT_SUM_FIELDS = ('충전량', '운행거리', '총거리', '빈차거리')
DAT_TIME_FIELDS = ('영업시작', '영업종료', '마감시작', '마감종료')
TMONEY_METRIC_FIELDS = ('실입금', '건수')
TMONEY_IDENTITY_FIELDS = ('날짜', '차번', '근무유형', '사번', '이름', '차종', '차량번호')


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


def _is_blank_text(value) -> bool:
    text = str(value or '').strip()
    if not text or text == '-':
        return True
    return text.lower() in ('nan', 'none')


def _row_has_tmoney(row: dict) -> bool:
    return _is_tmoney_csv_matched(row)


def _row_has_dat(row: dict) -> bool:
    source = str(row.get('원본파일') or '').lower()
    if source and '.dat' in source:
        return True
    if str(row.get('집계기준') or '') in ('closing', 'daily_split'):
        return True
    if str(row.get('영업시작') or '').strip():
        return True
    return any(_as_float(row.get(field)) > 0 for field in FLOAT_SUM_FIELDS + NUMERIC_SUM_FIELDS)


def _copy_nonempty_fields(target: dict, source: dict, fields: tuple[str, ...]) -> None:
    for field in fields:
        value = source.get(field)
        if field == '차종' and _is_blank_text(value):
            continue
        if value not in (None, ''):
            target[field] = value


def _sum_dat_metrics(target: dict, source: dict) -> None:
    for field in NUMERIC_SUM_FIELDS:
        target[field] = str(_as_int(target.get(field)) + _as_int(source.get(field)))
    for field in FLOAT_SUM_FIELDS:
        total = _as_float(target.get(field)) + _as_float(source.get(field))
        target[field] = str(round(total, 2))


def merge_complementary_sales_rows(a: dict, b: dict) -> dict:
    """T머니 항목 + .dat 항목을 (날짜·차번·사번) 기준 한 행으로 병합."""
    left_has_tmoney = _row_has_tmoney(a)
    right_has_tmoney = _row_has_tmoney(b)
    left_has_dat = _row_has_dat(a)
    right_has_dat = _row_has_dat(b)

    if left_has_tmoney and not right_has_tmoney:
        tmoney_row, dat_row = a, b
    elif right_has_tmoney and not left_has_tmoney:
        tmoney_row, dat_row = b, a
    elif left_has_tmoney and right_has_tmoney:
        tmoney_row, dat_row = a, b
    else:
        tmoney_row, dat_row = (a, b) if _as_int(a.get('실입금')) >= _as_int(b.get('실입금')) else (b, a)

    if left_has_dat and right_has_dat and left_has_tmoney == right_has_tmoney:
        merged = dict(a)
        _sum_dat_metrics(merged, b)
        if right_has_tmoney and not left_has_tmoney:
            _copy_nonempty_fields(merged, b, TMONEY_METRIC_FIELDS + TMONEY_IDENTITY_FIELDS + ('T머니출처',))
        elif left_has_tmoney and not right_has_tmoney:
            _copy_nonempty_fields(merged, a, TMONEY_METRIC_FIELDS + TMONEY_IDENTITY_FIELDS + ('T머니출처',))
    else:
        merged = dict(dat_row if _row_has_dat(dat_row) else tmoney_row)
        if _row_has_dat(dat_row):
            for field in DAT_TIME_FIELDS + NUMERIC_SUM_FIELDS + FLOAT_SUM_FIELDS:
                if dat_row.get(field) not in (None, ''):
                    merged[field] = dat_row[field]
        if _row_has_tmoney(tmoney_row):
            _copy_nonempty_fields(merged, tmoney_row, TMONEY_METRIC_FIELDS + TMONEY_IDENTITY_FIELDS)
            if tmoney_row.get('T머니출처'):
                merged['T머니출처'] = tmoney_row['T머니출처']
            if tmoney_row.get('집계기준') == 'tmoney':
                merged.pop('집계기준', None)

    files = []
    for row in (a, b):
        raw = str(row.get('원본파일') or '')
        files.extend(p.strip() for p in raw.split(',') if p.strip())
    if files:
        merged['원본파일'] = ','.join(sorted(set(files)))

    for field in ('영업시작', '영업종료', '마감시작', '마감종료'):
        if not str(merged.get(field) or '').strip():
            for row in (a, b):
                if str(row.get(field) or '').strip():
                    merged[field] = row[field]
                    break

    if _row_has_dat(dat_row):
        stored = dat_row.get('_dat_match_identity')
        if _sales_match_identity_valid(stored):
            merged['_dat_match_identity'] = list(stored[:4])
        else:
            _snapshot_dat_match_identity(merged)

    compute_sales_match_status(merged)
    return merged


def _merge_row_metrics(target: dict, source: dict) -> None:
    """같은 (날짜·차번·사번) 중복 행 병합 — T머니·.dat 역할 분리."""
    merged = merge_complementary_sales_rows(target, source)
    target.clear()
    target.update(merged)


def dedupe_sales_rows(rows: list[dict]) -> list[dict]:
    """같은 날짜·차번·사번 중복 행(T머니+.dat)을 한 행으로 병합."""
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
        key = sales_record_key(row)
        if key not in merged:
            merged[key] = dict(row)
            order.append(key)
            continue
        _merge_row_metrics(merged[key], row)
    return [merged[key] for key in order]


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
            }
    fallback = suffixes.get(car, [{}])[0] if suffixes.get(car) else {}
    return {
        '사번': emp,
        '이름': '',
        '차종': car_type or fallback.get('차종', ''),
        '근무유형': fallback.get('근무유형', ''),
        '차량번호': fallback.get('차량번호', car),
    }


def _enrich_reconcile_row(row: dict, lookup) -> None:
    """CSV 대조 행: 사번·이름 유지, 배차로 차종·근무·차량번호 보조."""
    emp = normalize_emp_id(row.get('사번', ''))
    csv_name = str(row.get('이름') or '').strip()
    car_type = row.get('차종', '')
    if _is_blank_text(car_type):
        car_type = ''
    info = _lookup_driver_info(
        str(row.get('차번') or '').strip(),
        emp,
        lookup,
        car_type=car_type,
    )
    if emp:
        row['사번'] = emp
    if csv_name:
        row['이름'] = csv_name
    elif info.get('이름'):
        row['이름'] = info['이름']
    if _is_blank_text(row.get('차종')):
        row['차종'] = info.get('차종', '')
    if not str(row.get('근무유형') or '').strip():
        row['근무유형'] = info.get('근무유형', '')
    if not str(row.get('차량번호') or '').strip():
        row['차량번호'] = info.get('차량번호', row.get('차번', ''))


def _csv_metrics_for_dat_row(row, driver_map, lookup, car) -> tuple[str, dict | None]:
    """당월 CSV 기사 중 .dat 주·야와 맞는 emp (배차·이전월 사번보다 CSV 우선)."""
    dat_band = _shift_band_for_sales_row(row)
    entries = _lookup_car_entries(lookup, car)
    entry_by_emp = {normalize_emp_id(e.get('사번', '')): e for e in entries}

    if dat_band:
        for emp, metrics in driver_map.items():
            emp_n = normalize_emp_id(emp)
            entry = entry_by_emp.get(emp_n, {})
            work = str(entry.get('근무유형') or '').strip()
            if work in _DISPATCH_DAY_WORK_TYPES:
                csv_band = 'day'
            elif work in _DISPATCH_NIGHT_WORK_TYPES:
                csv_band = 'night'
            else:
                csv_band = _shift_band_for_sales_row({'근무유형': work})
            if csv_band == dat_band:
                return emp_n, metrics

    if len(driver_map) == 1:
        emp_n = normalize_emp_id(next(iter(driver_map.keys())))
        return emp_n, driver_map[emp_n]

    return '', None


def apply_tmoney_to_rows(rows: list[dict], tmoney: dict, lookup_cache: dict) -> tuple[list[dict], dict]:
    """CSV/T머니 기준 실입금·건수만 보정 (.dat 운행 지표는 유지)."""
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
        'no_driver_match': 0,
        'removed_orphans': 0,
    }

    for (date, car), group in groups.items():
        stats['groups'] += 1
        car_key = (date, car)
        car_metrics = by_car.get(car_key)
        if not car_metrics:
            for row in group:
                if not _row_has_tmoney(row):
                    row['실입금'] = row.get('실입금') or '0'
                    row['건수'] = row.get('건수') or '0'
                row['T머니출처'] = row.get('T머니출처') or 'no_tmoney'
                compute_sales_match_status(row)
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
                if metrics.get('car_type') and _is_blank_text(row.get('차종')):
                    row['차종'] = metrics['car_type']
                row['T머니출처'] = 'driver'
                stats['driver_match'] += 1
                used_emps.add(emp)
                month_key = sales_dispatch_month_key(date)
                if month_key not in lookup_cache:
                    lookup_cache[month_key] = build_vehicle_lookup(dispatch_month=month_key)
                _enrich_reconcile_row(row, lookup_cache[month_key])
                compute_sales_match_status(row)
                surviving.append(row)
                continue

            if len(group) == 1 and not driver_map:
                row['실입금'] = str(car_metrics['income'])
                row['건수'] = str(car_metrics['trip_count'])
                row['T머니출처'] = 'daily'
                stats['car_fallback'] += 1
                compute_sales_match_status(row)
                surviving.append(row)
                continue

            if _row_has_dat(row) and driver_map:
                month_key = sales_dispatch_month_key(date)
                if month_key not in lookup_cache:
                    lookup_cache[month_key] = build_vehicle_lookup(dispatch_month=month_key)
                lookup = lookup_cache[month_key]
                daily_date = str(row.get('날짜') or '').strip()[:10] if (
                    str(row.get('집계기준') or '') == 'daily_split'
                ) else None
                csv_emp, metrics = _csv_metrics_for_dat_row(
                    row, driver_map, lookup, str(row.get('차번') or '').strip(),
                )
                rematched = match_vehicle_record(
                    str(row.get('차번') or '').strip(),
                    plate=str(row.get('차량번호') or ''),
                    lookup=lookup,
                    business_date=date,
                    row=row,
                    daily_date=daily_date,
                )
                if not metrics:
                    dispatch_emp = normalize_emp_id(rematched.get('사번', ''))
                    metrics = driver_map.get(dispatch_emp)
                    csv_emp = dispatch_emp
                if metrics:
                    row['사번'] = csv_emp
                    row['이름'] = (
                        metrics.get('driver_name')
                        or rematched.get('이름')
                        or row.get('이름')
                        or ''
                    )
                    if metrics.get('car_type') and _is_blank_text(row.get('차종')):
                        row['차종'] = metrics['car_type']
                    if not str(row.get('근무유형') or '').strip():
                        row['근무유형'] = rematched.get('근무유형', '')
                    row['실입금'] = str(metrics['income'])
                    row['건수'] = str(metrics['trip_count'])
                    row['T머니출처'] = 'driver'
                    stats['driver_match'] += 1
                    used_emps.add(csv_emp)
                    _enrich_reconcile_row(row, lookup)
                    compute_sales_match_status(row)
                    surviving.append(row)
                    continue

            row['실입금'] = row.get('실입금') or '0'
            row['건수'] = row.get('건수') or '0'
            if not _row_has_dat(row):
                row['T머니출처'] = row.get('T머니출처') or 'no_driver_match'
            elif str(row.get('T머니출처') or '') in ('no_driver_match', 'no_tmoney'):
                row.pop('T머니출처', None)
            compute_sales_match_status(row)
            surviving.append(row)
            stats['no_driver_match'] += 1

        for emp, metrics in driver_map.items():
            if emp in used_emps:
                continue
            if emp == '000000' and len(surviving) >= 1:
                target = max(surviving, key=lambda r: (_as_int(r.get('영업시간')), _as_int(r.get('실입금'))))
                target['실입금'] = str(_as_int(target.get('실입금')) + int(metrics['income']))
                target['건수'] = str(_as_int(target.get('건수')) + int(metrics['trip_count']))
                target['T머니출처'] = target.get('T머니출처') or 'driver'
                used_emps.add(emp)
                compute_sales_match_status(target)
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
            }
            _enrich_reconcile_row(new_row, lookup_cache[month_key])
            compute_sales_match_status(new_row)
            surviving.append(new_row)
            stats['added_rows'] += 1

        if not surviving and len(group) == 1:
            row = dict(group[0])
            row['실입금'] = str(car_metrics['income'])
            row['건수'] = str(car_metrics['trip_count'])
            row['T머니출처'] = 'daily'
            stats['car_fallback'] += 1
            compute_sales_match_status(row)
            surviving.append(row)
        elif len(surviving) == 1 and not surviving[0].get('T머니출처'):
            surviving[0]['실입금'] = str(car_metrics['income'])
            surviving[0]['건수'] = str(car_metrics['trip_count'])
            surviving[0]['T머니출처'] = 'daily'
            stats['car_fallback'] += 1
            compute_sales_match_status(surviving[0])

        if surviving:
            assigned_inc = sum(_as_int(r.get('실입금')) for r in surviving)
            assigned_trips = sum(_as_int(r.get('건수')) for r in surviving)
            inc_gap = int(car_metrics['income']) - assigned_inc
            trip_gap = int(car_metrics['trip_count']) - assigned_trips
            if inc_gap or trip_gap:
                target = max(surviving, key=lambda r: (_as_int(r.get('영업시간')), _as_int(r.get('실입금'))))
                target['실입금'] = str(_as_int(target.get('실입금')) + inc_gap)
                target['건수'] = str(_as_int(target.get('건수')) + trip_gap)
                compute_sales_match_status(target)

        if surviving:
            _align_dat_rows_to_month_csv(surviving)
            surviving = dedupe_sales_rows(surviving)

        updated_rows.extend(surviving)

    return updated_rows, stats


def add_missing_tmoney_cars(rows: list[dict], tmoney: dict, lookup_cache: dict) -> tuple[list[dict], int]:
    """CSV에 있으나 표에 없는 (날짜·차번·사번) 행 추가."""
    by_car = tmoney['by_car']
    by_driver = tmoney['by_driver']
    present_keys = {sales_record_key(r) for r in rows}
    added = 0

    for (date, car), car_metrics in by_car.items():
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
                }
                _enrich_reconcile_row(new_row, lookup_cache[month_key])
                row_key = sales_record_key(new_row)
                if row_key in present_keys:
                    for idx, existing in enumerate(rows):
                        if sales_record_key(existing) == row_key:
                            rows[idx] = merge_complementary_sales_rows(existing, new_row)
                            break
                else:
                    compute_sales_match_status(new_row)
                    rows.append(new_row)
                    present_keys.add(row_key)
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
            }
            _enrich_reconcile_row(new_row, lookup_cache[month_key])
            row_key = sales_record_key(new_row)
            if row_key not in present_keys:
                compute_sales_match_status(new_row)
                rows.append(new_row)
                present_keys.add(row_key)
                added += 1

    return rows, added


def reconcile_month_rows(rows: list[dict], tmoney: dict, lookup_cache: dict | None = None) -> tuple[list[dict], dict]:
    """한 달 분량 행에 CSV 실입금·건수 반영 후 T머니·.dat 한 행으로 병합."""
    if lookup_cache is None:
        lookup_cache = {}
    rows = dedupe_sales_rows(rows)
    rows, stats = apply_tmoney_to_rows(rows, tmoney, lookup_cache)
    rows = dedupe_sales_rows(rows)
    rows, car_added = add_missing_tmoney_cars(rows, tmoney, lookup_cache)
    _align_dat_rows_to_month_csv(rows)
    rows = dedupe_sales_rows(rows)
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
