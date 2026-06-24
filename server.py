#!/usr/bin/env python3
"""끌리메 교육팀 주간보고 자동화 서버 (포트 8081)"""

import csv
import json
import os
import re
import time
import threading
import requests
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta, date
from urllib.parse import urlparse, parse_qs

try:
    import config as _cfg
    BASE_URL = _cfg.BASE_URL
    EMAIL    = _cfg.EMAIL
    PASSWORD = _cfg.PASSWORD
    PORT     = _cfg.PORT
except ImportError:
    BASE_URL = os.environ.get('BASE_URL', 'https://beautyrise-academy.vercel.app')
    EMAIL    = os.environ.get('EMAIL', '')
    PASSWORD = os.environ.get('PASSWORD', '')
    PORT     = int(os.environ.get('PORT', '8081'))

CACHE_TTL = 300
COURSE_CACHE_TTL = 3600
EVAL_CACHE_TTL   = 3600

_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
HTML_PATH    = os.path.join(_BASE_DIR, 'index.html')
HISTORY_FILE = os.path.join(_BASE_DIR, 'history.json')

try:
    ROSTER_SHEET_ID     = _cfg.ROSTER_SHEET_ID
    _roster_active_gid  = _cfg.ROSTER_ACTIVE_GID
    _roster_resigned_gid = _cfg.ROSTER_RESIGNED_GID
except NameError:
    ROSTER_SHEET_ID      = os.environ.get('ROSTER_SHEET_ID', '')
    _roster_active_gid   = os.environ.get('ROSTER_ACTIVE_GID', '0')
    _roster_resigned_gid = os.environ.get('ROSTER_RESIGNED_GID', '')
ROSTER_ACTIVE_URL   = f'https://docs.google.com/spreadsheets/d/{ROSTER_SHEET_ID}/export?format=csv&gid={_roster_active_gid}'
ROSTER_RESIGNED_URL = f'https://docs.google.com/spreadsheets/d/{ROSTER_SHEET_ID}/export?format=csv&gid={_roster_resigned_gid}'
ROSTER_CACHE_TTL = 300

EXCLUDE_TITLES = {'테스트입니다', '업로드 테스트', '액티브리페어', '윤곽관리'}

PROGRAMS = [
    'DMTS/더마리젠', 'RbL데콜테/석고', '골드상하체후면', '골드페이스라인',
    '끌링 작은얼굴 이목구비', '끌링 코어 트레이닝', '뉴작은얼굴', '바스트 상체라인',
    '슬리/스핀마이저', '이목구비', '작은얼굴', '크레이지대칭', '클렌징/모델링/마무리',
    '페트/페라리/스피큘'
]
PROGRAMS_SET = set(PROGRAMS)

BELT_ORDER = ['화이트', '옐로우', '퍼플', '블루', '레드', '블랙', '실버']

# 미수료 요약에서 제외할 프로그램
INSTORE_PROGRAMS    = {'DMTS/더마리젠', '슬리/스핀마이저', '페트/페라리/스피큘', '끌링 코어 트레이닝'}  # 전 벨트 제외
WHITE_EXCL_PROGRAMS  = {'크레이지대칭', '이목구비', '뉴작은얼굴'}   # 화이트벨트 추가 제외
YELLOW_EXCL_PROGRAMS = {'크레이지대칭'}                              # 옐로우벨트 추가 제외

_session = None
_cache = {}
_cache_ts = 0
_courses_cache = []
_courses_cache_ts = 0
_cancelled_cache = None
_cancelled_cache_ts = 0
_course_stats_cache = {}   # {course_id: (stats_dict, timestamp)}
_eval_cache = None
_eval_cache_ts = 0
_roster_cache = None
_roster_cache_ts = 0
_lock = threading.Lock()

# ─── 인증 ─────────────────────────────────────────

def do_login():
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    page = s.get(f"{BASE_URL}/login?brand=cclime", timeout=10)
    html = page.text
    a0 = re.search(r'name="\$ACTION_1:0"[^>]+value="([^"]+)"', html)
    a1 = re.search(r'name="\$ACTION_1:1"[^>]+value="([^"]+)"', html)
    ak = re.search(r'name="\$ACTION_KEY"[^>]+value="([^"]+)"', html)
    s.post(
        f"{BASE_URL}/login",
        files={
            '$ACTION_REF_1': (None, '1'),
            '$ACTION_1:0': (None, a0.group(1).replace('&quot;', '"')),
            '$ACTION_1:1': (None, a1.group(1).replace('&quot;', '"')),
            '$ACTION_KEY': (None, ak.group(1)),
            'brand': (None, 'cclime'),
            'credential': (None, EMAIL),
            'password': (None, PASSWORD),
        },
        allow_redirects=True, timeout=15
    )
    sess = s.get(f"{BASE_URL}/api/auth/session", timeout=10).json()
    if not sess or isinstance(sess, str):
        raise Exception("로그인 실패")
    return s

def get_session():
    global _session
    with _lock:
        if _session is None:
            _session = do_login()
    return _session

def reset_session():
    global _session
    with _lock:
        _session = None

# ─── RSC 파싱 ─────────────────────────────────────

def get_pushes(html):
    raw_list = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html, re.DOTALL)
    result = []
    for raw in raw_list:
        try:
            content = json.loads(f'"{raw}"')
        except Exception:
            content = raw.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
        result.append(content)
    return result

def find_json_array(content, key):
    """중첩 브래킷을 지원하는 JSON 배열 추출"""
    pattern = f'"{key}":'
    idx = content.find(pattern)
    if idx == -1:
        return None
    arr_start = content.find('[', idx + len(pattern))
    if arr_start == -1:
        return None
    depth = 0
    for i in range(arr_start, len(content)):
        if content[i] == '[':
            depth += 1
        elif content[i] == ']':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(content[arr_start:i+1])
                except Exception:
                    return None
    return None

# ─── 페이지 가져오기 ───────────────────────────────

def fetch_html(session, path):
    resp = session.get(
        f"{BASE_URL}{path}",
        headers={'Accept': 'text/html'},
        timeout=20
    )
    return resp.text

# ─── 교육 목록 (전체 페이지 수집) ─────────────────

def extract_courses_from_html(html):
    pushes = get_pushes(html)
    status_lookup = {}
    for push in pushes:
        arr = find_json_array(push, 'courses')
        if not arr or any(c.get('deletedAt') for c in arr):
            continue
        for c in arr:
            status_lookup[(c.get('title', ''), c.get('startDate', ''))] = c.get('status', '')
        break

    courses = []
    for push in pushes:
        if '"/courses/' not in push:
            continue
        title_m = re.search(r'text-\[15px\] truncate[^}]+?"children":"([^"]+)"', push)
        date_m = re.search(
            r'"children":\["(\d{4}-\d{2}-\d{2})"," ~ ","(\d{4}-\d{2}-\d{2})"," · ","([^"]+)"',
            push
        )
        if title_m and date_m:
            title = title_m.group(1)
            if title in EXCLUDE_TITLES:
                continue
            id_m = re.search(r'"/courses/([a-f0-9-]{36})"', push)
            start = date_m.group(1)
            courses.append({
                'id': id_m.group(1) if id_m else '',
                'title': title,
                'startDate': start,
                'endDate': date_m.group(2),
                'type': date_m.group(3).strip(),
                'status': status_lookup.get((title, start), ''),
            })
    return courses

def extract_cancelled_courses(html):
    pushes = get_pushes(html)
    for push in pushes:
        arr = find_json_array(push, 'courses')
        if not arr:
            continue
        if not any(c.get('deletedAt') for c in arr):
            continue
        result = []
        for c in arr:
            title = c.get('title', '')
            if title in EXCLUDE_TITLES:
                continue
            if c.get('status') != '폐강':
                continue
            deleted_raw = c.get('deletedAt', '') or ''
            deleted_clean = deleted_raw.lstrip('$D')[:10] if deleted_raw else ''
            result.append({
                'id': c.get('id', ''),
                'title': title,
                'curriculumName': c.get('curriculumName', ''),
                'status': '폐강',
                'startDate': c.get('startDate', ''),
                'deletedAt': deleted_clean,
            })
        return result
    return []

def fetch_cancelled_courses(session):
    global _cancelled_cache, _cancelled_cache_ts
    now = time.time()
    if _cancelled_cache is not None and now - _cancelled_cache_ts < CACHE_TTL:
        return _cancelled_cache
    try:
        html = fetch_html(session, '/courses')
        _cancelled_cache = extract_cancelled_courses(html)
        _cancelled_cache_ts = now
        return _cancelled_cache
    except Exception:
        return []

def fetch_course_stats(session, course_id):
    now = time.time()
    if course_id in _course_stats_cache:
        stats, ts = _course_stats_cache[course_id]
        if now - ts < CACHE_TTL:
            return stats
    try:
        html = fetch_html(session, f'/courses/{course_id}')
        pushes = get_pushes(html)
        full = '\n'.join(pushes)

        # 수강생별 참석 상태 (string initial)
        attend_map = dict(re.findall(r'"applicationId":"([^"]+)","initial":"([^"]+)"', full))
        # 수강생별 bool 쌍 — 두 번째 bool(L29)이 1회차 여부
        bool_pairs: dict[str, list[bool]] = {}
        for app_id, bval in re.findall(r'"applicationId":"([^"]+)","initial":(true|false)', full):
            bool_pairs.setdefault(app_id, []).append(bval == 'true')

        enrolled = len(attend_map)
        attended = sum(1 for s in attend_map.values() if s == '참석')
        # 1회차 수료자 = 두 번째 bool True + 참석
        first_time = sum(
            1 for app_id, status in attend_map.items()
            if status == '참석'
            and len(bool_pairs.get(app_id, [])) >= 2
            and bool_pairs[app_id][1]
        )
        stats = {'enrolled': enrolled, 'attended': attended, 'first_time': first_time}
        _course_stats_cache[course_id] = (stats, now)
        return stats
    except Exception:
        return {'enrolled': 0, 'attended': 0, 'first_time': 0}

def fetch_all_courses(session):
    global _courses_cache, _courses_cache_ts
    now = time.time()
    if _courses_cache and now - _courses_cache_ts < COURSE_CACHE_TTL:
        return _courses_cache

    all_courses = []
    seen = set()
    for page_num in range(1, 11):
        url = f"/courses" if page_num == 1 else f"/courses?listPage={page_num}"
        try:
            html = fetch_html(session, url)
            courses = extract_courses_from_html(html)
            if not courses:
                break
            for c in courses:
                key = (c['title'], c['startDate'])
                if key not in seen:
                    seen.add(key)
                    all_courses.append(c)
        except Exception:
            break

    _courses_cache = all_courses
    _courses_cache_ts = now
    return all_courses

# ─── 대시보드 파서 ─────────────────────────────────

def parse_dashboard(html):
    pushes = get_pushes(html)
    full = '\n'.join(pushes)

    units_pattern = re.findall(r'(\d+)(?:건|명)', full)
    stats = {}
    if len(units_pattern) >= 4:
        stats['total_edu'] = int(units_pattern[0])
        stats['completed'] = int(units_pattern[1])
        stats['students'] = int(units_pattern[2])
        stats['graduates'] = int(units_pattern[3])

    for label, key in [('전체 교육', 'total_edu'), ('수료 완료', 'completed'),
                       ('교육생', 'students'), ('수료인원', 'graduates')]:
        m = re.search(rf'"{label}"[\s\S]{{0,300}}?(\d+)(?:건|명)', full)
        if m:
            stats[key] = int(m.group(1))

    staff_data = []
    for content in pushes:
        arr = find_json_array(content, 'staffData')
        if arr:
            staff_data = arr
            break

    total_programs = 14
    branch_map = {}
    for staff in staff_data:
        branch = staff.get('branchName', '미지정')
        completed = len(staff.get('completedPrograms', []))
        if branch not in branch_map:
            branch_map[branch] = {'total': 0, 'sum_completed': 0, 'all_done': 0}
        branch_map[branch]['total'] += 1
        branch_map[branch]['sum_completed'] += completed
        if completed >= total_programs:
            branch_map[branch]['all_done'] += 1

    branches = []
    for name, b in sorted(branch_map.items()):
        rate = min(100, round(b['sum_completed'] / (b['total'] * total_programs) * 100)) if b['total'] > 0 else 0
        branches.append({'name': name, 'total': b['total'], 'all_done': b['all_done'], 'rate': rate})

    return {'stats': stats, 'branches': branches, 'staff_count': len(staff_data), 'staff_data': staff_data}

# ─── 과제 파서 ────────────────────────────────────

def parse_tasks(html):
    pushes = get_pushes(html)
    for content in pushes:
        tasks = find_json_array(content, 'tasks')
        if tasks:
            by_status = {}
            for t in tasks:
                s = t.get('status', 'UNKNOWN')
                by_status.setdefault(s, []).append(t)
            return {
                'tasks': tasks,
                'by_status': by_status,
                'total': len(tasks),
                'pending': len(by_status.get('PENDING', [])),
                'completed': len(by_status.get('COMPLETED', [])),
                'overdue': len(by_status.get('OVERDUE', [])),
            }
    return {'tasks': [], 'total': 0, 'pending': 0, 'completed': 0, 'overdue': 0}

# ─── 평가 데이터 ───────────────────────────────────

def parse_eval_list_from_html(html):
    """평가 목록 페이지에서 커리큘럼+점수+날짜 추출"""
    pushes = get_pushes(html)
    evals = []
    seen = set()

    def extract_from_chunk(eval_id, chunk):
        score_m = re.search(r'"children":"(\d+(?:\.\d+)?)점"', chunk)
        if not score_m:
            return None
        curriculum = None
        for part in re.findall(r'"children":"([^"]{2,40})"', chunk):
            if part in PROGRAMS_SET:
                curriculum = part
                break
        date_m = re.search(r'(\d{4})\. (\d{1,2})\. (\d{1,2})\.', chunk)
        date_str = None
        if date_m:
            date_str = f"{date_m.group(1)}-{date_m.group(2).zfill(2)}-{date_m.group(3).zfill(2)}"
        return {'id': eval_id, 'score': float(score_m.group(1)), 'curriculum': curriculum, 'date': date_str}

    # Method 1: 데스크탑 테이블 행 (UUID 별 1개 push)
    for push in pushes:
        m_id = re.search(r'"tr","([a-f0-9-]{36})"', push)
        if not m_id:
            continue
        eid = m_id.group(1)
        if eid in seen:
            continue
        r = extract_from_chunk(eid, push)
        if r:
            seen.add(eid)
            evals.append(r)

    # Method 2: 모바일 li 항목 (데스크탑 행이 없는 경우 보완)
    for push in pushes:
        for m in re.finditer(r'"li","([a-f0-9-]{36})"', push):
            eid = m.group(1)
            if eid in seen:
                continue
            chunk = push[m.start(): m.start() + 2000]
            r = extract_from_chunk(eid, chunk)
            if r:
                seen.add(eid)
                evals.append(r)

    return evals

def fetch_evaluations(session):
    """평가 목록 수집 (1시간 캐시)"""
    global _eval_cache, _eval_cache_ts
    now = time.time()
    if _eval_cache is not None and now - _eval_cache_ts < EVAL_CACHE_TTL:
        return _eval_cache

    try:
        html = fetch_html(session, '/evaluations?view=list&inactive=1')
        result = parse_eval_list_from_html(html)
        _eval_cache = result
        _eval_cache_ts = now
        return result
    except Exception:
        return []

def aggregate_evaluations(eval_list):
    """프로그램별 평균 점수 및 재교육 통계"""
    by_curr = {}
    re_edu_count = 0
    for ev in eval_list:
        curr = ev.get('curriculum')
        if not curr:
            continue
        score = ev.get('score', 0)
        by_curr.setdefault(curr, {'scores': [], 'below70': 0})
        by_curr[curr]['scores'].append(score)
        if score < 70:
            by_curr[curr]['below70'] += 1
            re_edu_count += 1
    result_list = sorted([
        {
            'curriculum': c,
            'avg_score': round(sum(d['scores']) / len(d['scores']), 1),
            'count': len(d['scores']),
            'below70': d['below70'],
        }
        for c, d in by_curr.items()
    ], key=lambda x: x['avg_score'], reverse=True)
    total = sum(e.get('score') is not None for e in eval_list if e.get('curriculum'))
    return {
        'by_curriculum': result_list,
        're_edu_count': re_edu_count,
        're_edu_ratio': round(re_edu_count / total * 100, 1) if total else 0,
        'total': total,
    }

# ─── 벨트별 미수료 요약 ────────────────────────────

def parse_belt_summary(staff_data):
    belt_map = {}
    for staff in staff_data:
        belt = staff.get('belt', '미지정')
        completed = set(staff.get('completedPrograms', []))
        # 벨트별 대상 프로그램: 매장내교육 전 벨트 제외 + 화이트는 추가 3개 제외
        belt_excl = WHITE_EXCL_PROGRAMS if belt == '화이트' else YELLOW_EXCL_PROGRAMS if belt == '옐로우' else set()
        excluded = INSTORE_PROGRAMS | belt_excl
        target = [p for p in PROGRAMS if p not in excluded]
        incomplete = [p for p in target if p not in completed]
        if belt not in belt_map:
            belt_map[belt] = {'total': 0, 'fully_complete': 0, 'incomplete_programs': {}}
        belt_map[belt]['total'] += 1
        if not incomplete:
            belt_map[belt]['fully_complete'] += 1
        for p in incomplete:
            belt_map[belt]['incomplete_programs'][p] = belt_map[belt]['incomplete_programs'].get(p, 0) + 1

    result = []
    for belt in BELT_ORDER:
        if belt not in belt_map:
            continue
        bm = belt_map[belt]
        top3 = sorted(bm['incomplete_programs'].items(), key=lambda x: x[1], reverse=True)[:3]
        result.append({
            'belt': belt,
            'total': bm['total'],
            'complete': bm['fully_complete'],
            'incomplete_count': bm['total'] - bm['fully_complete'],
            'rate': round(bm['fully_complete'] / bm['total'] * 100) if bm['total'] else 0,
            'top_incomplete': [{'program': p, 'count': c} for p, c in top3],
        })
    return result

# ─── 월별 지표 히스토리 ───────────────────────────

def _load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_history(history):
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f, ensure_ascii=False)
    except Exception:
        pass

def save_monthly_metric(key, value):
    """당월 지표값을 history.json에 저장 (월 1회 누적)"""
    if value is None:
        return
    history = _load_history()
    month_key = date.today().strftime('%Y-%m')
    history.setdefault(key, {})[month_key] = value
    # 최근 13개월만 보관
    if len(history[key]) > 13:
        del history[key][sorted(history[key].keys())[0]]
    _save_history(history)

def save_weekly_completion(week_start_iso, attended_count):
    """주별 미수료해소건(참석 완료 수)을 history.json에 저장"""
    history = _load_history()
    cc = history.setdefault('completion_cases', {})
    cc[week_start_iso] = attended_count
    # 최근 52주만 보관
    if len(cc) > 52:
        del cc[sorted(cc.keys())[0]]
    _save_history(history)

def get_monthly_completion_total(month_key):
    """해당 월의 미수료해소건 주별 합산"""
    history = _load_history()
    cc = history.get('completion_cases', {})
    return sum(v for k, v in cc.items() if k.startswith(month_key))

def get_monthly_history():
    """history.json에서 월별 통합 지표 반환 (시계열 리스트)"""
    history = _load_history()
    incomplete_data  = history.get('monthly_incomplete', {})
    re_edu_data      = history.get('re_edu_ratio', {})
    retention_data   = history.get('retention_rate', {})
    completion_cc    = history.get('completion_cases', {})

    all_months = sorted(set(
        list(incomplete_data.keys()) +
        list(re_edu_data.keys()) +
        list(retention_data.keys())
    ))

    result = []
    prev_incomplete = None
    for month in all_months:
        inc = incomplete_data.get(month)
        delta = (inc - prev_incomplete) if (inc is not None and prev_incomplete is not None) else None
        monthly_comp = sum(v for k, v in completion_cc.items() if k.startswith(month))
        result.append({
            'month': month,
            'incomplete': inc,
            'incomplete_delta': delta,
            're_edu_ratio': re_edu_data.get(month),
            'retention_rate': retention_data.get(month),
            'completion': monthly_comp if monthly_comp > 0 else None,
        })
        if inc is not None:
            prev_incomplete = inc
    return result

# ─── 인명부 (Google Sheets) ───────────────────────

def parse_korean_date(s):
    """'2024년 1월 29일' → date 객체"""
    m = re.search(r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', s or '')
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return None

def _month_ago(n):
    """오늘 기준 n개월 전의 'YYYY-MM' 문자열"""
    today = date.today()
    y, m = today.year, today.month - n
    while m <= 0:
        m += 12
        y -= 1
    return f'{y:04d}-{m:02d}'

def _parse_roster_sheet(url, is_active_flag):
    """구글 시트 CSV → staff 리스트. is_active_flag: True=재직자탭, False=퇴직자탭"""
    resp = requests.get(url, timeout=15)
    resp.encoding = 'utf-8'
    rows = list(csv.reader(StringIO(resp.text)))

    header_idx = next(
        (i for i, r in enumerate(rows) if '지점' in r and '이름' in r and '입사일' in r),
        None
    )
    if header_idx is None:
        return []

    hdr = rows[header_idx]
    def col(name):
        return hdr.index(name) if name in hdr else -1

    c_branch  = col('지점')
    c_name    = col('이름')
    c_job     = col('직무')
    c_belt    = col('벨트')
    c_joined  = col('입사일')
    c_stable  = col('근무안정도')

    staff = []
    for row in rows[header_idx + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        def get(c):
            return row[c].strip() if 0 <= c < len(row) else ''
        name   = get(c_name)
        branch = get(c_branch)
        if not name or not branch:
            continue
        staff.append({
            'name':    name,
            'branch':  branch,
            'job':     get(c_job),
            'belt':    get(c_belt),
            'joined':  parse_korean_date(get(c_joined)),
            'stable':  get(c_stable),
            'active':  is_active_flag,
        })
    return staff

def fetch_roster():
    """재직자(gid=0) + 퇴직자(gid=681652795) 두 탭을 합산한 전체 인명부 반환"""
    global _roster_cache, _roster_cache_ts
    now = time.time()
    if _roster_cache is not None and now - _roster_cache_ts < ROSTER_CACHE_TTL:
        return _roster_cache
    try:
        active_staff   = _parse_roster_sheet(ROSTER_ACTIVE_URL,   True)
        resigned_staff = _parse_roster_sheet(ROSTER_RESIGNED_URL, False)
        all_staff = active_staff + resigned_staff
        _roster_cache = all_staff
        _roster_cache_ts = now
        return all_staff
    except Exception as e:
        print(f'인명부 fetch 오류: {e}')
        return _roster_cache or []

def compute_retention_from_roster(roster):
    """인명부 기반 신규입사자 재직률
    - 대상: 직전 3개월(M-1, M-2, M-3) 입사자
    - 분모: 해당 기간 입사자 전체 (퇴사자 포함 — 퇴사(예정)일이 과거이면 퇴사 처리)
    - 분자: 그 중 현재 재직 중 (퇴사(예정)일 없거나 오늘 이후)
    - 화이트벨트: 현재 재직 중인 분자 중 벨트가 화이트인 인원
    """
    today = date.today()

    def is_active(s):
        # 재직자 탭이면 active=True, 퇴직자 탭이면 active=False
        return s.get('active', True)

    # 직전 3개월 (당월 제외)
    ref_months = [_month_ago(i) for i in range(1, 4)]  # [M-1, M-2, M-3]
    ref_set = set(ref_months)

    def joined_mk(s):
        jd = s.get('joined')
        return f'{jd.year:04d}-{jd.month:02d}' if jd else ''

    # 화이트벨트만 대상 (재직자·퇴직자 모두 포함)
    cohort = [s for s in roster if joined_mk(s) in ref_set and s.get('belt', '').strip() == '화이트']
    active_cohort = [s for s in cohort if is_active(s)]

    # 월별 세부 집계
    by_month = {}
    for mk in ref_months:
        total_m  = [s for s in cohort        if joined_mk(s) == mk]
        active_m = [s for s in active_cohort if joined_mk(s) == mk]
        by_month[mk] = {
            'total':  len(total_m),
            'active': len(active_m),
            'left':   len(total_m) - len(active_m),
        }

    total_n  = len(cohort)
    active_n = len(active_cohort)
    rate = round(active_n / total_n * 100) if total_n > 0 else None

    # 근무안정도 (화이트벨트 재직자)
    stability = {'매우안정': 0, '안정': 0, '보통': 0, '불안정': 0}
    for s in active_cohort:
        st = s.get('stable', '')
        if st == '매우안정':   stability['매우안정'] += 1
        elif st == '안정':     stability['안정'] += 1
        elif st == '보통':     stability['보통'] += 1
        elif '불안정' in st:   stability['불안정'] += 1

    return {
        'rate':          rate,
        'ref_months':    ref_months,
        'by_month':      by_month,
        'total_cohort':  total_n,
        'active_cohort': active_n,
        'white_active':  active_n,
        'stability':     stability,
        'all_active':    sum(1 for s in roster if s.get('active', True)),
    }

# ─── 신규입사자 정착률 (당월 기준) ───────────────

def fetch_retention_monthly(session, all_courses, staff_data, month):
    """당월 신규입사자 과정 수강자 중 현재 재직 비율"""
    new_emp_courses = [
        c for c in all_courses
        if '신규' in c.get('title', '')
        and c.get('startDate', '').startswith(month)
        and c.get('id')
    ]
    if not new_emp_courses:
        return {'rate': None, 'enrolled': 0, 'retained': 0, 'note': '당월 신규과정 없음'}

    staff_names = {s.get('name', '') for s in staff_data}
    enrolled_names = set()
    for course in new_emp_courses[:5]:
        try:
            detail_html = fetch_html(session, f"/courses/{course['id']}")
            dfull = '\n'.join(get_pushes(detail_html))
            names = re.findall(r'"name":"([^"]+)"', dfull)
            enrolled_names.update(names)
        except Exception:
            pass

    if not enrolled_names:
        return {'rate': None, 'enrolled': 0, 'retained': 0, 'note': '수강자 정보 없음'}

    retained = enrolled_names & staff_names
    return {
        'rate': round(len(retained) / len(enrolled_names) * 100),
        'enrolled': len(enrolled_names),
        'retained': len(retained),
        'note': f"{month} 기준",
    }

# ─── 데이터 수집 ───────────────────────────────────

def collect_all(week_start_str=None):
    global _cache, _cache_ts
    now = time.time()

    if week_start_str:
        try:
            week_start = date.fromisoformat(week_start_str)
        except Exception:
            week_start = date.today() - timedelta(days=date.today().weekday())
    else:
        today = date.today()
        week_start = today - timedelta(days=today.weekday())

    week_end = week_start + timedelta(days=6)
    next_week_start = week_start + timedelta(days=7)
    next_week_end = week_start + timedelta(days=13)

    cache_key = week_start.isoformat()
    if _cache.get('_key') == cache_key and now - _cache_ts < CACHE_TTL:
        return _cache

    session = get_session()
    try:
        dash_html  = fetch_html(session, '/dashboard')
        tasks_html = fetch_html(session, '/tasks')
        all_courses = fetch_all_courses(session)
        cancelled_trash = fetch_cancelled_courses(session)
    except Exception:
        reset_session()
        raise

    # 대시보드 파싱 (staff_data 포함)
    dash_data = parse_dashboard(dash_html)
    staff_data = dash_data.get('staff_data', [])

    # 날짜 기준 교육 분류 (폐강 제외)
    this_week, next_week, upcoming, recent = [], [], [], []
    main_list_cancelled = []
    trash_ids = {c['id'] for c in cancelled_trash}

    for c in all_courses:
        try:
            if c.get('status') == '폐강':
                if c.get('id') not in trash_ids:
                    main_list_cancelled.append({
                        'id': c.get('id', ''),
                        'title': c.get('title', ''),
                        'curriculumName': c.get('type', ''),
                        'status': '폐강',
                        'startDate': c.get('startDate', ''),
                        'deletedAt': '',
                    })
                continue
            sd = date.fromisoformat(c['startDate'])
            ed = date.fromisoformat(c['endDate'])
            overlaps_this_week = sd <= week_end and ed >= week_start
            if overlaps_this_week:
                this_week.append(c)
            elif week_end < sd <= next_week_end:
                next_week.append(c)
            elif sd > next_week_end:
                upcoming.append(c)
            elif sd < week_start:
                recent.append(c)
        except Exception:
            pass

    cancelled = cancelled_trash + main_list_cancelled
    recent.sort(key=lambda x: x['startDate'], reverse=True)

    # 이번 주 교육 학생 수·수료 수
    weekly_enrolled = 0
    weekly_attended = 0
    weekly_first_time = 0
    for c in this_week:
        if c.get('id'):
            stats = fetch_course_stats(session, c['id'])
            c['enrolled'] = stats['enrolled']
            c['attended'] = stats['attended']
            c['first_time'] = stats.get('first_time', 0)
            weekly_enrolled += stats['enrolled']
            weekly_attended += stats['attended']
            weekly_first_time += stats.get('first_time', 0)

    # 당월 교육 타입별 집계 (참여/수료 건수)
    monthly_type_stats = {}
    monthly_courses = [
        c for c in all_courses
        if c.get('startDate', '').startswith(date.today().strftime('%Y-%m'))
        and c.get('status') != '폐강'
        and c.get('id')
    ]
    def _fetch_typed(c):
        return c, fetch_course_stats(session, c['id'])
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_fetch_typed, c): c for c in monthly_courses}
        for f in as_completed(futures):
            try:
                c, st = f.result()
                ct = (c.get('type') or '기타').strip() or '기타'
                if ct not in monthly_type_stats:
                    monthly_type_stats[ct] = {'courses': 0, 'enrolled': 0, 'attended': 0}
                monthly_type_stats[ct]['courses'] += 1
                monthly_type_stats[ct]['enrolled'] += st.get('enrolled', 0)
                monthly_type_stats[ct]['attended'] += st.get('attended', 0)
                monthly_type_stats[ct]['first_time'] = monthly_type_stats[ct].get('first_time', 0) + st.get('first_time', 0)
            except Exception:
                pass

    current_month = date.today().strftime('%Y-%m')
    total_programs_count = len(PROGRAMS)
    total_incomplete = sum(
        total_programs_count - len(s.get('completedPrograms', []))
        for s in staff_data
    )
    # 미수료 적체 (당월 저장)
    save_monthly_metric('monthly_incomplete', total_incomplete)
    # 미수료해소건 = 1회차 수료자 (주별 누적)
    save_weekly_completion(week_start.isoformat(), weekly_first_time)
    monthly_completion = get_monthly_completion_total(current_month)

    # 벨트별 미수료 요약
    belt_summary = parse_belt_summary(staff_data)

    # 평가 통계 (당월 데이터만 필터링)
    eval_list = fetch_evaluations(session)
    monthly_evals = [e for e in eval_list if (e.get('date') or '').startswith(current_month)]
    eval_stats = aggregate_evaluations(monthly_evals if monthly_evals else eval_list)
    eval_stats['month'] = current_month
    eval_stats['month_filtered'] = bool(monthly_evals)
    save_monthly_metric('re_edu_ratio', eval_stats.get('re_edu_ratio') if monthly_evals else None)

    # 신규입사자 정착률 (인명부 연동)
    roster = fetch_roster()
    retention = compute_retention_from_roster(roster)
    save_monthly_metric('retention_rate', retention.get('rate'))

    # 월별 히스토리 (UI용)
    monthly_history = get_monthly_history()

    courses_data = {
        'total': len(all_courses),
        'this_week': this_week,
        'next_week': next_week,
        'upcoming': upcoming[:10],
        'recent': recent[:5],
        'all': all_courses[:30],
    }

    result = {
        '_key': cache_key,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'week': {
            'start': week_start.isoformat(),
            'end': week_end.isoformat(),
            'label': f"{week_start.year}년 {week_start.month}월 {week_start.day}일 ~ {week_end.month}월 {week_end.day}일",
        },
        'dashboard': dash_data,
        'courses': courses_data,
        'tasks': parse_tasks(tasks_html),
        'weekly_stats': {
            'course_count': len(this_week),
            'enrolled': weekly_enrolled,
            'attended': weekly_attended,
            'completion': weekly_first_time,  # 미수료해소건 = 이번 주 1회차 수료자
        },
        'monthly_completion': monthly_completion,
        'monthly_type_stats': monthly_type_stats,
        'cancelled': cancelled,
        'eval_stats': eval_stats,
        'belt_summary': belt_summary,
        'total_incomplete': total_incomplete,
        'retention': retention,
        'monthly_history': monthly_history,
        'current_month': current_month,
        'roster_retention': retention,
    }

    _cache = result
    _cache_ts = now
    return result

# ─── HTTP 서버 ─────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path in ('/', '/index.html'):
            self._serve_file()
        elif path == '/api/weekly':
            week_start = params.get('weekStart', [None])[0]
            self._serve_api(week_start)
        elif path == '/api/refresh':
            global _cache, _cache_ts, _courses_cache, _courses_cache_ts
            global _cancelled_cache, _cancelled_cache_ts, _course_stats_cache, _eval_cache, _eval_cache_ts
            global _roster_cache, _roster_cache_ts
            _cache = {}
            _cache_ts = 0
            _courses_cache = []
            _courses_cache_ts = 0
            _cancelled_cache = None
            _cancelled_cache_ts = 0
            _course_stats_cache = {}
            _eval_cache = None
            _eval_cache_ts = 0
            _roster_cache = None
            _roster_cache_ts = 0
            self._json({'status': 'refreshed'})
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_file(self):
        try:
            with open(HTML_PATH, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def _serve_api(self, week_start=None):
        try:
            data = collect_all(week_start)
            self._json(data)
        except Exception as e:
            self._json({'error': str(e)}, 500)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == '__main__':
    print(f"✅ 주간보고 서버 시작: http://localhost:{PORT}")
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
