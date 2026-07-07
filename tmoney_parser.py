"""T머니 / 운전자별 수입금 상세 CSV 파서."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

# cp949 고정 문자열 — 소스 인코딩 이슈 회피
TRIP_ALIGHT_ITEM = bytes.fromhex('c8c4bad228bdc2c0ce29').decode('cp949')  # 하차(승인)
TRIP_BOARD_ITEM = bytes.fromhex('bcb1bad228bdc2c0ce29').decode('cp949')  # 승차(승인)
TRIP_COUNT_ITEMS = frozenset({TRIP_ALIGHT_ITEM, TRIP_BOARD_ITEM})

# 구형 T머니 일자별상세거래내역 CSV 컬럼 수
LEGACY_DETAIL_COLUMN_COUNT = 15
# 신형 운전자별수입금상세내역 CSV 컬럼 수
DRIVER_INCOME_COLUMN_COUNT = 16


def _clean_bracketed(value) -> str:
    text = str(value or '').strip()
    if text.startswith('[') and text.endswith(']'):
        return text[1:-1].strip()
    return text


def _car_suffix(plate: str) -> str:
    match = re.search(r'(\d{4})', str(plate or ''))
    return match.group(1) if match else ''


def find_tmoney_csv_file(uploads_dir: str | Path = 'uploads') -> Path | None:
    """uploads 폴더에서 수입금 상세 CSV 탐색."""
    uploads = Path(uploads_dir)
    candidates: list[Path] = []
    for path in uploads.iterdir():
        if path.suffix.lower() != '.csv':
            continue
        name = path.name.lower()
        if any(token in name for token in (
            '운전자별', '수입금', 'tmoney', 't머니', '스마트카드', '거래내역',
        )):
            candidates.append(path)
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def find_tmoney_upload_files(uploads_dir: str | Path = 'uploads') -> tuple[Path | None, Path | None]:
    """레거시 호환 — (일별 Excel, 상세 CSV). Excel 없으면 CSV만 반환."""
    uploads = Path(uploads_dir)
    daily_xlsx = None
    for path in uploads.iterdir():
        name = path.name
        if path.suffix.lower() == '.xlsx' and 'Excel' in name and 't' in name.lower():
            daily_xlsx = path
            break
    detail_csv = find_tmoney_csv_file(uploads)
    return daily_xlsx, detail_csv


def detect_csv_format(columns: list) -> str:
    """CSV 컬럼 구성으로 형식 판별."""
    if len(columns) >= DRIVER_INCOME_COLUMN_COUNT:
        return 'driver_income'
    if len(columns) >= LEGACY_DETAIL_COLUMN_COUNT:
        return 'legacy_detail'
    raise ValueError(f'지원하지 않는 CSV 컬럼 수: {len(columns)}')


def load_tmoney_daily(path: str | Path) -> pd.DataFrame:
    """일별 Excel → (date, car) 단위 승차건수·정산금액."""
    raw = pd.read_excel(path, sheet_name=0, header=None)
    df = raw.iloc[2:].copy()
    df.columns = [
        'date_raw', 'plate', 'trip_count', 'cancel_count',
        'fare_total', 'fee', 'discount', 'platform_fee', 'settle_amount',
    ]
    df['date'] = pd.to_datetime(df['date_raw'], errors='coerce').dt.strftime('%Y-%m-%d')
    df['car'] = df['plate'].astype(str).map(_car_suffix)
    for col in ('trip_count', 'settle_amount'):
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
    return df[df['date'].notna() & df['car'].ne('')].reset_index(drop=True)


def _normalize_detail_frame(df: pd.DataFrame, fmt: str) -> pd.DataFrame:
    if fmt == 'driver_income':
        df.columns = [
            'tx_date', 'driver_id', 'driver_name', 'plate', 'car_type', 'item',
            'card_type', 'card_no', 'used_at', 'amount', 'fee', 'adj_amount',
            'op_fee', 'platform_fee', 'settle_amount', 'corp_settle_date',
        ]
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].map(_clean_bracketed)
        df = df[df['tx_date'].str.match(r'^\d{4}-\d{2}-\d{2}$', na=False)].copy()
        df['date'] = df['tx_date']
    else:
        df.columns = [
            'plate', 'driver_id', 'driver_name', 'tx_time', 'card_type', 'card_no',
            'approval_no', 'item', 'fare', 'fee1', 'fee2', 'discount',
            'platform_fee', 'settle_amount', 'settle_date',
        ]
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].map(_clean_bracketed)
        df = df[df['plate'].str.contains(r'\d{4}', na=False)].copy()
        df['date'] = pd.to_datetime(df['tx_time'].str[:19], errors='coerce').dt.strftime('%Y-%m-%d')
        df['car_type'] = ''
        df = df[df['date'].notna()].copy()

    df['car'] = df['plate'].map(_car_suffix)
    df['emp'] = df['driver_id'].astype(str).str.replace(r'\.0$', '', regex=True)
    df['driver_name'] = df['driver_name'].astype(str)
    df['is_trip'] = df['item'].isin(TRIP_COUNT_ITEMS)
    df['settle_amount'] = pd.to_numeric(df['settle_amount'], errors='coerce').fillna(0).astype(int)
    return df[df['car'].ne('')].reset_index(drop=True)


def load_tmoney_detail(path: str | Path) -> pd.DataFrame:
    """상세 CSV → 정규화된 거래 프레임."""
    df = pd.read_csv(path, encoding='cp949')
    fmt = detect_csv_format(list(df.columns))
    return _normalize_detail_frame(df, fmt)


def build_tmoney_lookups(
    daily_path: str | Path | None = None,
    detail_path: str | Path | None = None,
    uploads_dir: str | Path = 'uploads',
) -> dict:
    """일별·운전자별 집계 룩업 생성. 일별 Excel 없으면 CSV에서 차량 합계 산출."""
    if detail_path is None:
        _, detail_path = find_tmoney_upload_files(uploads_dir)
    if not detail_path:
        raise FileNotFoundError('수입금 상세 CSV를 uploads에서 찾을 수 없습니다.')

    detail = load_tmoney_detail(detail_path)
    csv_format = detect_csv_format(
        list(pd.read_csv(detail_path, encoding='cp949', nrows=0).columns),
    )

    by_driver = {}
    grouped = detail.groupby(['date', 'car', 'emp'], dropna=False)
    for (date, car, emp), grp in grouped:
        emp_str = str(emp)
        if emp_str in ('', 'nan'):
            continue
        by_driver[(date, car, emp_str)] = {
            'trip_count': int(grp['is_trip'].sum()),
            'income': int(grp['settle_amount'].sum()),
            'driver_name': str(grp['driver_name'].iloc[0] or ''),
            'car_type': str(grp['car_type'].iloc[0] or '') if 'car_type' in grp else '',
        }

    by_car = {}
    if daily_path and Path(daily_path).is_file():
        daily = load_tmoney_daily(daily_path)
        for _, row in daily.iterrows():
            by_car[(row['date'], row['car'])] = {
                'trip_count': int(row['trip_count']),
                'income': int(row['settle_amount']),
            }
        daily_total_income = int(daily['settle_amount'].sum())
        daily_total_trips = int(daily['trip_count'].sum())
    else:
        car_grouped = detail.groupby(['date', 'car'], dropna=False)
        for (date, car), grp in car_grouped:
            by_car[(date, car)] = {
                'trip_count': int(grp['is_trip'].sum()),
                'income': int(grp['settle_amount'].sum()),
            }
        daily_total_income = int(detail['settle_amount'].sum())
        daily_total_trips = int(detail['is_trip'].sum())

    return {
        'daily_path': str(daily_path) if daily_path else '',
        'detail_path': str(detail_path),
        'csv_format': csv_format,
        'by_car': by_car,
        'by_driver': by_driver,
        'daily_total_income': daily_total_income,
        'daily_total_trips': daily_total_trips,
        'month_keys': sorted({
            f"{date[5:7]}월"
            for date, _car in by_car
            if date and len(date) >= 7
        }),
    }


def build_tmoney_lookups_from_bytes(raw: bytes, filename: str = '') -> dict:
    """업로드된 CSV 바이트에서 룩업 생성."""
    import tempfile
    suffix = Path(filename or 'tmoney.csv').suffix or '.csv'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        return build_tmoney_lookups(detail_path=tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
