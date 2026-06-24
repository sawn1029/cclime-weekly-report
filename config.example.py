# 끌리메 주간보고 서버 설정 예시
# 이 파일을 config.py 로 복사한 뒤 실제 값으로 채워주세요.
# cp config.example.py config.py

# beautyrise-academy 접속 정보
BASE_URL = "https://beautyrise-academy.vercel.app"
EMAIL    = "your-admin@example.com"
PASSWORD = "your-password"

# 인명부 구글 시트 ID
# 시트 URL에서 /d/ 뒤의 긴 문자열이 ID입니다.
# 예: https://docs.google.com/spreadsheets/d/[여기]/edit
ROSTER_SHEET_ID       = 'your-google-sheet-id'
ROSTER_ACTIVE_GID     = '0'           # 재직자 탭 gid (기본값 0)
ROSTER_RESIGNED_GID   = '681652795'   # 퇴직자 탭 gid (시트 탭 URL에서 확인)

# 서버 설정
PORT = 8081
