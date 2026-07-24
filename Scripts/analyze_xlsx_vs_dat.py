#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""주행내역관리 엑셀(건별) vs .dat(마감/일별) 상세 대조."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    allowed_dat_file,
    build_dat_upload_sales_rows,
    normalize_emp_id,
    parse_dat_bytes,
)

OUT = Path(__file__).with_name("xlsx_dat_analysis.txt")
XLSX = next(ROOT.joinpath("uploads").glob("*0401-0430*.xlsx"))
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
    return 0


def parse_dt(text):
    s = str(text or "").strip().replace(".", "-")
    return pd.to_datetime(s, errors="coerce")


def load_xlsx_trips() -> pd.DataFrame:
    raw = pd.read_excel(XLSX, sheet_name=0)
    c = list(raw.columns)
    # 실측 컬럼 (compare 실행 결과 기준)
    m = {
        "plate": c[1],
        "grade": c[2],
        "board_dt": c[3],
        "alight_dt": c[4],
        "drive_time": c[5],
        "pay_method": c[6],
        "pay_type": c[7],
        "fare": c[8],
        "call_fee": c[9],
        "toll": c[10],
        "other_fee": c[11],
        "income": c[12],
        "empty_km": c[13],   # 빈차거리
        "run_km": c[14],     # 영업거리
        "emp": c[15],
    }
    df = pd.DataFrame()
    df["plate"] = raw[m["plate"]].astype(str)
    df["car"] = df["plate"].map(car_suffix)
    df["emp"] = raw[m["emp"]].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    df["board_dt"] = raw[m["board_dt"]].map(parse_dt)
    df["alight_dt"] = raw[m["alight_dt"]].map(parse_dt)
    df["date"] = df["board_dt"].dt.strftime("%Y-%m-%d")
    df["drive_min"] = raw[m["drive_time"]].map(parse_hms_to_minutes)
    df["pay_method"] = raw[m["pay_method"]].astype(str)
    df["pay_type"] = raw[m["pay_type"]].astype(str)
    df["grade"] = raw[m["grade"]].astype(str)
    df["fare"] = pd.to_numeric(raw[m["fare"]], errors="coerce").fillna(0).astype(int)
    df["income"] = pd.to_numeric(raw[m["income"]], errors="coerce").fillna(0).astype(int)
    df["run_km"] = pd.to_numeric(raw[m["run_km"]], errors="coerce").fillna(0.0)
    df["empty_km"] = pd.to_numeric(raw[m["empty_km"]], errors="coerce").fillna(0.0)
    df.attrs["col_names"] = {k: str(v) for k, v in m.items()}
    df.attrs["raw_columns"] = [str(x) for x in c]
    return df


def aggregate_xlsx_daily(trips: pd.DataFrame) -> pd.DataFrame:
    g = trips.groupby(["date", "car", "emp"], dropna=False)
    agg = g.agg(
        trips=("income", "count"),
        income=("income", "sum"),
        fare=("fare", "sum"),
        run_km=("run_km", "sum"),
        empty_km=("empty_km", "sum"),
        drive_min=("drive_min", "sum"),
        board_first=("board_dt", "min"),
        alight_last=("alight_dt", "max"),
    ).reset_index()
    agg["total_km"] = agg["run_km"] + agg["empty_km"]
    return agg


def load_dat_daily() -> pd.DataFrame:
    parsed_list = []
    for path in sorted(DAT_DIR.glob("202604*.dat")):
        if not allowed_dat_file(path.name):
            continue
        parsed = parse_dat_bytes(path.read_bytes(), path.name)
        parsed["source_file"] = path.name
        parsed_list.append(parsed)
    rows = []
    for r in build_dat_upload_sales_rows(parsed_list, lookup_cache={}):
        rows.append(
            {
                "date": str(r.get("날짜", ""))[:10],
                "car": str(r.get("차번", "")).strip(),
                "emp": normalize_emp_id(r.get("사번", "")),
                "name": str(r.get("이름", "")).strip(),
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
                "out_dt": str(r.get("출고일시") or r.get("영업시작") or ""),
                "in_dt": str(r.get("입고일시") or r.get("영업종료") or ""),
                "source": str(r.get("원본파일") or ""),
            }
        )
    return pd.DataFrame(rows)


def match_stats(merged: pd.DataFrame, col_x: str, col_d: str, tol: float, label: str) -> str:
    if merged.empty:
        return f"  {label}: 비교 불가"
    diff = (merged[col_x] - merged[col_d]).abs()
    ok = int((diff <= tol).sum())
    n = len(merged)
    med = float((merged[col_x] - merged[col_d]).median())
    p90 = float(diff.quantile(0.9))
    return f"  {label}: 일치 {ok}/{n} (±{tol}), 중앙 diff={med:.2f}, p90 diff={p90:.2f}"


def main():
    lines: list[str] = []
    w = lines.append

    w("=" * 70)
    w("주행내역관리 엑셀 vs .dat 비교 분석 (2026년 4월)")
    w("=" * 70)
    w(f"엑셀: {XLSX.name}")
    w(f"dat: {DAT_DIR} (202604*.dat)")
    w("")

    trips = load_xlsx_trips()
    w("[1] 엑셀 구조")
    w(f"  원본 행 수: {len(trips):,} (건별 승하차 기록)")
    w(f"  컬럼({len(trips.attrs['raw_columns'])}): {trips.attrs['raw_columns']}")
    w("  컬럼 매핑:")
    for k, v in trips.attrs["col_names"].items():
        w(f"    {k}: {v}")
    w(f"  결제수단 분포: {trips['pay_method'].value_counts(dropna=False).head(8).to_dict()}")
    w(f"  차량등급 분포: {trips['grade'].value_counts().head(5).to_dict() if 'grade' in trips else 'N/A'}")
    w("")

    x_daily = aggregate_xlsx_daily(trips)
    dat = load_dat_daily()
    w("[2] 집계 단위")
    w(f"  엑셀 일별(날짜|차번|사번) 집계: {len(x_daily):,}행")
    w(f"  dat → ERP 일별 행: {len(dat):,}행")
    w(f"  엑셀 날짜 범위: {x_daily['date'].min()} ~ {x_daily['date'].max()}")
    w(f"  dat 날짜 범위: {dat['date'].min()} ~ {dat['date'].max()}")
    w("")

    x_daily["key"] = x_daily["date"] + "|" + x_daily["car"] + "|" + x_daily["emp"]
    dat["key"] = dat["date"] + "|" + dat["car"] + "|" + dat["emp"]
    merged = x_daily.merge(dat, on="key", how="outer", suffixes=("_x", "_d"), indicator=True)
    counts = merged["_merge"].value_counts().to_dict()
    w("[3] 키 매칭 (날짜|차번|사번)")
    w(f"  공통: {counts.get('both', 0)}")
    w(f"  엑셀에만: {counts.get('left_only', 0)}")
    w(f"  dat에만: {counts.get('right_only', 0)}")
    w("")

    both = merged[merged["_merge"] == "both"].copy()
    w(f"[4] 공통 {len(both)}건 필드 비교 (엑셀 일집계 vs dat ERP행)")
    w(match_stats(both, "income_x", "income_d", 500, "최종요금합 vs 실입금"))
    w(match_stats(both, "trips_x", "trips_d", 2, "건수"))
    w(match_stats(both, "run_km_x", "run_km_d", 3.0, "영업거리합 vs 운행거리"))
    w(match_stats(both, "empty_km_x", "empty_km_d", 3.0, "빈차거리합"))
    w(match_stats(both, "total_km_x", "total_km_d", 5.0, "총거리(합산)"))
    w(match_stats(both, "drive_min", "work_min", 60, "주행시간합 vs 영업시간"))
    w("  연료비/충전량: 엑셀에 해당 컬럼 없음 → dat 전용")
    w("  총시간/출고·입고: 엑셀에 마감 구간 없음 → dat #1 header/duty 기반")
    w("")

    # card-only income compare
    card = trips[trips["pay_method"].str.contains("카드", na=False)]
    card_daily = aggregate_xlsx_daily(card)
    card_daily["key"] = card_daily["date"] + "|" + card_daily["car"] + "|" + card_daily["emp"]
    m_card = card_daily.merge(dat, on="key", how="inner", suffixes=("_x", "_d"))
    w(f"[5] 카드 결제만 집계 vs dat 실입금 (공통 {len(m_card)}건)")
    w(match_stats(m_card, "income_x", "income_d", 500, "카드 최종요금 vs 실입금"))
    w("")

    # date|car only
    x2 = x_daily.groupby(["date", "car"]).agg(
        income=("income", "sum"), trips=("trips", "sum"), run_km=("run_km", "sum")
    ).reset_index()
    d2 = dat.groupby(["date", "car"]).agg(
        income=("income", "sum"), trips=("trips", "sum"), run_km=("run_km", "sum")
    ).reset_index()
    m2 = x2.merge(d2, on=["date", "car"], suffixes=("_x", "_d"))
    w(f"[6] 날짜|차번 단위 (사번 무시, {len(m2)}건)")
    w(match_stats(m2, "income_x", "income_d", 1000, "수입"))
    w(match_stats(m2, "trips_x", "trips_d", 3, "건수"))
    w("")

    # sample mismatch
    if len(both):
        both["income_diff"] = both["income_x"] - both["income_d"]
        worst = both.reindex(both["income_diff"].abs().sort_values(ascending=False).index).head(5)
        w("[7] 수입 차이 큰 샘플 5건")
        for _, r in worst.iterrows():
            w(
                f"  {r['key']}: 엑셀={int(r['income_x']):,} dat={int(r['income_d']):,} "
                f"diff={int(r['income_diff']):,} | 건수 {int(r['trips_x'])}/{int(r['trips_d'])}"
            )
        w("")

    w("[8] 엑셀에 없고 dat/ERP에만 있는 필드")
    missing = [
        "연료비", "충전량", "총시간", "빈차시간(마감기준)",
        "출고일시/입고일시(마감)", "차종", "근무유형", "매칭(T머니/배차)",
    ]
    for item in missing:
        w(f"  - {item}")
    w("")

    w("[9] 엑셀에만 있는 정보")
    extra = [
        "건별 승차/하차 일시", "건별 주행시간", "결제수단(카드/현금/앱 등)",
        "할증여부", "운임/호출료/통행료/기타요금 분리", "차량등급",
    ]
    for item in extra:
        w(f"  + {item}")
    w("")

    w("[10] dat 대체 업로드 가능성 검토")
    w("  결론 요약:")
    w("  - 엑셀은 '건별 주행내역', dat는 '미터 마감 파일'로 집계 단위·산출 로직이 다름.")
    w("  - 연료비·충전량·총시간·빈차시간·출고/입고는 엑셀만으로 동일 테이블 재현 불가.")
    w("  - 수입·건수·거리는 일별 집계 후 근사 가능하나, dat 파서의 건수/영업시간 산식과 차이 존재.")
    w("  - 현재 업로더(dat input)에 xlsx를 넣으면 파싱 실패 — 별도 xlsx 파서·집계·T머니 CSV 병행 개발 필요.")
    w("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT}")
    print("\n".join(lines[:40]))
    print("...")
    print("\n".join(lines[-15:]))


if __name__ == "__main__":
    main()
