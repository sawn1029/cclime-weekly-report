# 끌리메 교육팀 주간현황보고 웹앱

beautyrise-academy 플랫폼 데이터를 실시간으로 수집·가공하여  
주간/월간 교육 현황 보고서를 자동으로 생성하는 로컬 웹 서버입니다.

---

## 주요 기능

- **주간보고** — 이번 주 교육 현황, 교육생 수, 수료 현황, 폐강, 과제 현황
- **월간보고** — 교육 타입별(아카데미/특별) 참여·수료·1회차 수료 분석, 미수료 적체, 재교육 비율, 신규입사자 재직률
- **신규입사자 재직률** — 구글 시트 인명부 실시간 연동 (재직자·퇴직자 탭 합산, 화이트벨트 기준)
- **벨트별 미수료 요약** — 벨트별 미수료 인원 및 집중 프로그램 TOP3
- **비정기 주요업무** — 브라우저 localStorage에 저장되는 수기 업무 테이블

---

## 설치 및 실행

### 요구 사항

- Python 3.10+
- `requests` 라이브러리

```bash
pip install requests
```

### 설정

```bash
# config 파일 복사 후 실제 값 입력
cp config.example.py config.py
nano config.py
```

`config.py`에 입력할 항목:

| 항목 | 설명 |
|------|------|
| `BASE_URL` | beautyrise-academy 도메인 |
| `EMAIL` | 관리자 로그인 이메일 |
| `PASSWORD` | 관리자 비밀번호 |
| `ROSTER_SHEET_ID` | 구글 시트 인명부 ID |
| `ROSTER_ACTIVE_GID` | 재직자 탭 gid (URL에서 확인) |
| `ROSTER_RESIGNED_GID` | 퇴직자 탭 gid (URL에서 확인) |
| `PORT` | 서버 포트 (기본값 8081) |

### 실행

```bash
bash start.sh
```

브라우저에서 `http://localhost:8081` 접속

### 서버 중지

```bash
kill $(cat server.pid)
```

---

## 구글 시트 인명부 설정

인명부 시트는 **"링크가 있는 모든 사용자"** 권한으로 공개 설정되어야 합니다.

시트 구조 (필수 컬럼):

| 지점 | 이름 | 직무 | 벨트 | 근무형태 | 본인연락처 | 입사일 | 근무안정도 | 퇴사(예정)일 |
|------|------|------|------|----------|------------|--------|------------|--------------|

- 재직자 탭과 퇴직자 탭을 분리하여 관리
- 탭별 gid는 해당 탭 클릭 후 URL `gid=숫자` 에서 확인

---

## 파일 구조

```
weekly-report/
├── server.py          # 메인 서버 (포트 8081)
├── index.html         # 프론트엔드 UI
├── start.sh           # 서버 시작 스크립트
├── config.py          # ⚠️ 민감 정보 (gitignore됨)
├── config.example.py  # 설정 예시 파일
├── history.json       # 월별 지표 누적 데이터 (자동 생성)
└── .gitignore
```

---

## 주의사항

- `config.py`는 `.gitignore`에 포함되어 있어 GitHub에 올라가지 않습니다.
- `history.json`은 서버 실행 시 자동 생성됩니다.
