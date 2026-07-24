#!/usr/bin/env python
"""주행내역관리 엑셀 vs .dat 대조 분석."""
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
    build_dat_upload_sales_rows,
    build_vehicle_lookup,
    match_vehicle_record,
    normalize_emp_id,
    parse_dat_bytes,
    sales_dispatch_month_key,
)
from dat_parser import compute_closing_sales_metrics, resolve_closing_business_date  # noqa: E402

XLSX_GLOB = "*0401-0430*.xlsx"
DAT_DIR = ROOT / "uploads" / "dat"


def car_suffix(plate: str) -> str:
    m = re.search(r"(\d{4})", str(plate or ""))
    return m.group(1) if m else ""


def parse_hms_to_minutes(text) -> int:
    s = str(text or "").strip()
    if not s or s.lower() == "nan":
        return 0
    parts = s.split(":")
    if len(parts) == 3:
        h, m, sec = (int(float(p)) for p in parts)
        return h * 60 + m + (1 if sec >= 30 else 0)
    if len(parts) == 2:
        h, m = (int(float(p)) for p in parts)
        return h * 60 + m
    return int(float(s)) if s.replace(".", "", 1).isdigit() else 0


def parse_num(text) -> float:
    return float(str(text or "0").replace(",", "").replace("km", "").strip() or 0)


def find_xlsx() -> Path:
    files = sorted(ROOT.joinpath("uploads").glob(XLSX_GLOB))
    if not files:
        raise FileNotFoundError(f"엑셀 파일 없음: uploads/{XLSX_GLOB}")
    return files[0]


def load_xlsx(path: Path) -> pd.DataFrame:
    xl = pd.ExcelFile(path)
    sheet = xl.sheet_names[0]
    df = pd.read_excel(path, sheet_name=sheet)
    print(f"[엑셀] 파일: {path.name}, 시트: {sheet}, 행: {len(df)}, 열: {len(df.columns)}")
    print("[엑셀] 컬럼:", list(df.columns))
    return df


def normalize_xlsx(df: pd.DataFrame) -> pd.DataFrame:
    cols = {str(c).strip(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    date_col = pick("날짜", "영업일", "일자")
    car_col = pick("차번", "담당차량", "차량번호", "차량")
    emp_col = pick("사번", "기사사번")
    out_col = pick("출고일시", "출고", "영업시작")
    in_col = pick("입고일시", "입고", "영업종료")
    income_col = pick("실입금", "수입", "수입금", "운임")
    trips_col = pick("건수", "운임건수", "승차건수")
    work_col = pick("영업시간", "영업시간(분)")
    empty_time_col = pick("빈차시간", "공차시간")
    total_time_col = pick("총시간", "근무시간")
    fuel_col = pick("연료비", "주유금액")
    fuel_l_col = pick("충전량", "연료량", "주유량", "연료(L)")
    run_col = pick("운행거리", "주행거리", "영업거리")
    empty_dist_col = pick("빈차거리", "공차거리")
    total_dist_col = pick("총거리", "누적거리")

    out = pd.DataFrame()
    out["date"] = df[date_col].astype(str).str.strip() if date_col else ""
    if date_col and out["date"].str.match(r"^\d{8}$").any():
        out["date"] = pd.to_datetime(out["date"], format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    elif date_col:
        out["date"] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")

    plate_raw = df[car_col].astype(str) if car_col else ""
    out["car"] = plate_raw.map(car_suffix) if car_col else ""
    out["plate"] = plate_raw if car_col else ""
    out["emp"] = (
        df[emp_col].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
        if emp_col
        else ""
    )
    out["out_dt"] = df[out_col] if out_col else pd.NaT
    out["in_dt"] = df[in_col] if in_col else pd.NaT
    out["income"] = df[income_col].map(parse_num).astype(int) if income_col else 0
    out["trips"] = pd.to_numeric(df[trips_col], errors="coerce").fillna(0).astype(int) if trips_col else 0
    out["work_min"] = df[work_col].map(parse_hms_to_minutes) if work_col else 0
    out["empty_min"] = df[empty_time_col].map(parse_hms_to_minutes) if empty_time_col else 0
    out["total_min"] = df[total_time_col].map(parse_hms_to_minutes) if total_time_col else 0
    out["fuel_cost"] = df[fuel_col].map(parse_num).astype(int) if fuel_col else 0
    out["fuel_l"] = df[fuel_l_col].map(parse_num) if fuel_l_col else 0.0
    out["run_km"] = df[run_col].map(parse_num) if run_col else 0.0
    out["empty_km"] = df[empty_dist_col].map(parse_num) if empty_dist_col else 0.0
    out["total_km"] = df[total_dist_col].map(parse_num) if total_dist_col else 0.0

    if total_time_col is None and work_col and empty_time_col:
        out["total_min"] = out["work_min"] + out["empty_min"]
    if total_dist_col is None and run_col and empty_dist_col:
        out["total_km"] = out["run_km"] + out["empty_km"]

    out = out[out["date"].str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)].copy()
    return out


def load_dat_rows() -> pd.DataFrame:
    lookup_cache = {}
    rows = []
    files = sorted(DAT_DIR.glob("202604*.dat"))
    parsed_list = []
    for path in files:
        if not allowed_dat_file(path.name):
            continue
        parsed = parse_dat_bytes(path.read_bytes(), path.name)
        parsed["source_file"] = path.name
        parsed_list.append(parsed)

    built = build_dat_upload_sales_rows(parsed_list, lookup_cache=lookup_cache)
    for r in built:
        rows.append(
            {
                "date": str(r.get("날짜", "")).strip(),
                "car": str(r.get("차번", "")).strip(),
                "emp": normalize_emp_id(r.get("사번", "")),
                "name": str(r.get("이름", "")).strip(),
                "out_dt": r.get("출고일시", r.get("영업시작", "")),
                "in_dt": r.get("입고일시", r.get("영업종료", "")),
                "income": int(float(r.get("실입금") or 0)),
                "trips": int(float(r.get("건수") or 0)),
                "work_min": int(float(r.get("영업시간") or 0)),
                "empty_min": int(float(r.get("빈차시간") or 0)),
                "total_min": int(float(r.get("총시간") or 0)),
                "fuel_cost": int(float(r.get("연료비") or 0)),
                "fuel_l": float(r.get("충전량") or 0),
                "run_km": float(r.get("운행거리") or 0),
                "empty_km": float(r.get("빈차거리") or 0),
                "total_km": float(r.get("총거리") or 0),
                "source": str(r.get("원본파일", "")),
            }
        )
    df = pd.DataFrame(rows)
    print(f"[dat] 파싱 파일: {len(parsed_list)}, ERP 행: {len(df)}")
    return df


def compare(xlsx: pd.DataFrame, dat: pd.DataFrame):
    x_key = xlsx.assign(key=lambda d: d["date"] + "|" + d["car"] + "|" + d["emp"])
    d_key = dat.assign(key=lambda d: d["date"] + "|" + d["car"] + "|" + d["emp"])

    x_keys = set(x_key["key"])
    d_keys = set(d_key["key"])
    only_x = x_keys - d_keys
    only_d = d_keys - x_keys
    both = x_keys & d_keys

    print("\n=== 키 매칭 (날짜|차번|사번) ===")
    print(f"엑셀 고유: {len(x_keys)}, dat ERP행: {len(d_keys)}, 공통: {len(both)}")
    print(f"엑셀에만: {len(only_x)}, dat에만: {len(only_d)}")

    # 차번|날짜만 매칭 (사번 불일치 가능)
    x2 = xlsx.assign(key2=lambda d: d["date"] + "|" + d["car"])
    d2 = dat.assign(key2=lambda d: d["date"] + "|" + d["car"])
    only_x2 = set(x2["key2"]) - set(d2["key2"])
    only_d2 = set(d2["key2"]) - set(x2["key2"])
    print(f"\n[차번|날짜] 엑셀에만: {len(only_x2)}, dat에만: {len(only_d2)}")

    metrics = [
        ("income", "실입금", 0),
        ("trips", "건수", 0),
        ("work_min", "영업시간(분)", 5),
        ("empty_min", "빈차시간(분)", 5),
        ("total_min", "총시간(분)", 5),
        ("fuel_cost", "연료비", 100),
        ("fuel_l", "충전량(L)", 0.5),
        ("run_km", "운행거리(km)", 1.0),
        ("empty_km", "빈차거리(km)", 1.0),
        ("total_km", "총거리(km)", 1.0),
    ]

    merged = x_key.merge(d_key, on="key", suffixes=("_x", "_d"))
    print(f"\n=== 공통 {len(merged)}건 필드 비교 ===")
    for col, label, tol in metrics:
        dx = merged[f"{col}_x"] - merged[f"{col}_d"]
        if col in ("fuel_l", "run_km", "empty_km", "total_km"):
            mismatch = (dx.abs() > tol).sum()
        else:
            mismatch = (dx.abs() > tol).sum()
        exact = len(merged) - mismatch
        print(f"  {label}: 일치(±{tol}) {exact}/{len(merged)}, 불일치 {mismatch}")
        if mismatch and mismatch <= 10:
            bad = merged[dx.abs() > tol][["key", f"{col}_x", f"{col}_d"]].head(5)
            print(bad.to_string(index=False))

    # 엑셀 컬럼 존재 여부
    print("\n=== 엑셀 필드 커버리지 (ERP 테이블 대비) ===")
    erp_fields = {
        "date": "날짜",
        "car": "차번",
        "emp": "사번",
        "out_dt": "출고일시",
        "in_dt": "입고일시",
        "income": "실입금",
        "trips": "건수",
        "work_min": "영업시간",
        "empty_min": "빈차시간",
        "total_min": "총시간",
        "fuel_cost": "연료비",
        "fuel_l": "충전량",
        "run_km": "운행거리",
        "empty_km": "빈차거리",
        "total_km": "총거리",
    }
    raw_df = load_xlsx(find_xlsx())
    cols = {str(c).strip() for c in raw_df.columns}
    mapping_notes = {
        "날짜": ["날짜", "영업일", "일자"],
        "차번": ["차번", "담당차량", "차량번호"],
        "사번": ["사번", "기사사번"],
        "출고일시": ["출고일시", "출고", "영업시작"],
        "입고일시": ["입고일시", "입고", "영업종료"],
        "실입금": ["실입금", "수입", "수입금", "운임"],
        "건수": ["건수", "운임건수", "승차건수"],
        "영업시간": ["영업시간"],
        "빈차시간": ["빈차시간", "공차시간"],
        "총시간": ["총시간", "근무시간"],
        "연료비": ["연료비", "주유금액"],
        "충전량": ["충전량", "연료량", "주유량"],
        "운행거리": ["운행거리", "주행거리", "영업거리"],
        "빈차거리": ["빈차거리", "공차거리"],
        "총거리": ["총거리", "누적거리"],
    }
    for erp, candidates in mapping_notes.items():
        found = [c for c in candidates if c in cols]
        status = found[0] if found else "없음"
        print(f"  {erp}: {status}")

    # sample rows
    print("\n=== 엑셀 샘플 3행 ===")
    print(xlsx.head(3).to_string())
    print("\n=== dat ERP 샘플 3행 ===")
    print(dat.head(3).to_string())


def main():
    xlsx_path = find_xlsx()
    raw = load_xlsx(xlsx_path)
    x = normalize_xlsx(raw)
    d = load_dat_rows()
    compare(x, d)


if __name__ == "__main__":
    main()
