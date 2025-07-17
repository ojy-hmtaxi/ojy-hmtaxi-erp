# OJY-HMTAXI-ERP

택시 회사 ERP 시스템

## 설치 및 설정

### 1. 의존성 설치
```bash
pip install -r requirements.txt
```

### 2. 환경변수 설정

프로젝트 루트에 `.env` 파일을 생성하고 다음 내용을 추가하세요:

```
GITHUB_TOKEN=your_github_personal_access_token_here
```

#### GitHub Personal Access Token 생성 방법:
1. GitHub.com → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token (classic)
3. 권한 설정: `repo` (전체 체크)
4. 토큰 생성 후 복사하여 `.env` 파일에 붙여넣기

### 3. 애플리케이션 실행
```bash
python app.py
```

## 보안 주의사항

- **절대 `.env` 파일을 Git에 커밋하지 마세요!**
- GitHub Personal Access Token이 노출되면 즉시 재생성하세요
- `.env` 파일은 로컬에만 보관하세요

## 기능

- 급여 계산
- 배차 관리
- 사고 관리
- 운전기사 관리
- 사고 현장 약도 그리기 및 저장
