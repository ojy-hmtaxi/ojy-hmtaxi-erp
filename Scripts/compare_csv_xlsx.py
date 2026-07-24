#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T머니 CSV vs 주행내역관리 엑셀 비교."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tmoney_parser import (  # noqa: E402
    TRIP_ALIGHT_ITEM,
    TRIP_BOARD_ITEM,
    build_tmoney_lookups,
    load_tmoney_detail,
)

OUT = Path(__file__).with_name("csv_xlsx_analysis.txt")
UPLOADS = ROOT / "uploads"


def find_csv() -> Path:
    for p in UPLOADS.glob("*.csv"):
        if "4" in p.name or "수입" in p.name or "운전" in p.name:
            return p
    files = list(UPLOADS.glob("*.csv"))
    if not files:
        raise FileNotFoundError("CSV 없음")
    return files[0]


def find_xlsx() -> Path:
    files = sorted(UPLOADS.glob("*0401-0430*.xlsx"))
    if not files:
        raise FileNotFoundError("xlsx 없음")
    return files[0]


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


def load_xlsx_trips(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name=0)
    c = list(raw.columns)
    df = pd.DataFrame()
    df["plate"] = raw[c[1]].astype(str)
    df["car"] = df["plate"].map(car_suffix)
    df["emp"] = raw[c[15]].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    df["board_dt"] = pd.to_datetime(
        raw[c[3]].astype(str).str.replace(".", "-"), errors="coerce"
    )
    df["alight_dt"] = pd.to_datetime(
        raw[c[4]].astype(str).str.replace(".", "-"), errors="coerce"
    )
    df["date"] = df["board_dt"].dt.strftime("%Y-%m-%d")
    df["pay_method"] = raw[c[6]].astype(str)
    df["income"] = pd.to_numeric(raw[c[12]], errors="coerce").fillna(0).astype(int)
    df["fare"] = pd.to_numeric(raw[c[8]], errors="coerce").fillna(0).astype(int)
    df["drive_min"] = raw[c[5]].map(parse_hms_to_minutes)
    df["run_km"] = pd.to_numeric(raw[c[14]], errors="coerce").fillna(0.0)
    df["empty_km"] = pd.to_numeric(raw[c[13]], errors="coerce").fillna(0.0)
    df.attrs["columns"] = [str(x) for x in c]
    return df[df["date"].notna()].copy()


def aggregate_xlsx(daily: pd.DataFrame) -> pd.DataFrame:
    all_df = daily.groupby(["date", "car", "emp"], dropna=False).agg(
        trips=("income", "count"),
        income=("income", "sum"),
        fare=("fare", "sum"),
        drive_min=("drive_min", "sum"),
        run_km=("run_km", "sum"),
        empty_km=("empty_km", "sum"),
    ).reset_index()
    card = daily[daily["pay_method"].str.contains("카드", na=False)]
    card_df = card.groupby(["date", "car", "emp"], dropna=False).agg(
        card_trips=("income", "count"),
        card_income=("income", "sum"),
    ).reset_index()
    return all_df.merge(card_df, on=["date", "car", "emp"], how="left").fillna(0)


def match_line(merged, col_x, col_d, tol, label) -> str:
    if merged.empty:
        return f"  {label}: 비교 불가"
    diff = (merged[col_x] - merged[col_d]).abs()
    ok = int((diff <= tol).sum())
    n = len(merged)
    med = float((merged[col_x] - merged[col_d]).median())
    return f"  {label}: 일치 {ok}/{n} (±{tol}), 중앙 diff={med:.1f}"


def main():
    csv_path = find_csv()
    xlsx_path = find_xlsx()
    lines: list[str] = []
    w = lines.append

    w("=" * 72)
    w("T머니 CSV vs 주행내역관리 엑셀 비교 (2026년 4월)")
    w("=" * 72)
    w(f"CSV: {csv_path.name}")
    w(f"엑셀: {xlsx_path.name}")
    w("")

    tmoney = build_tmoney_lookups(detail_path=csv_path)
    detail = load_tmoney_detail(csv_path)
    trips = load_xlsx_trips(xlsx_path)
    x_agg = aggregate_xlsx(trips)

    w("[1] 파일 개요")
    w(f"  CSV 형식: {tmoney['csv_format']}")
    w(f"  CSV 원본 거래 행: {len(detail):,}")
    w(f"  CSV 승·하차(건수) 행: {int(detail['is_trip'].sum()):,}")
    w(f"  CSV 정산금액 합: {int(detail['settle_amount'].sum()):,}원")
    w(f"  CSV (날짜|차번|사번) 집계: {len(tmoney['by_driver']):,}건")
    w(f"  엑셀 원본 승하차 행: {len(trips):,}")
    w(f"  엑셀 최종요금 합: {int(trips['income'].sum()):,}원")
    w(f"  엑셀 (날짜|차번|사번) 집계: {len(x_agg):,}건")
    w("")

    w("[2] CSV 컬럼·집계 기준 (ERP 실입금·건수 소스)")
    w(f"  컬럼: {list(pd.read_csv(csv_path, encoding='cp949', nrows=0).columns)}")
    w(f"  건수: item ∈ {{승차(승인), 하차(승인)}} → is_trip=True 행 수")
    w(f"  실입금: settle_amount(정산금액) 일별·기사별 합")
    w(f"  범위: **카드(T머니) 정산 거래만** — 현금·앱 미포함")
    w("")

    w("[3] 엑셀 컬럼·집계 기준")
    w(f"  컬럼: {trips.attrs['columns']}")
    w(f"  건수: 승하차 1행 = 1건")
    w(f"  실입금: 최종요금 합 (현금·앱·카드 포함)")
    w(f"  결제수단: {trips['pay_method'].value_counts(dropna=False).head(6).to_dict()}")
    w("")

    # build csv daily driver frame
    csv_rows = []
    for (date, car, emp), v in tmoney["by_driver"].items():
        csv_rows.append({
            "date": date, "car": car, "emp": str(emp),
            "csv_trips": v["trip_count"], "csv_income": v["income"],
            "csv_name": v.get("driver_name", ""),
        })
    csv_df = pd.DataFrame(csv_rows)
    csv_df["key"] = csv_df["date"] + "|" + csv_df["car"] + "|" + csv_df["emp"]
    x_agg["key"] = x_agg["date"] + "|" + x_agg["car"] + "|" + x_agg["emp"]

    merged = x_agg.merge(csv_df, on="key", how="outer", suffixes=("_x", "_c"), indicator=True)
    counts = merged["_merge"].value_counts().to_dict()
    w("[4] 키 매칭 (날짜|차번|사번)")
    w(f"  공통: {counts.get('both', 0)}")
    w(f"  엑셀에만: {counts.get('left_only', 0)}")
    w(f"  CSV에만: {counts.get('right_only', 0)}")
    w("")

    both = merged[merged["_merge"] == "both"].copy()
    w(f"[5] 공통 {len(both)}건 — 전체 최종요금 vs CSV 정산")
    w(match_line(both, "income", "csv_income", 500, "최종요금(전체) vs 정산금액"))
    w(match_line(both, "trips", "csv_trips", 2, "건수(전체) vs CSV 건수"))
    w("")

    w(f"[6] 공통 {len(both)}건 — 엑셀 **카드만** vs CSV")
    both["card_income"] = both["card_income"].fillna(0).astype(int)
    both["card_trips"] = both["card_trips"].fillna(0).astype(int)
    w(match_line(both, "card_income", "csv_income", 500, "카드 최종요금 vs CSV 정산"))
    w(match_line(both, "card_trips", "csv_trips", 2, "카드 건수 vs CSV 건수"))
    w("")

    # item breakdown in csv
    w("[7] CSV item(거래유형) 분포")
    item_counts = detail["item"].value_counts().head(12)
    for item, cnt in item_counts.items():
        w(f"  {item}: {cnt:,}")
    w("")

    # coverage comparison
    w("[8] ERP 표 컬럼별 — 어느 파일이 적합한가")
    erp = [
        ("실입금(카드 정산)", "CSV", "정산금액 = T머니 실제 입금 기준"),
        ("건수(카드 승·하차)", "CSV", "승차/하차(승인) 이벤트"),
        ("실입금(현금·앱 포함)", "엑셀", "최종요금, 단 CSV와 카드 구간 불일치 존재"),
        ("건수(전체 승하차)", "엑셀", "1행=1건, 현금·앱 포함"),
        ("영업시간", "없음", "엑셀=건별 주행시간 합, CSV=미제공"),
        ("연료비·충전량", "없음", "둘 다 없음 → .dat 필요"),
        ("운행·빈차거리", "엑셀", "건별 거리 합, CSV=미제공"),
        ("출고·입고", "없음", "둘 다 없음 → .dat 필요"),
        ("기사명·차종", "CSV", "driver_name·car_type 포함"),
        ("결제수단별 분석", "엑셀", "현금/앱/카드 구분"),
    ]
    for field, src, note in erp:
        w(f"  {field}: {src} — {note}")
    w("")

    # month totals
    w("[9] 4월 전체 합계")
    w(f"  CSV 정산 합: {tmoney['daily_total_income']:,}원 / 건수 {tmoney['daily_total_trips']:,}")
    w(f"  엑셀 최종요금 합: {int(trips['income'].sum()):,}원 / 건수 {len(trips):,}")
    card_all = trips[trips["pay_method"].str.contains("카드", na=False)]
    w(f"  엑셀 카드만: {int(card_all['income'].sum()):,}원 / 건수 {len(card_all):,}")
    w(f"  차이(엑셀 전체 - CSV): {int(trips['income'].sum()) - tmoney['daily_total_income']:,}원")
    w("")

    if len(both):
        both["card_diff"] = both["card_income"] - both["csv_income"]
        both["card_trip_diff"] = both["card_trips"] - both["csv_trips"]
        w("[10] 카드 구간 — 차이 큰 샘플 5건")
        worst = both.reindex(both["card_diff"].abs().sort_values(ascending=False).index).head(5)
        for _, r in worst.iterrows():
            w(
                f"  {r['key']}: 엑셀카드={int(r['card_income']):,}({int(r['card_trips'])}건) "
                f"CSV={int(r['csv_income']):,}({int(r['csv_trips'])}건) diff={int(r['card_diff']):,}"
            )
        w("")

    w("[11] 종합 판단")
    w("  · **ERP 실입금·건수(카드)**: T머니 CSV가 정확하고 이미 ERP 기준 데이터.")
    w("  · **현금·앱·전체 운임·거리·건별 시각**: 주행내역 엑셀이 유일/우수.")
    w("  · **두 파일 대체 관계 아님** — CSV=정산, 엑셀=미터 포털 운행로그.")
    w("  · 카드만 비교해도 완전 일치는 아님 → 승인일·영업일·건 정의 차이 가능.")
    w("  · 연료·총시간·출고입고 필요 시 .dat 여전히 필수.")
    w("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT}")
    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
