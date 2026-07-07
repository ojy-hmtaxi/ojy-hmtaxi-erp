"""택시 미터기 .dat 파일 파서 (TIMS/미터기 전용 텍스트 형식)."""
import re
from datetime import datetime, timedelta
from typing import Any


def read_dat_text(raw: bytes) -> str:
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_dat_filename(filename: str) -> tuple[str | None, str | None]:
    """파일명에서 영업일(YYYY-MM-DD)과 차번 뒤 4자리 추출."""
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    match = re.match(r"(\d{8})_(\d{4})_", name, re.IGNORECASE)
    if not match:
        return None, None
    d = match.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}", match.group(2)


def parse_dat_content(content: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "header": {},
        "trips": [],
        "intervals": [],
        "duty": [],
    }

    header_match = re.match(
        r"#1_(.+?)(\d{4})(\d{8})(\d{4})(\d{8})(\d{4})(.+?)#2",
        content,
    )
    if header_match:
        sd, st, ed, et = (
            header_match.group(3),
            header_match.group(4),
            header_match.group(5),
            header_match.group(6),
        )
        result["header"] = {
            "plate": header_match.group(1) + header_match.group(2),
            "car_suffix": header_match.group(2),
            "start": f"{sd[:4]}-{sd[4:6]}-{sd[6:8]} {st[:2]}:{st[2:4]}",
            "end": f"{ed[:4]}-{ed[4:6]}-{ed[6:8]} {et[:2]}:{et[2:4]}",
            "raw_tail": header_match.group(7),
        }

    trip_block = re.search(r"#2(.+?)#3", content)
    if trip_block:
        block = trip_block.group(1)
        for i in range(0, len(block) - 19, 20):
            chunk = block[i : i + 20]
            result["trips"].append(
                {
                    "date": chunk[0:8],
                    "time": f"{chunk[8:10]}:{chunk[10:12]}",
                    "type": chunk[12:15],
                    "amount_won": int(chunk[15:20]) / 100,
                }
            )

    interval_block = re.search(r"#3(.+?)#4", content)
    if interval_block:
        pattern = (
            r"(\d{8})(\d{4})(\d{8})(\d{4})"
            r"(\d+\.\d{2})(\d{5})(\d{6})(\d+\.\d{2})"
        )
        for match in re.finditer(pattern, interval_block.group(1)):
            sd, st, ed, et = match.group(1), match.group(2), match.group(3), match.group(4)
            result["intervals"].append(
                {
                    "start": f"{sd[:4]}-{sd[4:6]}-{sd[6:8]} {st[:2]}:{st[2:4]}",
                    "end": f"{ed[:4]}-{ed[4:6]}-{ed[6:8]} {et[:2]}:{et[2:4]}",
                    "distance_km": float(match.group(5)),
                    "trip_count": int(match.group(6)),
                    "fuel_l": float(match.group(8)),
                }
            )

    duty_block = re.search(r"#4(.+)$", content)
    if duty_block:
        for match in re.finditer(r"(of|on)(\d{8})(\d{4})", duty_block.group(1)):
            dt, tm = match.group(2), match.group(3)
            result["duty"].append(
                {
                    "status": "off" if match.group(1) == "of" else "on",
                    "datetime": f"{dt[:4]}-{dt[4:6]}-{dt[6:8]} {tm[:2]}:{tm[2:4]}",
                }
            )

    result["trip_count"] = len(result["trips"])
    result["total_income_won"] = round(sum(t["amount_won"] for t in result["trips"]))
    result["total_distance_km"] = round(
        sum(i["distance_km"] for i in result["intervals"]), 2
    )
    result["total_fuel_l"] = round(sum(i["fuel_l"] for i in result["intervals"]), 2)
    return result


_INTERVAL_FMT = '%Y-%m-%d %H:%M'


def _day_bounds(business_date: str) -> tuple[datetime | None, datetime | None]:
    if not business_date:
        return None, None
    date_part = str(business_date).strip()[:10]
    try:
        day_start = datetime.strptime(f'{date_part} 00:00', _INTERVAL_FMT)
    except ValueError:
        return None, None
    return day_start, day_start + timedelta(days=1)


def _interval_minutes_on_day(interval, day_start: datetime, day_end: datetime) -> tuple[int, int]:
    """interval이 당일에 걸친 분(clipped)과 구간 전체 분(total) 반환."""
    try:
        start = datetime.strptime(str(interval['start']).strip()[:16], _INTERVAL_FMT)
        end = datetime.strptime(str(interval['end']).strip()[:16], _INTERVAL_FMT)
    except (ValueError, TypeError, KeyError):
        return 0, 0
    if end <= start:
        return 0, 0
    total = int((end - start).total_seconds() // 60)
    clip_start = max(start, day_start)
    clip_end = min(end, day_end)
    clipped = int((clip_end - clip_start).total_seconds() // 60) if clip_end > clip_start else 0
    return clipped, total


def compute_daily_trip_stats(trips, business_date: str) -> dict[str, int]:
    """당일 #2 수입 레코드 실입금·로그 건수(#2 전체 행 수, 영업 건수 아님)."""
    day_start, _ = _day_bounds(business_date)
    if not day_start:
        return {'income_won': 0, 'trip_count': 0}
    ymd = day_start.strftime('%Y%m%d')
    daily = [t for t in (trips or []) if str(t.get('date', '')).strip() == ymd]
    return {
        'income_won': round(sum(t['amount_won'] for t in daily)),
        'trip_count': len(daily),
    }


def compute_daily_fare_trip_count(trips, business_date: str) -> int:
    """당일 #2 영업 건수: amount=0(type 000) 레코드로 요금 누적 구간(승차~정산) 구분."""
    day_start, _ = _day_bounds(business_date)
    if not day_start:
        return 0
    ymd = day_start.strftime('%Y%m%d')

    trip_sum = 0.0
    count = 0
    for trip in trips or []:
        if str(trip.get('date', '')).strip() != ymd:
            continue
        amount = float(trip.get('amount_won', 0) or 0)
        if amount == 0:
            if trip_sum > 0:
                count += 1
                trip_sum = 0
        else:
            trip_sum += amount
    if trip_sum > 0:
        count += 1
    return count


def compute_daily_interval_trip_count(intervals, business_date: str) -> int:
    """당일 00:00~24:00에 시작하는 #3 구간의 trip_count 합(거리 펄스, 영업 건수 아님)."""
    day_start, day_end = _day_bounds(business_date)
    if not day_start:
        return 0
    total = 0
    for interval in intervals or []:
        try:
            start = datetime.strptime(str(interval['start']).strip()[:16], _INTERVAL_FMT)
        except (ValueError, TypeError, KeyError):
            continue
        if day_start <= start < day_end:
            total += int(interval.get('trip_count', 0) or 0)
    return total


def compute_daily_interval_stats(intervals, business_date: str) -> dict[str, float]:
    """당일 #3 구간에 해당하는 운행거리·연료(시간 비율 배분)."""
    day_start, day_end = _day_bounds(business_date)
    if not day_start:
        return {'distance_km': 0.0, 'fuel_l': 0.0}

    distance = 0.0
    fuel = 0.0
    for interval in intervals or []:
        clipped, total = _interval_minutes_on_day(interval, day_start, day_end)
        if clipped <= 0 or total <= 0:
            continue
        ratio = clipped / total
        distance += float(interval.get('distance_km', 0) or 0) * ratio
        fuel += float(interval.get('fuel_l', 0) or 0) * ratio

    return {
        'distance_km': round(distance, 2),
        'fuel_l': round(fuel, 2),
    }


def compute_daily_interval_minutes(intervals, business_date: str) -> int:
    """당일 00:00~24:00에 해당하는 #3 운행 구간(interval) 시작~종료 시간 합(분)."""
    day_start, day_end = _day_bounds(business_date)
    if not day_start:
        return 0
    total = 0
    for interval in intervals or []:
        clipped, _ = _interval_minutes_on_day(interval, day_start, day_end)
        total += clipped
    return total


def is_prolonged_closing(parsed: dict) -> bool:
    """#1 영업 구간이 2일 이상 달력일에 걸치면 장기 미마감(리스·장기 미전송 등).

    하루 넘김 야간(전날 21시~다음날 05시)은 1일 차이만 나므로 제외.
    """
    header = parsed.get('header') or {}
    start = str(header.get('start', '')).strip()
    end = str(header.get('end', '')).strip()
    if len(start) < 10 or len(end) < 10:
        return False
    try:
        start_d = datetime.strptime(start[:10], '%Y-%m-%d').date()
        end_d = datetime.strptime(end[:10], '%Y-%m-%d').date()
    except ValueError:
        return False
    return (end_d - start_d).days >= 2


def iter_closing_calendar_dates(parsed: dict):
    """장기 마감 #1 구간에 포함된 달력일(YYYY-MM-DD) 순회."""
    header = parsed.get('header') or {}
    start = str(header.get('start', '')).strip()
    end = str(header.get('end', '')).strip()
    if len(start) < 10 or len(end) < 10:
        return
    try:
        start_d = datetime.strptime(start[:10], '%Y-%m-%d').date()
        end_d = datetime.strptime(end[:10], '%Y-%m-%d').date()
    except ValueError:
        return
    day = start_d
    while day <= end_d:
        yield day.isoformat()
        day += timedelta(days=1)


def clip_datetime_to_business_day(dt_str: str, business_date: str, bound: str) -> str:
    """장기 마감 행 표시용 — 해당 영업일 00:00~24:00 안으로 #1 시각 클립."""
    day_start, day_end = _day_bounds(business_date)
    if not day_start or not dt_str:
        return dt_str
    try:
        dt = datetime.strptime(str(dt_str).strip()[:16], _INTERVAL_FMT)
    except ValueError:
        return dt_str
    if bound == 'start':
        clipped = max(dt, day_start)
    else:
        clipped = min(dt, day_end - timedelta(minutes=1))
    return clipped.strftime(_INTERVAL_FMT)


def compute_daily_interval_start_count(intervals, business_date: str) -> int:
    """당일 00:00~24:00에 **시작**하는 #3 운행 구간 수 (ERP 카드횟수·건수 대응)."""
    day_start, day_end = _day_bounds(business_date)
    if not day_start:
        return 0
    count = 0
    for interval in intervals or []:
        try:
            start = datetime.strptime(str(interval['start']).strip()[:16], _INTERVAL_FMT)
        except (ValueError, TypeError, KeyError):
            continue
        if day_start <= start < day_end:
            count += 1
    return count


def compute_daily_overnight_interval_end_count(intervals, business_date: str) -> int:
    """전날 시작해 당일 새벽에 끝나는 #3 구간 수 (당일 시작 구간이 없을 때 보조)."""
    day_start, _ = _day_bounds(business_date)
    if not day_start:
        return 0
    count = 0
    for interval in intervals or []:
        start_s = str(interval.get('start', '')).strip()[:10]
        end_s = str(interval.get('end', '')).strip()[:10]
        if end_s == business_date and start_s and start_s < business_date:
            count += 1
    return count


def compute_daily_segment_count(intervals, business_date: str) -> int:
    """장기 마감 일별 건수 — #3 구간 시작 수(ERP 카드횟수), 없으면 전날→당일 종료 1건."""
    starts = compute_daily_interval_start_count(intervals, business_date)
    if starts > 0:
        return starts
    return compute_daily_overnight_interval_end_count(intervals, business_date)


def compute_daily_duty_minutes(duty, business_date: str, closing_end: str = '') -> int:
    """당일 #4 on→off 영업시간(분). 마지막 on이 당일이면 closing_end까지 잘림."""
    day_start, day_end = _day_bounds(business_date)
    if not day_start:
        return 0

    clip_end = day_end
    if closing_end:
        try:
            end_dt = datetime.strptime(str(closing_end).strip()[:16], _INTERVAL_FMT)
            if day_start <= end_dt < day_end:
                clip_end = end_dt
        except ValueError:
            pass

    on_mins = 0
    current_on = None
    for event in duty or []:
        try:
            dt = datetime.strptime(str(event.get('datetime', '')).strip()[:16], _INTERVAL_FMT)
        except (ValueError, TypeError, KeyError):
            continue
        if event.get('status') == 'on':
            current_on = dt
        elif event.get('status') == 'off' and current_on is not None:
            seg_start = max(current_on, day_start)
            seg_end = min(dt, clip_end)
            if seg_end > seg_start:
                on_mins += int((seg_end - seg_start).total_seconds() // 60)
            current_on = None

    if current_on is not None:
        seg_start = max(current_on, day_start)
        seg_end = clip_end
        if seg_end > seg_start:
            on_mins += int((seg_end - seg_start).total_seconds() // 60)

    return on_mins


def is_handshake_closing_day(parsed: dict, business_date: str, all_parsed: list) -> bool:
    """이전 마감 종료일과 겹치는 시작일 — 이중 집계 방지용 스킵."""
    header = parsed.get('header') or {}
    start_day = str(header.get('start', '')).strip()[:10]
    if business_date != start_day:
        return False
    for other in all_parsed or []:
        if other is parsed:
            continue
        other_end = str((other.get('header') or {}).get('end', '')).strip()[:10]
        if other_end == business_date:
            return True
    return False


def compute_daily_sales_metrics(parsed: dict, business_date: str) -> dict[str, int | float]:
    """장기 마감 .dat에서 특정 달력일에 해당하는 실입금·건수·연료·거리·영업시간."""
    trips = parsed.get('trips') or []
    intervals = parsed.get('intervals') or []
    duty = parsed.get('duty') or []
    trip_stats = compute_daily_trip_stats(trips, business_date)
    segment_count = compute_daily_segment_count(intervals, business_date)
    fare_count = compute_daily_fare_trip_count(trips, business_date)

    if not any((
        trip_stats['income_won'],
        segment_count,
        fare_count,
        trip_stats['trip_count'],
    )):
        return {
            'income_won': 0,
            'fare_count': 0,
            'fuel_l': 0.0,
            'distance_km': 0.0,
            'work_minutes': 0,
            'total_minutes': 0,
            'empty_minutes': 0,
            'total_distance_km': 0.0,
            'empty_distance_km': 0.0,
        }

    header = parsed.get('header') or {}
    if is_prolonged_closing(parsed):
        interval_stats = compute_daily_interval_stats(intervals, business_date)
        work_minutes = compute_daily_duty_minutes(
            duty, business_date, closing_end=header.get('end', ''),
        )
        distance_km = interval_stats['distance_km']
        total_minutes = compute_daily_header_span_minutes(header, business_date)
        total_distance_km = _prorate_closing_total_distance_km(
            header, business_date, distance_km,
        )
        return {
            'income_won': int(trip_stats['income_won']),
            'fare_count': segment_count,
            'fuel_l': interval_stats['fuel_l'],
            'distance_km': distance_km,
            'work_minutes': work_minutes,
            **_build_operation_metrics(
                work_minutes, distance_km, total_minutes, total_distance_km,
            ),
        }

    interval_stats = compute_daily_interval_stats(intervals, business_date)
    work_minutes = compute_daily_interval_minutes(intervals, business_date)
    distance_km = interval_stats['distance_km']
    total_minutes = compute_daily_header_span_minutes(header, business_date)
    if total_minutes <= 0:
        total_minutes = _closing_header_span_minutes(
            header.get('start', ''),
            header.get('end', ''),
        )
    total_distance_km = _resolve_closing_total_distance_km(header, distance_km)
    return {
        'income_won': int(trip_stats['income_won']),
        'fare_count': fare_count,
        'fuel_l': interval_stats['fuel_l'],
        'distance_km': distance_km,
        'work_minutes': work_minutes,
        **_build_operation_metrics(
            work_minutes, distance_km, total_minutes, total_distance_km,
        ),
    }


def resolve_closing_business_date(parsed: dict) -> str | None:
    """마감 영업일 YYYY-MM-DD (#1 header.end 우선, 없으면 file_date)."""
    header = parsed.get('header') or {}
    end = header.get('end', '')
    if end and len(str(end)) >= 10:
        return str(end)[:10]
    return parsed.get('file_date')


def infer_shift_band_from_start(start_str: str) -> str:
    """#1 영업 시작 시각 → day|night (배차 주간/야간 매칭용).

    - day: 03:00~13:59 시작 (주간·일차·리스 등)
    - night: 그 외 (야간·교대 야간대 등)
    """
    try:
        hour = int(str(start_str or '').strip()[11:13])
    except (ValueError, IndexError):
        return ''
    if 3 <= hour <= 13:
        return 'day'
    return 'night'


def _interval_full_minutes(interval) -> int:
    """#3 interval 전체 구간 길이(분)."""
    try:
        start = datetime.strptime(str(interval['start']).strip()[:16], _INTERVAL_FMT)
        end = datetime.strptime(str(interval['end']).strip()[:16], _INTERVAL_FMT)
    except (ValueError, TypeError, KeyError):
        return 0
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


def parse_header_raw_tail(raw_tail: str) -> dict[str, float]:
    """#1 raw_tail에서 총거리·주행거리(미터기 누계) 추출.

    raw_tail 앞쪽에 ``총거리``·``주행거리``가 각각 ``NNN.NN`` 형태로 연속 기록됨.
    """
    distances: list[float] = []
    for match in re.finditer(r'(\d{1,3}\.\d{2})', str(raw_tail or '')):
        distances.append(float(match.group(1)))
        if len(distances) >= 2:
            break
    total_km = distances[0] if distances else 0.0
    running_km = distances[1] if len(distances) >= 2 else 0.0
    return {
        'total_distance_km': round(total_km, 2),
        'running_distance_km': round(running_km, 2),
    }


def _closing_header_span_minutes(closing_start: str = '', closing_end: str = '') -> int:
    """#1 마감 시작~종료 구간 길이(분). interval·duty가 없을 때 최후 폴백."""
    if not closing_start or not closing_end:
        return 0
    try:
        start = datetime.strptime(str(closing_start).strip()[:16], _INTERVAL_FMT)
        end = datetime.strptime(str(closing_end).strip()[:16], _INTERVAL_FMT)
    except ValueError:
        return 0
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


def compute_daily_header_span_minutes(header: dict, business_date: str) -> int:
    """장기 마감 일별 #1 출고~입고 구간이 해당 영업일에 걸친 분."""
    day_start, day_end = _day_bounds(business_date)
    if not day_start:
        return 0
    start_s = str((header or {}).get('start', '')).strip()
    end_s = str((header or {}).get('end', '')).strip()
    if not start_s or not end_s:
        return 0
    try:
        start = datetime.strptime(start_s[:16], _INTERVAL_FMT)
        end = datetime.strptime(end_s[:16], _INTERVAL_FMT)
    except ValueError:
        return 0
    clip_start = max(start, day_start)
    clip_end = min(end, day_end)
    if clip_end <= clip_start:
        return 0
    return int((clip_end - clip_start).total_seconds() // 60)


def _resolve_closing_total_distance_km(header: dict, running_distance_km: float) -> float:
    """raw_tail 총거리. 없으면 주행거리와 동일하게 본다."""
    tail_stats = parse_header_raw_tail(str((header or {}).get('raw_tail', '')))
    total_km = float(tail_stats.get('total_distance_km') or 0)
    if total_km <= 0:
        return round(float(running_distance_km or 0), 2)
    return round(total_km, 2)


def _prorate_closing_total_distance_km(
    header: dict,
    business_date: str,
    running_distance_km: float,
) -> float:
    """장기 마감 raw_tail 총거리를 당일 #1 구간 비율로 배분."""
    full_total = _resolve_closing_total_distance_km(header, running_distance_km)
    if full_total <= 0:
        return 0.0
    full_span = _closing_header_span_minutes(
        str(header.get('start', '')),
        str(header.get('end', '')),
    )
    daily_span = compute_daily_header_span_minutes(header, business_date)
    if full_span <= 0 or daily_span <= 0:
        return 0.0
    return round(full_total * daily_span / full_span, 2)


def _build_operation_metrics(
    work_minutes: int | float,
    distance_km: float,
    total_minutes: int,
    total_distance_km: float,
) -> dict[str, int | float]:
    work = max(0, int(work_minutes or 0))
    running = round(float(distance_km or 0), 2)
    total_dist = round(float(total_distance_km or 0), 2)
    if total_dist < running:
        total_dist = running
    total_time = max(0, int(total_minutes or 0))
    return {
        'total_minutes': total_time,
        'empty_minutes': max(0, total_time - work),
        'total_distance_km': total_dist,
        'empty_distance_km': max(0.0, round(total_dist - running, 2)),
    }


def compute_closing_duty_minutes(
    duty,
    closing_start: str = '',
    closing_end: str = '',
) -> int:
    """#4 duty on→off 전체 영업시간(분). #3 interval이 0일 때 폴백."""
    window_start = None
    window_end = None
    if closing_start:
        try:
            window_start = datetime.strptime(str(closing_start).strip()[:16], _INTERVAL_FMT)
        except ValueError:
            pass
    if closing_end:
        try:
            window_end = datetime.strptime(str(closing_end).strip()[:16], _INTERVAL_FMT)
        except ValueError:
            pass

    on_mins = 0
    current_on = None
    for event in duty or []:
        try:
            dt = datetime.strptime(str(event.get('datetime', '')).strip()[:16], _INTERVAL_FMT)
        except (ValueError, TypeError, KeyError):
            continue
        if event.get('status') == 'on':
            current_on = dt
        elif event.get('status') == 'off' and current_on is not None:
            seg_start = current_on
            seg_end = dt
            if window_start:
                seg_start = max(seg_start, window_start)
            if window_end:
                seg_end = min(seg_end, window_end)
            if seg_end > seg_start:
                on_mins += int((seg_end - seg_start).total_seconds() // 60)
            current_on = None

    if current_on is not None:
        seg_start = current_on
        if window_start:
            seg_start = max(seg_start, window_start)
        seg_end = window_end or current_on
        if seg_end > seg_start:
            on_mins += int((seg_end - seg_start).total_seconds() // 60)

    return on_mins


def compute_closing_fare_trip_count(trips) -> int:
    """파일 #2 전체 영업 건수 (날짜 필터 없음)."""
    trip_sum = 0.0
    count = 0
    for trip in trips or []:
        amount = float(trip.get('amount_won', 0) or 0)
        if amount == 0:
            if trip_sum > 0:
                count += 1
                trip_sum = 0
        else:
            trip_sum += amount
    if trip_sum > 0:
        count += 1
    return count


def compute_closing_sales_metrics(parsed: dict) -> dict[str, int | float]:
    """.dat 1파일 = 미터 마감 1영업일 수치 (ERP 마감 기준)."""
    intervals = parsed.get('intervals') or []
    income = int(parsed.get('total_income_won') or 0)
    if not income and parsed.get('trips'):
        income = round(sum(float(t.get('amount_won', 0) or 0) for t in parsed['trips']))
    fuel_l = round(float(parsed.get('total_fuel_l') or 0), 2)
    if not fuel_l and intervals:
        fuel_l = round(sum(float(i.get('fuel_l', 0) or 0) for i in intervals), 2)
    distance_km = round(float(parsed.get('total_distance_km') or 0), 2)
    if not distance_km and intervals:
        distance_km = round(sum(float(i.get('distance_km', 0) or 0) for i in intervals), 2)
    header = parsed.get('header') or {}
    work_minutes = sum(_interval_full_minutes(i) for i in intervals)
    if work_minutes <= 0:
        work_minutes = compute_closing_duty_minutes(
            parsed.get('duty') or [],
            closing_start=header.get('start', ''),
            closing_end=header.get('end', ''),
        )
    if work_minutes <= 0 and income > 0:
        work_minutes = _closing_header_span_minutes(
            header.get('start', ''),
            header.get('end', ''),
        )
    total_minutes = _closing_header_span_minutes(
        header.get('start', ''),
        header.get('end', ''),
    )
    total_distance_km = _resolve_closing_total_distance_km(header, distance_km)
    return {
        'income_won': income,
        'fare_count': compute_closing_fare_trip_count(parsed.get('trips') or []),
        'fuel_l': fuel_l,
        'distance_km': distance_km,
        'work_minutes': work_minutes,
        **_build_operation_metrics(
            work_minutes, distance_km, total_minutes, total_distance_km,
        ),
    }


def parse_dat_bytes(raw: bytes, filename: str = "") -> dict[str, Any]:
    content = read_dat_text(raw)
    parsed = parse_dat_content(content)
    file_date, file_car = parse_dat_filename(filename)
    parsed["file_date"] = file_date
    parsed["file_car_suffix"] = file_car
    parsed["source_file"] = filename
    parsed["closing_date"] = resolve_closing_business_date(parsed)
    header = parsed.get("header") or {}
    parsed["closing_start"] = header.get("start", "")
    parsed["closing_end"] = header.get("end", "")
    return parsed
