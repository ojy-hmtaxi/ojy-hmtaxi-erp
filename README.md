# OJY-HMTAXI-ERP

택시 회사 ERP 시스템

## 설치 및 설정 (로컬)

### 1. 저장소 클론 및 가상환경 (권장)
```bash
git clone <저장소 URL>
cd ojy-hmtaxi-erp
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

### 2. 의존성 설치
```bash
pip install -r requirements.txt
```

### 3. 로컬 실행
```bash
python app.py
```

브라우저에서 `http://127.0.0.1:5000` 으로 접속합니다.

- 최초 실행 시 `instance/users.db`(SQLite), `data/`, `uploads/` 폴더가 자동 생성됩니다.
- 관리자 계정은 회원가입 후 DB에서 `role`을 `admin`으로 변경하거나, 최초 사용자를 직접 등록해 사용합니다.

### 4. 로컬 데이터 경로

| 경로 | 용도 |
|------|------|
| `data/*.json` | 배차·급여·사고·기사·수입금 등 업무 데이터 |
| `uploads/` | 업로드한 엑셀·약도 PNG/JSON |
| `uploads/dat/` | 수입금 `.dat` 원본 |
| `instance/users.db` | 사용자·메시지·업로드 이력 |

> `data/`, `uploads/`, `instance/` 는 `.gitignore`에 포함되어 Git에는 올라가지 않습니다. 로컬에서 엑셀/dat를 업로드해 데이터를 쌓습니다.

---

## Cloudtype 배포

[Cloudtype](https://cloudtype.io)에 Python 웹 앱으로 배포하는 기준 설정입니다. 프로덕션에서는 `python app.py` 대신 **gunicorn**으로 실행합니다.

### 1. 사전 준비

- GitHub 등에 저장소 연결
- Cloudtype에서 **Python** 런타임 프로젝트 생성
- `requirements.txt`에 gunicorn 추가 (배포용):

```bash
pip install gunicorn
pip freeze | findstr gunicorn   # Windows
# requirements.txt에 Gunicorn==… 한 줄 추가
```

또는 Cloudtype **빌드 명령**에 `pip install gunicorn`을 포함합니다.

### 2. 빌드 / 실행 명령 (예시)

| 항목 | 값 |
|------|-----|
| **Install / Build** | `pip install -r requirements.txt` |
| **Start command** | `gunicorn -w 2 -b 0.0.0.0:8080 app:app` |

Cloudtype 대시보드에서 포트를 `$PORT`로 받는 경우:

```bash
gunicorn -w 2 -b 0.0.0.0:$PORT app:app
```

### 3. 환경 변수 (Cloudtype 대시보드)

| 변수 | 필수 | 설명 |
|------|------|------|
| `CLOUDTYPE_ENV` | 권장 | `1` 또는 `true` — 사고 약도 PNG/JSON을 `/tmp/uploads/maps`에 저장·조회 |
| `SECRET_KEY` | 권장 | Flask 세션 암호화용 (미설정 시 코드 기본값 사용) |

예시:
```
CLOUDTYPE_ENV=1
SECRET_KEY=운영용-랜덤-긴-문자열
```

`.env` 파일은 로컬 개발용이며, Cloudtype에서는 대시보드 **환경 변수**에 직접 등록합니다.

### 4. 배포 시 자동 처리

`app.py` import 시(gunicorn 기동 포함) 아래가 자동 실행됩니다.

- SQLite 테이블 생성 (`users`, `message`, `upload_record` 등)
- `users.department` 컬럼 등 **스키마 마이그레이션**
- 사고번호·약도 파일명 **레거시 마이그레이션** (`init_accident_migrations`)

로컬에서 `python app.py`만 실행할 때와 달리, gunicorn은 `if __name__ == '__main__'` 블록을 타지 않으므로 DB 초기화는 import 시점 로직에 의존합니다.

### 5. Cloudtype vs 로컬 경로 차이

| 구분 | 로컬 | Cloudtype (`CLOUDTYPE_ENV` 설정) |
|------|------|----------------------------------|
| 사고 약도 | `uploads/maps/` | `/tmp/uploads/maps/` |
| JSON·엑셀·DB | 프로젝트 하위 `data/`, `uploads/`, `instance/` | 컨테이너 디스크 (재배포 시 초기화될 수 있음) |

**주의:** Cloudtype 기본 디스크는 **휘발성**일 수 있습니다. 재배포·재시작 후 `data/*.json`, `uploads/`, `instance/users.db`가 비어 있으면 각 메뉴에서 엑셀/dat를 **다시 업로드**해야 합니다. 약도 파일도 `/tmp`는 재시작 시 사라질 수 있으므로, 중요 데이터는 별도 백업·영구 스토리지(Cloudtype Volume 등) 사용을 권장합니다.

### 6. 배포 후 확인 체크리스트

1. 로그인·회원가입(소속 선택) 동작
2. 배차 / 급여 / 사고 / 기사 / 수입금 엑셀·dat 업로드
3. 사고 약도 저장·불러오기 (`CLOUDTYPE_ENV` 설정 여부)
4. 관리자 **사용자 관리** (`role=admin` 계정)
5. `department` 관련 DB 오류 없음 (자동 마이그레이션)

### 7. 문제 해결

| 증상 | 조치 |
|------|------|
| `users.department` 컬럼 없음 | 최신 `app.py` 배포 후 재기동 (import 시 `ensure_user_department_column` 실행) |
| 약도가 저장되지 않음 | `CLOUDTYPE_ENV=1` 설정, 로그에서 `/tmp/uploads/maps` 경로 확인 |
| 업로드 데이터가 사라짐 | 재배포 후 엑셀/dat 재업로드 또는 Volume·백업 복원 |
| 502 / 앱 기동 실패 | Start command가 `gunicorn … app:app` 인지, `requirements.txt`에 gunicorn 포함 여부 확인 |

---

## 보안 주의사항

- **SECRET_KEY**는 운영 환경에서 반드시 환경 변수로 설정하세요.
- SQLite·JSON·업로드 파일은 서버 로컬(또는 Cloudtype 디스크)에 저장됩니다. 접근 제어는 **로그인**과 관리자 권한에 의존합니다.
- `.env`는 Git에 커밋하지 마세요 (`.gitignore` 처리됨).

## 기능

### 대시보드
- 월별 **실입금·연료비** 요약 카드 (전월 대비 증감 표시)
- **배차 현황** 요약: 근무 유형별(주간·야간·일차·교대·리스) 배차일수·기사 수
- **사고 현황** 요약: 가해/피해 건수, 미결 건수, 수리·치료·보상 금액
- **월별 수입금·배차·사고** 통계 차트 (Chart.js)
  - 사고 차트: 월별 가해/피해 막대 그래프, 호버 시 **사고원인** 분포 표시

### 배차 관리
- 월별(01월~12월) 엑셀 업로드 → **월간 배차현황** 표 저장·조회
- 일별 근무 표시: `o`(승무), `x`(결근), `/`(휴가), `H`(공휴일) 등
- **인정일·승무일·결근일·휴가** 자동 집계 (엑셀 수식과 동일: 인정일 = 승무일 + 휴가 + 공휴일(`H`))
- **배차 현황 통계**: 날짜별·근무유형별 집계표
- 통합 검색: 차번, 차종, 근무, 사번, 기사명 (쉼표 다중 조건, 차종 정확 일치)
- 업로드 이력 표시 (최근 2건)

### 급여 계산 (리스)
- 월별 시트 엑셀 업로드 (`실입금`, `리스료`, `연료비` 필수)
- 급여 자동 계산: `(실입금 - 리스료 - 연료비) × 0.8`
- 월별 기사별 급여·실입금 표, **급여 순위 차트** (바+라인)
- 월별 요약: 평균·최고·최저 급여
- 통합 검색 및 업로드 이력

### 수입금 관리
- 택시 미터 **`.dat` 파일** 업로드 (단건·일괄, 진행률 표시)
- 연료 단가 입력 → **연료비** 자동 산출
- **영업분** 계산 및 `분 (시간분)` 형식 표시
- 배차 데이터와 **자동 매칭**: 사번·이름·차종 보정 (`완료` / `미매칭`)
- 월별 탭, 통합 검색, 차트 필터(차번·사번·이름·차종)
- 표 셀 **인라인 편집** 및 저장 API

### 사고 관리
- 엑셀 업로드: `가해사고` / `피해사고` 시트 분리 저장
- **사고번호** 형식 통일 (`26-G01` 등), 약도 파일명 연동
- 가해/피해 탭, **통계·분석** 탭
  - 전체·미결 건수, 수리·치료·피해·보상 금액 합계
  - **월별 사고 현황** 막대 차트 (가해/피해, 사고원인 툴팁)
  - 기사별·차량별 통계 (정렬 가능)
- **미결** 행 강조, 처리여부(종결/미결) 색상 구분
- **사고 보고서** 인쇄 (`/accident/print/...`)
- 통합 검색, 업로드 이력

### 운전기사 관리
- 기사 명부 엑셀 업로드 (사번, 이름, 면허, 입·퇴사일, 연락처 등)
- 기사 목록 검색 (사번·이름)
- **기사 프로필** 페이지: 월별 승무일·실입금·연료비·급여 표·차트
- 사고 이력 링크 (보고서 바로가기), 처리여부 색상 표시

### 차량 관리
- **정비 일정** 엑셀 업로드 → 월별 정비현황 표 (○예방 / △일반 / □사고 / !검사 / m정비소)
- **수리** 데이터 업로드
- 월별 탭 전환, 정비 통계

### 사고 현장 약도
- 캔버스 기반 **약도 그리기** (`/map`)
- 사고번호별 **PNG 이미지·JSON** 저장·불러오기
- 보고서 인쇄 시 약도 연동

### 공통·계정
- **로그인 / 회원가입** (소속 선택), 프로필 수정
- **관리자**: 사용자 목록·역할·소속 관리
- 우측 **메시지 보드** (새 글 알림, 사용자별 읽음 처리)
- 엑셀·dat 업로드 기록 DB 저장
