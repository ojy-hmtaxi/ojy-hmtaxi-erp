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


def parse_dat_bytes(raw: bytes, filename: str = "") -> dict[str, Any]:
    content = read_dat_text(raw)
    parsed = parse_dat_content(content)
    file_date, file_car = parse_dat_filename(filename)
    parsed["file_date"] = file_date
    parsed["file_car_suffix"] = file_car
    parsed["source_file"] = filename
    return parsed
