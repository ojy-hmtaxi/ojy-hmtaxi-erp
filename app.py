from flask import Flask, render_template, request, session, redirect, url_for, flash, send_from_directory, send_file, jsonify, Response, stream_with_context
from io import BytesIO
import pandas as pd
import os
import json
from datetime import timedelta, datetime
from collections import OrderedDict
from werkzeug.utils import secure_filename
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from models import db, User, Message, UploadRecord
from sqlalchemy.orm import joinedload
import base64
import calendar
import re
import shutil

from dat_parser import (
    parse_dat_bytes,
    compute_daily_interval_minutes,
    compute_closing_sales_metrics,
    compute_daily_sales_metrics,
    resolve_closing_business_date,
    infer_shift_band_from_start,
    is_prolonged_closing,
    is_handshake_closing_day,
    iter_closing_calendar_dates,
    clip_datetime_to_business_day,
)

from dotenv import load_dotenv
import pytz

# .env 파일 로드 (배포 환경에서는 환경변수 직접 사용)
try:
    load_dotenv()
except:
    pass  # .env 파일이 없어도 계속 진행

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['DAT_FOLDER'] = os.path.join('uploads', 'dat')
app.config['DATA_FOLDER'] = 'data'  # 데이터 저장용 폴더
app.config['SECRET_KEY'] = 'hanmi_taxi_secret_key'  # 실제 운영 환경에서는 환경 변수로 관리
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 데이터베이스 초기화
db.init_app(app)

# LoginManager 설정
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '이 페이지에 접근하려면 로그인이 필요합니다.'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# 모든 템플릿에서 current_user 사용 가능하도록 context processor 추가
@app.context_processor
def inject_user():
    return dict(current_user=current_user)


DISPATCH_HEADER_DISPLAY = {
    '차량번호': '차번',
    '차종': '차종',
    '근무유형': '근무',
    '사번': '사번',
    '운전기사': '운전기사',
    '인정일': '인정',
    '인정일수': '인정',
    '승무일': '승무',
    '승무일수': '승무',
    '근무일수': '승무',
    '결근일': '결근',
    '결근일수': '결근',
    '휴가': '휴가',
}


@app.template_filter('dispatch_header')
def dispatch_header_display(name):
    """배차 표 thead용 짧은 헤더 라벨."""
    return DISPATCH_HEADER_DISPLAY.get(str(name), str(name))


def format_work_minutes_label(minutes):
    """영업분 표시: 905분 (15시간05분)"""
    minutes = max(0, int(minutes or 0))
    hours, mins = divmod(minutes, 60)
    return f'{minutes}분 ({hours}시간{mins:02d}분)'


@app.template_filter('sales_minutes')
def sales_minutes_label(value):
    """분 단위 숫자를 수입금 표 시간 라벨로 표시."""
    try:
        minutes = int(value or 0)
    except (ValueError, TypeError):
        minutes = 0
    return format_work_minutes_label(minutes)


@app.template_filter('sales_work_minutes')
def sales_work_minutes_display(row):
    """수입금 행의 영업분 표시값. 저장 필드 없으면 영업시작·종료 시각으로 계산."""
    if not isinstance(row, dict):
        return format_work_minutes_label(0)
    minutes = row.get('영업시간')
    if minutes in (None, ''):
        minutes = row.get('영업분')
    if minutes in (None, ''):
        minutes = resolve_sales_work_minutes(row, reparse_dat=False)
    else:
        try:
            minutes = int(minutes)
        except (ValueError, TypeError):
            minutes = 0
    return format_work_minutes_label(minutes)


@app.template_filter('sales_emp_id')
def sales_emp_id_display(value):
    """수입금 표 사번 표시 (소수점 제거)."""
    return normalize_emp_id(value)


@app.template_filter('sales_modified')
def sales_modified_cell(row, field):
    """수입금 행에서 수동 수정된 필드인지 여부."""
    if not isinstance(row, dict):
        return False
    field_edits = row.get('_field_edits') or {}
    if isinstance(field_edits, dict) and field in field_edits:
        return True
    return field in (row.get('_modified_fields') or [])


def _sales_month_edit_history(month_data):
    """월별 수입금 수정 이력 (최대 2건, last_edit 호환)."""
    if not isinstance(month_data, dict):
        return []
    history = month_data.get('edit_history')
    if isinstance(history, list) and history:
        return history[:2]
    legacy = month_data.get('last_edit')
    if isinstance(legacy, dict) and (legacy.get('date') or legacy.get('editor')):
        return [legacy]
    return []


@app.template_filter('sales_modified_tier')
def sales_modified_tier(row, field, month_data):
    """수동 수정 필드의 강조 단계 — latest(최근) / previous(이전)."""
    if not isinstance(row, dict) or not sales_modified_cell(row, field):
        return ''
    field_edits = row.get('_field_edits') or {}
    meta = field_edits.get(field) if isinstance(field_edits, dict) else None
    history = _sales_month_edit_history(month_data)
    if not isinstance(meta, dict):
        return 'previous' if history else 'latest'
    session_id = str(meta.get('session_id') or '').strip()
    if not session_id:
        return 'previous' if len(history) > 1 else 'latest'
    latest_id = str((history[0] or {}).get('id') or '').strip() if history else ''
    prev_id = str((history[1] or {}).get('id') or '').strip() if len(history) > 1 else ''
    if session_id and latest_id and session_id == latest_id:
        return 'latest'
    if session_id and prev_id and session_id == prev_id:
        return 'previous'
    if session_id and latest_id:
        return 'previous'
    return 'latest'


def _format_sales_edit_tooltip(meta):
    """편집자·일시 툴팁 문자열."""
    if not isinstance(meta, dict):
        return ''
    editor = str(meta.get('editor') or '').strip()
    date = str(meta.get('date') or '').strip()
    time = str(meta.get('time') or '').strip()
    if not editor and not date:
        return ''
    parts = []
    if editor:
        parts.append('편집자: ' + editor)
    if date or time:
        parts.append((date + ' ' + time).strip())
    return ' | '.join(parts)


@app.template_filter('sales_field_edit_tooltip')
def sales_field_edit_tooltip(row, field, month_data=None):
    """수동 수정 셀 툴팁 — 편집자·일시."""
    if not isinstance(row, dict) or not sales_modified_cell(row, field):
        return ''
    field_edits = row.get('_field_edits') or {}
    meta = field_edits.get(field) if isinstance(field_edits, dict) else None
    tooltip = _format_sales_edit_tooltip(meta)
    if tooltip:
        return tooltip
    history = _sales_month_edit_history(month_data)
    if not history:
        return ''
    if isinstance(meta, dict):
        session_id = str(meta.get('session_id') or '').strip()
        if session_id:
            for edit in history:
                if str((edit or {}).get('id') or '').strip() == session_id:
                    matched = _format_sales_edit_tooltip(edit)
                    if matched:
                        return matched
    tier = sales_modified_tier(row, field, month_data) if month_data else 'latest'
    edit = history[1] if tier == 'previous' and len(history) > 1 else history[0]
    return _format_sales_edit_tooltip(edit)


@app.template_filter('sales_edit_history_json')
def sales_edit_history_json(month_data):
    """월별 수정 이력 JSON (툴팁 초기화용)."""
    import json
    return json.dumps(_sales_month_edit_history(month_data), ensure_ascii=False)


@app.template_filter('sales_edit_history_items')
def sales_edit_history_items(month_data):
    """월별 수입금 수정 이력 라벨 목록 (최대 2건)."""
    items = []
    for edit in _sales_month_edit_history(month_data):
        if not isinstance(edit, dict):
            continue
        date = str(edit.get('date') or '').strip()
        time = str(edit.get('time') or '').strip()
        editor = str(edit.get('editor') or '').strip()
        if date or editor:
            items.append(f'수정 일시: {date} | {time} | 편집자: {editor}')
    return items


@app.template_filter('sales_last_edit_label')
def sales_last_edit_label(month_data):
    """월별 수입금 표 마지막 수동 수정 기록 라벨."""
    items = sales_edit_history_items(month_data)
    return items[0] if items else ''


# 로그인 라우트
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('calculate_salary'))
        
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            flash('로그인되었습니다.', 'success')
            return redirect(url_for('calculate_salary'))
        else:
            return render_template('login.html', error='아이디 또는 비밀번호가 올바르지 않습니다.')
            
    return render_template('login.html')

# 회원가입 라우트
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('calculate_salary'))
        
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        name = request.form.get('name')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        phone = request.form.get('phone')
        department = request.form.get('department')
        position = request.form.get('position')
        
        if password != confirm_password:
            return render_template('register.html', error='비밀번호가 일치하지 않습니다.', departments=USER_DEPARTMENTS)
            
        if User.query.filter_by(username=username).first():
            return render_template('register.html', error='이미 존재하는 아이디입니다.', departments=USER_DEPARTMENTS)
            
        if User.query.filter_by(email=email).first():
            return render_template('register.html', error='이미 존재하는 이메일입니다.', departments=USER_DEPARTMENTS)

        if department not in USER_DEPARTMENTS:
            return render_template('register.html', error='소속을 선택해주세요.', departments=USER_DEPARTMENTS)
            
        user = User(username=username, email=email, name=name, phone=phone, department=department, position=position)
        user.set_password(password)
        
        db.session.add(user)
        db.session.commit()
        
        flash('회원가입이 완료되었습니다. 로그인해주세요.', 'success')
        return redirect(url_for('login'))
        
    return render_template('register.html', departments=USER_DEPARTMENTS)

# 로그아웃 라우트
@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('로그아웃되었습니다.', 'info')
    return redirect(url_for('login'))

DASHBOARD_MONTH_ORDER = [
    '01월', '02월', '03월', '04월', '05월', '06월',
    '07월', '08월', '09월', '10월', '11월', '12월',
]
DASHBOARD_DISPATCH_CATEGORIES = ['주간', '야간', '일차', '교대', '리스']


def _dashboard_available_years():
    """대시보드 연도 필터 — 배차·사고·수입금 데이터에서 사용 가능한 연도."""
    years = set()
    for store in (load_dispatch_data_store(), load_accident_data_store()):
        for key in (store or {}).keys():
            if str(key).isdigit() and len(str(key)) == 4:
                years.add(int(key))
    sales_data = _read_sales_data_raw()
    if sales_data:
        for month_data in sales_data.values():
            for row in month_data.get('data', []):
                date = str(row.get('날짜', '')).strip()
                if len(date) >= 4 and date[:4].isdigit():
                    years.add(int(date[:4]))
    if not years:
        years.add(datetime.now().year)
    return sorted(years, reverse=True)


def _resolve_dashboard_year():
    years = _dashboard_available_years()
    selected = request.args.get('year', type=int)
    if not selected or selected not in years:
        selected = _default_dispatch_year([str(y) for y in years])
    return years, selected


def _parse_dashboard_amount(amount_str):
    if not amount_str or amount_str in ('', '-'):
        return 0
    try:
        return int(str(amount_str).replace(',', ''))
    except (ValueError, TypeError):
        return 0


def _build_dashboard_sales_monthly(year, month_order=None):
    """수입금 JSON 행 날짜 기준 — 선택 연도의 월별 실입금·연료비."""
    month_order = month_order or DASHBOARD_MONTH_ORDER
    monthly_incomes = {month: 0 for month in month_order}
    monthly_fuel_costs = {month: 0 for month in month_order}
    sales_data = _read_sales_data_raw()
    if not sales_data:
        return monthly_incomes, monthly_fuel_costs
    year_prefix = str(year)
    for month_data in sales_data.values():
        for row in month_data.get('data', []):
            date = str(row.get('날짜', '')).strip()
            if not date.startswith(year_prefix):
                continue
            month_key = f'{date[5:7]}월' if len(date) >= 7 else ''
            if month_key not in monthly_incomes:
                continue
            try:
                monthly_incomes[month_key] += int(row.get('실입금') or 0)
                monthly_fuel_costs[month_key] += int(row.get('연료비') or 0)
            except (ValueError, TypeError):
                continue
    return monthly_incomes, monthly_fuel_costs


def _build_dashboard_dispatch_metrics(dispatch_data, month_order=None):
    month_order = month_order or DASHBOARD_MONTH_ORDER
    categories = DASHBOARD_DISPATCH_CATEGORIES
    dispatch_stats = {}
    driver_counts = {}
    driver_counts_by_category = {}
    for month in month_order:
        cat_counts = {cat: 0 for cat in categories}
        drivers = set()
        cat_drivers = {cat: set() for cat in categories}
        month_rows = (dispatch_data or {}).get(month, {}).get('data', [])
        for row in month_rows:
            cat = row.get('근무유형', '')
            name = str(row.get('운전기사', '')).strip()
            if cat in categories:
                for day in range(1, 32):
                    val = str(row.get(str(day), '')).strip()
                    if _dispatch_val_matches(val, 'o') or val == '/' or _dispatch_val_matches(val, 'H'):
                        cat_counts[cat] += 1
                if name:
                    cat_drivers[cat].add(name)
            if name:
                drivers.add(name)
        dispatch_stats[month] = cat_counts
        driver_counts[month] = len(drivers)
        driver_counts_by_category[month] = {cat: len(cat_drivers[cat]) for cat in categories}
    return dispatch_stats, driver_counts, driver_counts_by_category


def _build_dashboard_accident_summary(year, month_order=None):
    month_order = month_order or DASHBOARD_MONTH_ORDER
    accident_data = load_accident_data(year) or {}
    at_fault = accident_data.get('at_fault', [])
    not_at_fault = accident_data.get('not_at_fault', [])
    total_at_fault = len(at_fault)
    total_not_at_fault = len(not_at_fault)
    total_at_fault_repair = sum(_parse_dashboard_amount(a.get('수리지급', 0)) for a in at_fault)
    total_not_at_fault_payment = sum(_parse_dashboard_amount(a.get('금액', 0)) for a in not_at_fault)
    unresolved_at_fault = sum(1 for a in at_fault if str(a.get('처리여부', '')).strip() == '미결')
    unresolved_not_at_fault = sum(1 for a in not_at_fault if str(a.get('처리여부', '')).strip() == '미결')
    unpaid_at_fault_estimate = sum(_parse_dashboard_amount(a.get('견적', 0)) for a in at_fault)
    unpaid_not_at_fault_estimate = sum(_parse_dashboard_amount(a.get('피해견적', 0)) for a in not_at_fault)
    accident_stats_by_month = build_accident_stats_by_month(at_fault, not_at_fault, month_order)
    return {
        'total_at_fault': total_at_fault,
        'total_not_at_fault': total_not_at_fault,
        'total_at_fault_repair': total_at_fault_repair,
        'total_not_at_fault_payment': total_not_at_fault_payment,
        'unresolved_at_fault': unresolved_at_fault,
        'unresolved_not_at_fault': unresolved_not_at_fault,
        'unpaid_at_fault_estimate': unpaid_at_fault_estimate,
        'unpaid_not_at_fault_estimate': unpaid_not_at_fault_estimate,
        'accident_stats_by_month': accident_stats_by_month,
    }


def _build_dashboard_payload_for_year(year, month_order=None):
    """대시보드 카드·차트용 연도별 데이터 묶음."""
    month_order = month_order or DASHBOARD_MONTH_ORDER
    monthly_incomes, monthly_fuel_costs = _build_dashboard_sales_monthly(year, month_order)
    dispatch_data = load_dispatch_data(year)
    dispatch_stats, driver_counts, driver_counts_by_category = _build_dashboard_dispatch_metrics(
        dispatch_data, month_order,
    )
    accident_summary = _build_dashboard_accident_summary(year, month_order)
    return {
        'monthly_incomes': monthly_incomes,
        'monthly_fuel_costs': monthly_fuel_costs,
        'dispatch_stats': dispatch_stats,
        'driver_counts': driver_counts,
        'driver_counts_by_category': driver_counts_by_category,
        'accident_summary': accident_summary,
    }


def _build_dashboard_payload_by_year(years, month_order=None):
    return {str(year): _build_dashboard_payload_for_year(year, month_order) for year in years}


@app.route('/api/dashboard/year/<int:year>')
@login_required
def api_dashboard_year(year):
    """대시보드 카드·차트용 연도별 데이터 API."""
    years = _dashboard_available_years()
    if year not in years:
        return jsonify({'success': False, 'error': '연도를 찾을 수 없습니다.'}), 404
    return jsonify({
        'success': True,
        'year': year,
        'payload': _build_dashboard_payload_for_year(year),
    })


def _build_dashboard_income_card_metrics(monthly_incomes, monthly_fuel_costs, selected_month, month_order=None):
    month_order = month_order or DASHBOARD_MONTH_ORDER
    total_income = sum(monthly_incomes.values())
    total_fuel_cost = sum(monthly_fuel_costs.values())
    active_months = [m for m in month_order if monthly_incomes.get(m, 0) > 0 or monthly_fuel_costs.get(m, 0) > 0]
    month_count = len(active_months) or len(month_order)
    monthly_avg_income = total_income // month_count if month_count else 0
    current_month_income = monthly_incomes.get(selected_month, 0)
    current_month_fuel_cost = monthly_fuel_costs.get(selected_month, 0)
    try:
        current_index = month_order.index(selected_month)
        previous_month = month_order[current_index - 1] if current_index > 0 else month_order[-1]
        previous_month_income = monthly_incomes.get(previous_month, 0)
        previous_month_fuel_cost = monthly_fuel_costs.get(previous_month, 0)
        income_diff = current_month_income - previous_month_income
        income_diff_percent = round((income_diff / previous_month_income) * 100, 2) if previous_month_income > 0 else 0
        fuel_diff = current_month_fuel_cost - previous_month_fuel_cost
        fuel_diff_percent = round((fuel_diff / previous_month_fuel_cost) * 100, 2) if previous_month_fuel_cost > 0 else 0
    except ValueError:
        income_diff = income_diff_percent = fuel_diff = fuel_diff_percent = 0
    income_change = income_percent = 0
    if len(month_order) >= 2:
        last_month = month_order[-1]
        prev_month = month_order[-2]
        last_income = monthly_incomes.get(last_month, 0)
        prev_income = monthly_incomes.get(prev_month, 0)
        if prev_income > 0:
            income_change = last_income - prev_income
            income_percent = round((income_change / prev_income) * 100, 2)
    return {
        'total_income': total_income,
        'total_fuel_cost': total_fuel_cost,
        'monthly_avg_income': monthly_avg_income,
        'current_month_income': current_month_income,
        'current_month_fuel_cost': current_month_fuel_cost,
        'income_diff': income_diff,
        'income_diff_percent': income_diff_percent,
        'fuel_diff': fuel_diff,
        'fuel_diff_percent': fuel_diff_percent,
        'income_change': income_change,
        'income_percent': income_percent,
    }


# 기존 라우트들에 @login_required 데코레이터 추가
@app.route('/', methods=['GET', 'POST'])
@login_required
def calculate_salary():
    month_order = DASHBOARD_MONTH_ORDER
    dashboard_years, selected_year = _resolve_dashboard_year()
    dashboard_payload_by_year = _build_dashboard_payload_by_year(dashboard_years, month_order)

    monthly_incomes, monthly_fuel_costs = _build_dashboard_sales_monthly(selected_year, month_order)
    income_metrics = _build_dashboard_income_card_metrics(
        monthly_incomes, monthly_fuel_costs,
        request.args.get('month', f'{datetime.now().month:02d}월'),
        month_order,
    )
    total_income = income_metrics['total_income']
    total_fuel_cost = income_metrics['total_fuel_cost']
    monthly_avg_income = income_metrics['monthly_avg_income']
    selected_month = request.args.get('month', f'{datetime.now().month:02d}월')
    current_month_income = income_metrics['current_month_income']
    current_month_fuel_cost = income_metrics['current_month_fuel_cost']
    income_diff = income_metrics['income_diff']
    income_diff_percent = income_metrics['income_diff_percent']
    fuel_diff = income_metrics['fuel_diff']
    fuel_diff_percent = income_metrics['fuel_diff_percent']
    income_change = income_metrics['income_change']
    income_percent = income_metrics['income_percent']

    dispatch_data = load_dispatch_data(selected_year)
    dispatch_stats, driver_counts, driver_counts_by_category = _build_dashboard_dispatch_metrics(
        dispatch_data, month_order,
    )
    accident_summary = _build_dashboard_accident_summary(selected_year, month_order)
    total_at_fault = accident_summary['total_at_fault']
    total_not_at_fault = accident_summary['total_not_at_fault']
    total_at_fault_repair = accident_summary['total_at_fault_repair']
    total_not_at_fault_payment = accident_summary['total_not_at_fault_payment']
    unresolved_at_fault = accident_summary['unresolved_at_fault']
    unresolved_not_at_fault = accident_summary['unresolved_not_at_fault']
    unpaid_at_fault_estimate = accident_summary['unpaid_at_fault_estimate']
    unpaid_not_at_fault_estimate = accident_summary['unpaid_not_at_fault_estimate']
    accident_stats_by_month = accident_summary['accident_stats_by_month']

    if request.method == 'POST':
        if 'excel_file' in request.files:
            file = request.files['excel_file']
            if file.filename != '':
                session.pop('salary_data', None)
                session.pop('salary_calculated', None)
                
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
                file.save(filepath)
                
                try:
                    sheet_names = ['01월', '02월', '03월', '04월', '05월', '06월', '07월', '08월', '09월', '10월', '11월', '12월']
                    salary_data = OrderedDict()
                    
                    for sheet in sheet_names:
                        try:
                            df = pd.read_excel(filepath, sheet_name=sheet)
                            required_columns = ['실입금', '리스료', '연료비']
                            if not all(col in df.columns for col in required_columns):
                                continue
                            
                            df['급여'] = (df['실입금'] - df['리스료'] - df['연료비']) * 0.8
                            
                            # 사번, 이름, 차종 컬럼이 있는 경우 포함, 없는 경우 빈 문자열로 처리
                            additional_columns = ['사번', '이름', '차종']
                            for col in additional_columns:
                                if col not in df.columns:
                                    df[col] = ''
                            
                            # 데이터 저장 시 추가 컬럼 포함
                            columns_to_save = ['사번', '이름', '차종', '실입금', '리스료', '연료비', '급여']
                            numeric_data = df[columns_to_save].fillna('').astype(str).to_dict('records')
                            
                            salary_data[sheet] = {
                                'data': numeric_data,
                                'summary': {
                                    'total_count': len(df),
                                    'avg_salary': int(df['급여'].mean()),
                                    'max_salary': int(df['급여'].max()),
                                    'min_salary': int(df['급여'].min())
                                }
                            }
                        except:
                            continue
                    
                    if not salary_data:
                        empty_accident_stats = {m: {'at_fault': 0, 'at_fault_causes': {}, 'not_at_fault': 0, 'not_at_fault_causes': {}} for m in month_order}
                        return render_template('index.html', 
                                            error="엑셀 파일에 '실입금', '리스료', '연료비' 컬럼이 있는 시트가 없습니다.",
                                            total_income=total_income,
                                            monthly_avg_income=monthly_avg_income,
                                            selected_month=selected_month,
                                            current_month_income=current_month_income,
                                            current_month_fuel_cost=current_month_fuel_cost,
                                            income_diff=income_diff,
                                            income_diff_percent=income_diff_percent,
                                            fuel_diff=fuel_diff,
                                            fuel_diff_percent=fuel_diff_percent,
                                            income_change=income_change,
                                            income_percent=income_percent,
                                            dispatch_stats=dispatch_stats,
                                            month_order=month_order,
                                            driver_counts=driver_counts,
                                            monthly_incomes=monthly_incomes,
                                            monthly_fuel_costs=monthly_fuel_costs,
                                            accident_stats_by_month=empty_accident_stats,
                                            dashboard_years=dashboard_years,
                                            selected_year=selected_year,
                                            dashboard_payload_by_year=dashboard_payload_by_year,
                                            driver_counts_by_category=driver_counts_by_category)
                    
                    session['salary_data'] = salary_data
                    session['salary_calculated'] = True

                    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
                    return render_template('index.html', 
                                        salary_data=salary_data,
                                        calculated=True,
                                        messages=messages,
                                        current_user=current_user,
                                        total_income=total_income,
                                        monthly_avg_income=monthly_avg_income,
                                        selected_month=selected_month,
                                        current_month_income=current_month_income,
                                        current_month_fuel_cost=current_month_fuel_cost,
                                        income_diff=income_diff,
                                        income_diff_percent=income_diff_percent,
                                        fuel_diff=fuel_diff,
                                        fuel_diff_percent=fuel_diff_percent,
                                        income_change=income_change,
                                        income_percent=income_percent,
                                        total_at_fault=total_at_fault,
                                        total_not_at_fault=total_not_at_fault,
                                        total_at_fault_repair=total_at_fault_repair,
                                        total_not_at_fault_payment=total_not_at_fault_payment,
                                        unresolved_at_fault=unresolved_at_fault,
                                        unresolved_not_at_fault=unresolved_not_at_fault,
                                        unpaid_at_fault_estimate=unpaid_at_fault_estimate,
                                        unpaid_not_at_fault_estimate=unpaid_not_at_fault_estimate,
                                        dispatch_stats=dispatch_stats,
                                        month_order=month_order,
                                        driver_counts=driver_counts,
                                        monthly_incomes=monthly_incomes,
                                        monthly_fuel_costs=monthly_fuel_costs,
                                        accident_stats_by_month=accident_stats_by_month,
                                        dashboard_years=dashboard_years,
                                        selected_year=selected_year,
                                        dashboard_payload_by_year=dashboard_payload_by_year,
                                        driver_counts_by_category=driver_counts_by_category)
                except Exception as e:
                    empty_accident_stats = {m: {'at_fault': 0, 'at_fault_causes': {}, 'not_at_fault': 0, 'not_at_fault_causes': {}} for m in month_order}
                    return render_template('index.html',
                                        error=f"엑셀 파일 처리 중 오류가 발생했습니다: {str(e)}",
                                        total_income=total_income,
                                        monthly_avg_income=monthly_avg_income,
                                        selected_month=selected_month,
                                        current_month_income=current_month_income,
                                        current_month_fuel_cost=current_month_fuel_cost,
                                        income_diff=income_diff,
                                        income_diff_percent=income_diff_percent,
                                        fuel_diff=fuel_diff,
                                        fuel_diff_percent=fuel_diff_percent,
                                        income_change=income_change,
                                        income_percent=income_percent,
                                        dispatch_stats=dispatch_stats,
                                        month_order=month_order,
                                        driver_counts=driver_counts,
                                        monthly_incomes=monthly_incomes,
                                        monthly_fuel_costs=monthly_fuel_costs,
                                        accident_stats_by_month=empty_accident_stats,
                                        dashboard_years=dashboard_years,
                                        selected_year=selected_year,
                                        dashboard_payload_by_year=dashboard_payload_by_year,
                                        driver_counts_by_category=driver_counts_by_category)
    
    # GET 요청이거나 세션에 저장된 데이터가 있는 경우
    salary_data = session.get('salary_data', None)
    calculated = session.get('salary_calculated', False)

    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    return render_template('index.html',
                        salary_data=salary_data,
                        calculated=calculated,
                        messages=messages,
                        current_user=current_user,
                        total_income=total_income,
                        monthly_avg_income=monthly_avg_income,
                        selected_month=selected_month,
                        current_month_income=current_month_income,
                        current_month_fuel_cost=current_month_fuel_cost,
                        income_diff=income_diff,
                        income_diff_percent=income_diff_percent,
                        fuel_diff=fuel_diff,
                        fuel_diff_percent=fuel_diff_percent,
                        income_change=income_change,
                        income_percent=income_percent,
                        total_at_fault=total_at_fault,
                        total_not_at_fault=total_not_at_fault,
                        total_at_fault_repair=total_at_fault_repair,
                        total_not_at_fault_payment=total_not_at_fault_payment,
                        unresolved_at_fault=unresolved_at_fault,
                        unresolved_not_at_fault=unresolved_not_at_fault,
                        unpaid_at_fault_estimate=unpaid_at_fault_estimate,
                        unpaid_not_at_fault_estimate=unpaid_not_at_fault_estimate,
                        dispatch_stats=dispatch_stats,
                        month_order=month_order,
                        driver_counts=driver_counts,
                        driver_counts_by_category=driver_counts_by_category,
                        monthly_incomes=monthly_incomes,
                        monthly_fuel_costs=monthly_fuel_costs,
                        accident_stats_by_month=accident_stats_by_month,
                        dashboard_years=dashboard_years,
                        selected_year=selected_year,
                        dashboard_payload_by_year=dashboard_payload_by_year)

@app.route('/schedule', methods=['GET', 'POST'])
@login_required
def schedule():
    print("=== /schedule 라우트 호출됨 ===")
    print(f"요청 메서드: {request.method}")
    print(f"현재 사용자: {current_user.username if current_user else 'None'}")
    if request.method == 'POST':
        print("POST 요청 받음")
        if 'excel_file' in request.files:
            file = request.files['excel_file']
            print(f"파일명: {file.filename}")
            if file.filename != '':
                filename = file.filename.replace('/', '').replace('\\', '')
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                print(f"저장 경로: {filepath}")
                file.save(filepath)
                print(f"파일 저장 완료: {filepath}")
                
                try:
                    view_year = request.form.get('view_year', type=int)
                    dispatch_year = extract_dispatch_year_from_filename(filename)
                    dispatch_data = parse_dispatch_excel(filepath)
                    if not dispatch_data:
                        return render_template(
                            'schedule.html',
                            **_schedule_page_context(
                                selected_year=view_year,
                                error="엑셀 파일에서 읽을 수 있는 시트가 없습니다.",
                            ),
                        )

                    save_dispatch_data(dispatch_data, year=dispatch_year)
                    remarks_by_date, remarks_year = parse_dispatch_remarks_excel(filepath)
                    save_dispatch_remarks(remarks_by_date, remarks_year or dispatch_year)

                    flask_url = url_for('uploaded_file', filename=os.path.basename(filepath), _external=True)
                    record = UploadRecord(filename=filename, uploader=current_user.name, github_url=flask_url, upload_type='schedule')
                    db.session.add(record)
                    db.session.commit()

                    if view_year:
                        return redirect(url_for('schedule', year=view_year))
                    return redirect(url_for('schedule'))
                except Exception as e:
                    view_year = request.form.get('view_year', type=int)
                    return render_template(
                        'schedule.html',
                        **_schedule_page_context(
                            selected_year=view_year,
                            error=f"엑셀 파일 처리 중 오류가 발생했습니다: {str(e)}",
                        ),
                    )

    return render_template('schedule.html', **_schedule_page_context())

@app.route('/pay_lease', methods=['GET', 'POST'])
@login_required
def pay_lease():
    print("=== /pay_lease 라우트 호출됨 ===")
    print(f"요청 메서드: {request.method}")
    print(f"현재 사용자: {current_user.username if current_user else 'None'}")
    if request.method == 'POST':
        print("POST 요청 받음")
        if 'excel_file' in request.files:
            file = request.files['excel_file']
            print(f"파일명: {file.filename}")
            if file.filename != '':
                filename = file.filename.replace('/', '').replace('\\', '')
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                print(f"저장 경로: {filepath}")
                file.save(filepath)
                print(f"파일 저장 완료: {filepath}")
                
                try:
                    sheet_names = ['01월', '02월', '03월', '04월', '05월', '06월', '07월', '08월', '09월', '10월', '11월', '12월']
                    salary_data = OrderedDict()
                    
                    for sheet in sheet_names:
                        try:
                            df = pd.read_excel(filepath, sheet_name=sheet)
                            required_columns = ['실입금', '리스료', '연료비']
                            if not all(col in df.columns for col in required_columns):
                                continue
                            
                            df['급여'] = (df['실입금'] - df['리스료'] - df['연료비']) * 0.8
                            
                            # 사번, 이름, 차종 컬럼이 있는 경우 포함, 없는 경우 빈 문자열로 처리
                            additional_columns = ['사번', '이름', '차종']
                            for col in additional_columns:
                                if col not in df.columns:
                                    df[col] = ''
                            
                            # 데이터 저장 시 추가 컬럼 포함
                            columns_to_save = ['사번', '이름', '차종', '실입금', '리스료', '연료비', '급여']
                            numeric_data = df[columns_to_save].fillna('').astype(str).to_dict('records')
                            
                            salary_data[sheet] = {
                                'data': numeric_data,
                                'summary': {
                                    'total_count': len(df),
                                    'avg_salary': int(df['급여'].mean()),
                                    'max_salary': int(df['급여'].max()),
                                    'min_salary': int(df['급여'].min())
                                }
                            }
                        except:
                            continue
                    
                    if not salary_data:
                        return render_template('pay_lease.html', 
                                            error="엑셀 파일에 '실입금', '리스료', '연료비' 컬럼이 있는 시트가 없습니다.")
                    
                    # 파일로 저장
                    save_lease_data(salary_data)
                    
                    # UploadRecord 데이터베이스 저장
                    flask_url = url_for('uploaded_file', filename=os.path.basename(filepath), _external=True)
                    record = UploadRecord(filename=filename, uploader=current_user.name, github_url=flask_url, upload_type='pay_lease')
                    db.session.add(record)
                    db.session.commit()
                    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
                    return render_template('pay_lease.html', salary_data=salary_data, messages=messages, current_user=current_user)
                except Exception as e:
                    return render_template('pay_lease.html', 
                                        error=f"엑셀 파일 처리 중 오류가 발생했습니다: {str(e)}")
    
    # GET 요청이거나 저장된 데이터가 있는 경우
    salary_data = load_lease_data()
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    return render_template('pay_lease.html', salary_data=salary_data, messages=messages, current_user=current_user)

@app.route('/sales', methods=['GET', 'POST'])
@login_required
def sales():
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    upload_summary = None

    if request.method == 'POST':
        files = request.files.getlist('dat_files')
        fuel_price = float(request.form.get('fuel_price', DEFAULT_FUEL_PRICE) or DEFAULT_FUEL_PRICE)
        valid_files = [f for f in files if f and f.filename]

        if not valid_files:
            return render_template(
                'sales.html',
                sales_data=load_sales_data(),
                error='.dat 파일을 선택해 주세요.',
                fuel_price=fuel_price,
                messages=messages,
                current_user=current_user,
                upload_summary=upload_summary,
            )

        try:
            merged, saved_names, err = process_dat_upload(valid_files, fuel_price=fuel_price)
            if err:
                return render_template(
                    'sales.html',
                    sales_data=load_sales_data(),
                    error=err,
                    fuel_price=fuel_price,
                    messages=messages,
                    current_user=current_user,
                    upload_summary=upload_summary,
                )

            batch_label = saved_names[0] if len(saved_names) == 1 else f'{len(saved_names)}개 dat 파일'
            flask_url = url_for(
                'uploaded_dat_file',
                filename=os.path.basename(saved_names[0]),
                _external=True,
            ) if saved_names else ''
            record = UploadRecord(
                filename=batch_label,
                uploader=current_user.name,
                github_url=flask_url,
                upload_type='sales',
            )
            db.session.add(record)
            db.session.commit()

            matched = sum(1 for m in merged.values() for r in m.get('data', []) if r.get('매칭') == '완료')
            total_rows = sum(len(m.get('data', [])) for m in merged.values())
            upload_summary = {
                'file_count': len(saved_names),
                'matched': matched,
                'total': total_rows,
            }
            return render_template(
                'sales.html',
                sales_data=normalize_sales_data(merged),
                fuel_price=fuel_price,
                messages=messages,
                current_user=current_user,
                upload_summary=upload_summary,
            )
        except Exception as e:
            return render_template(
                'sales.html',
                sales_data=load_sales_data(),
                error=f'.dat 파일 처리 중 오류: {str(e)}',
                fuel_price=fuel_price,
                messages=messages,
                current_user=current_user,
                upload_summary=upload_summary,
            )

    sales_data = load_sales_data()
    return render_template(
        'sales.html',
        sales_data=sales_data,
        fuel_price=DEFAULT_FUEL_PRICE,
        messages=messages,
        current_user=current_user,
        upload_summary=upload_summary,
    )


@app.route('/sales/upload', methods=['POST'])
@login_required
def sales_upload_api():
    """dat 파일 업로드 — JSON 응답 (소량·레거시)."""
    files = request.files.getlist('dat_files')
    fuel_price = float(request.form.get('fuel_price', DEFAULT_FUEL_PRICE) or DEFAULT_FUEL_PRICE)
    valid_files = [f for f in files if f and f.filename]

    if not valid_files:
        return jsonify({'success': False, 'error': '.dat 파일을 선택해 주세요.'}), 400

    try:
        _merged, saved_names, err = process_dat_upload(valid_files, fuel_price=fuel_price)
        if err:
            return jsonify({'success': False, 'error': err}), 400
        return jsonify({
            'success': True,
            'saved': saved_names,
            'saved_count': len(saved_names),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': f'.dat 파일 처리 중 오류: {str(e)}'}), 500


@app.route('/sales/upload/batch', methods=['POST'])
@login_required
def sales_upload_batch():
    """dat 일괄 업로드 + 선택적 T머니 CSV 대조 — NDJSON 스트림으로 진행률 전송."""
    files = request.files.getlist('dat_files')
    csv_file = request.files.get('tmoney_csv')
    fuel_price = float(request.form.get('fuel_price', DEFAULT_FUEL_PRICE) or DEFAULT_FUEL_PRICE)
    valid_files = [
        f for f in files
        if f and f.filename and allowed_dat_file(f.filename)
    ]
    csv_filename = ''
    csv_raw = b''
    if csv_file and csv_file.filename:
        if not allowed_tmoney_csv_file(csv_file.filename):
            return jsonify({'success': False, 'error': 'T머니 CSV는 .csv 파일만 업로드할 수 있습니다.'}), 400
        csv_filename = secure_filename(csv_file.filename.replace('/', '').replace('\\', ''))
        csv_raw = csv_file.read()

    if not valid_files and not csv_raw:
        return jsonify({
            'success': False,
            'error': '미터기 .dat 파일 또는 T머니 CSV 중 하나 이상을 선택해 주세요.',
        }), 400

    @stream_with_context
    def generate():
        try:
            from sales_reconcile import reconcile_sales_with_csv_bytes

            existing = _read_sales_data_raw() or OrderedDict()
            lookup_cache = {}
            parsed_list = []
            saved_names = []
            total = len(valid_files)

            for index, file in enumerate(valid_files, start=1):
                filename = secure_filename(file.filename.replace('/', '').replace('\\', ''))
                filepath = os.path.join(app.config['DAT_FOLDER'], filename)
                raw = file.read()
                with open(filepath, 'wb') as out:
                    out.write(raw)

                parsed = parse_dat_bytes(raw, filename)
                parsed_list.append(parsed)
                _purge_sales_rows_by_source(existing, filename)
                saved_names.append(filename)
                yield json.dumps({'completed': index, 'total': total}, ensure_ascii=False) + '\n'

            merged = existing
            if parsed_list:
                new_rows = build_dat_upload_sales_rows(
                    parsed_list, fuel_price=fuel_price, lookup_cache=lookup_cache,
                )
                merged = merge_sales_records(existing, new_rows)

            csv_reconcile_report = None
            if csv_raw:
                merged, csv_reconcile_report = reconcile_sales_with_csv_bytes(
                    merged, csv_raw, filename=csv_filename,
                )

            save_sales_data(merged, normalize=True)
            if saved_names:
                _remove_dat_files(saved_names)
            if csv_raw and csv_filename:
                _remove_tmoney_csv_file(csv_filename)

            parts = []
            if saved_names:
                parts.append(f'{len(saved_names)}개 dat')
            if csv_raw:
                parts.append('T머니 CSV')
            batch_label = ' + '.join(parts) if parts else '수입금 데이터'
            first_filename = os.path.basename(saved_names[0]) if saved_names else csv_filename
            flask_url = url_for(
                'uploaded_dat_file',
                filename=first_filename,
                _external=True,
            ) if first_filename and saved_names else ''
            record = UploadRecord(
                filename=batch_label,
                uploader=current_user.name,
                github_url=flask_url,
                upload_type='sales',
            )
            db.session.add(record)
            db.session.commit()

            yield json.dumps({
                'done': True,
                'saved_count': len(saved_names),
                'csv_reconciled': bool(csv_raw),
                'csv_filename': csv_filename,
                'first_filename': first_filename,
                'reconcile_report': csv_reconcile_report,
            }, ensure_ascii=False) + '\n'
        except Exception as e:
            db.session.rollback()
            yield json.dumps({'error': f'수입금 데이터 처리 중 오류: {str(e)}'}, ensure_ascii=False) + '\n'

    return Response(
        generate(),
        mimetype='application/x-ndjson',
        headers={
            'X-Accel-Buffering': 'no',
            'Cache-Control': 'no-cache',
        },
    )


@app.route('/sales/upload/complete', methods=['POST'])
@login_required
def sales_upload_complete():
    """일괄 업로드 완료 후 업로드 이력 기록."""
    payload = request.get_json(silent=True) or {}
    try:
        file_count = max(0, int(payload.get('file_count') or 0))
    except (TypeError, ValueError):
        file_count = 0
    first_filename = os.path.basename(str(payload.get('first_filename') or '').strip())

    if file_count <= 0:
        return jsonify({'success': False, 'error': '업로드 파일 수가 없습니다.'}), 400

    batch_label = first_filename if file_count == 1 else f'{file_count}개 dat 파일'
    flask_url = url_for(
        'uploaded_dat_file',
        filename=first_filename,
        _external=True,
    ) if first_filename else ''
    record = UploadRecord(
        filename=batch_label,
        uploader=current_user.name,
        github_url=flask_url,
        upload_type='sales',
    )
    db.session.add(record)
    db.session.commit()
    return jsonify({'success': True, 'file_count': file_count})


SALES_EXPORT_COLUMNS = (
    '날짜', '차번', '차종', '근무유형', '사번', '운전기사', '실입금', '건수',
    '출고일시', '입고일시', '영업시간', '빈차시간', '총시간', '연료비', '충전량',
    '운행거리', '빈차거리', '총거리', '매칭',
)


def _sales_export_minutes_value(row, field, fallback_work=False):
    value = row.get(field)
    if field == '영업시간' and value in (None, ''):
        value = row.get('영업분')
    if value not in (None, ''):
        try:
            return format_work_minutes_label(int(value))
        except (ValueError, TypeError):
            pass
    if fallback_work:
        try:
            return format_work_minutes_label(resolve_sales_work_minutes(row, reparse_dat=False))
        except Exception:
            pass
    return format_work_minutes_label(0)


def _sales_row_for_export(row: dict) -> dict:
    """수입 내역 표와 동일한 컬럼·표시 형식으로 변환."""
    try:
        income = int(float(row.get('실입금') or 0))
    except (ValueError, TypeError):
        income = 0
    try:
        trips = int(float(row.get('건수') or 0))
    except (ValueError, TypeError):
        trips = 0
    try:
        fuel_cost = int(float(row.get('연료비') or 0))
    except (ValueError, TypeError):
        fuel_cost = 0
    try:
        fuel_l = round(float(row.get('충전량') or row.get('연료L') or 0), 2)
    except (ValueError, TypeError):
        fuel_l = 0.0

    return {
        '날짜': str(row.get('날짜') or '').strip(),
        '차번': str(row.get('차번') or '').strip(),
        '차종': str(row.get('차종') or '').strip(),
        '근무유형': str(row.get('근무유형') or '').strip(),
        '사번': normalize_emp_id(row.get('사번', '')),
        '운전기사': str(row.get('이름') or '').strip(),
        '실입금': income,
        '건수': trips,
        '출고일시': str(row.get('영업시작') or row.get('마감시작') or '').strip(),
        '입고일시': str(row.get('영업종료') or row.get('마감종료') or '').strip(),
        '영업시간': _sales_export_minutes_value(row, '영업시간', fallback_work=True),
        '빈차시간': _sales_export_minutes_value(row, '빈차시간'),
        '총시간': _sales_export_minutes_value(row, '총시간'),
        '연료비': fuel_cost,
        '충전량': fuel_l,
        '운행거리': str(row.get('운행거리') or '').strip(),
        '빈차거리': str(row.get('빈차거리') or '0').strip(),
        '총거리': str(row.get('총거리') or '0').strip(),
        '매칭': str(row.get('매칭') or '').strip(),
    }


def _build_sales_export_filename(fmt: str, month: str = '') -> str:
    stamp = datetime.now().strftime('%Y%m%d')
    if month:
        safe_month = re.sub(r'[^\d월]', '', str(month))
        base = f'수입금관리_{safe_month}_{stamp}'
    else:
        base = f'수입금관리_{stamp}'
    return f'{base}.{"xlsx" if fmt == "xlsx" else "csv"}'


@app.route('/sales/export')
@login_required
def sales_export():
    """수입 내역 표를 CSV 또는 Excel로 다운로드."""
    fmt = (request.args.get('format') or 'xlsx').lower()
    if fmt not in ('csv', 'xlsx'):
        return jsonify({'success': False, 'error': 'format은 csv 또는 xlsx만 지원합니다.'}), 400

    month = (request.args.get('month') or '').strip()
    sales_data = load_sales_data()
    if not sales_data:
        return jsonify({'success': False, 'error': '다운로드할 수입금 데이터가 없습니다.'}), 404

    month_items = list(sales_data.items())
    if month:
        month_items = [(k, v) for k, v in month_items if k == month]
        if not month_items:
            return jsonify({'success': False, 'error': f'[{month}] 데이터가 없습니다.'}), 404

    if fmt == 'xlsx':
        buffer = BytesIO()
        wrote_sheet = False
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            for month_key, month_data in month_items:
                rows = [_sales_row_for_export(r) for r in month_data.get('data', [])]
                if not rows:
                    continue
                df = pd.DataFrame(rows, columns=list(SALES_EXPORT_COLUMNS))
                sheet_name = str(month_key)[:31] or '수입금'
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                wrote_sheet = True
        if not wrote_sheet:
            return jsonify({'success': False, 'error': '다운로드할 행이 없습니다.'}), 404
        buffer.seek(0)
        return send_file(
            buffer,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=_build_sales_export_filename('xlsx', month),
        )

    csv_rows = []
    for month_key, month_data in month_items:
        for row in month_data.get('data', []):
            exported = _sales_row_for_export(row)
            if len(month_items) > 1 or not month:
                exported = {'월': month_key, **exported}
            csv_rows.append(exported)
    if not csv_rows:
        return jsonify({'success': False, 'error': '다운로드할 행이 없습니다.'}), 404

    columns = (['월'] if (len(month_items) > 1 or not month) else []) + list(SALES_EXPORT_COLUMNS)
    df = pd.DataFrame(csv_rows, columns=columns)
    buffer = BytesIO()
    buffer.write('\ufeff'.encode('utf-8'))
    buffer.write(df.to_csv(index=False).encode('utf-8'))
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype='text/csv; charset=utf-8',
        as_attachment=True,
        download_name=_build_sales_export_filename('csv', month),
    )


@app.route('/sales/save', methods=['POST'])
@login_required
def sales_save():
    payload = request.get_json(silent=True) or {}
    updates = payload.get('updates') or []
    ok, result = apply_sales_row_updates(
        updates,
        editor_name=_sales_editor_name(),
    )
    if not ok:
        return jsonify({'success': False, 'error': result}), 400
    return jsonify({
        'success': True,
        'updated': result['updated'],
        'last_edits': result.get('last_edits', {}),
        'edit_histories': result.get('edit_histories', {}),
        'last_edit': next(iter(result.get('last_edits', {}).values()), None),
        'field_changes': result.get('field_changes', []),
    })


@app.route('/accident', methods=['GET', 'POST'])
@login_required
def accident():
    print("=== /accident 라우트 호출됨 ===")
    print(f"요청 메서드: {request.method}")
    print(f"현재 사용자: {current_user.username if current_user else 'None'}")
    if request.method == 'POST':
        print("POST 요청 받음")
        view_year = request.form.get('view_year', type=int)
        if 'excel_file' not in request.files:
            flash('파일이 선택되지 않았습니다.', 'error')
            return redirect(request.url)
        
        file = request.files['excel_file']
        print(f"파일명: {file.filename}")
        if file.filename == '':
            flash('파일이 선택되지 않았습니다.', 'error')
            return redirect(request.url)
        
        if file and allowed_file(file.filename):
            filename = file.filename.replace('/', '').replace('\\', '')
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            print(f"저장 경로: {file_path}")
            file.save(file_path)
            print(f"파일 저장 완료: {file_path}")
            
            try:
                # 엑셀 파일에서 시트 읽기
                at_fault_df = pd.read_excel(file_path, sheet_name='가해사고')
                not_at_fault_df = pd.read_excel(file_path, sheet_name='피해사고')
                
                # 컬럼명 공백 제거 및 정리
                at_fault_df.columns = [str(col).strip() for col in at_fault_df.columns]
                not_at_fault_df.columns = [str(col).strip() for col in not_at_fault_df.columns]

                # 데이터 클리닝 및 형식 변환
                def clean_and_format(df):
                    # NaN 값을 빈 문자열로 대체
                    df = df.fillna('')
                    
                    for col in df.columns:
                        # 사고관리 날짜/시간 컬럼별 포맷 지정
                        if col == '사고번호':
                            df[col] = df[col].apply(normalize_accident_no)
                        elif col == '사고일시':
                            try:
                                df[col] = pd.to_datetime(df[col], format='%m/%d %H:%M', errors='coerce').dt.strftime('%m/%d %H:%M').fillna('')
                            except:
                                df[col] = df[col].astype(str).str.strip()
                        elif col == '입금일':
                            try:
                                df[col] = pd.to_datetime(df[col], format='%m/%d', errors='coerce').dt.strftime('%m/%d').fillna('')
                            except:
                                df[col] = df[col].astype(str).str.strip()
                        # 기타 날짜/시간 컬럼(기존 방식 유지)
                        elif '일시' in col or '일' in col:
                            try:
                                df[col] = pd.to_datetime(df[col], errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S').fillna('')
                            except:
                                df[col] = df[col].astype(str).str.strip()
                        # 숫자형 컬럼은 문자열로 변환하여 형식 유지
                        else:
                            df[col] = df[col].astype(str).str.strip()
                    return df

                at_fault_df = clean_and_format(at_fault_df)
                not_at_fault_df = clean_and_format(not_at_fault_df)

                # JSON으로 변환 - 모든 컬럼 포함
                accident_data = {
                    'at_fault': at_fault_df.to_dict('records'),
                    'not_at_fault': not_at_fault_df.to_dict('records'),
                    'at_fault_columns': list(at_fault_df.columns),
                    'not_at_fault_columns': list(not_at_fault_df.columns)
                }
                
                # 파일로 저장 
                accident_year = extract_dispatch_year_from_filename(filename)
                save_accident_data(accident_data, year=accident_year)
                
                # 업로드 정보 저장
                kst = pytz.timezone('Asia/Seoul')
                session['last_accident_file'] = filename
                session['upload_time'] = pd.Timestamp.now(tz=kst).strftime('%Y-%m-%d %H:%M:%S')
                session['uploader_name'] = current_user.name if hasattr(current_user, 'name') else current_user.username
                
                flash(f'<{filename}> 파일이 성공적으로 업로드되었습니다. (업로드 일시: {session.get("upload_time")})', 'success')
                # UploadRecord 데이터베이스 저장
                flask_url = url_for('uploaded_file', filename=os.path.basename(file_path), _external=True)
                record = UploadRecord(filename=filename, uploader=current_user.name, github_url=flask_url, upload_type='accident')
                db.session.add(record)
                db.session.commit()

            except Exception as e:
                flash(f'파일 처리 중 오류 발생: {e}', 'error')
                
            if view_year:
                return redirect(url_for('accident', year=view_year))
            return redirect(url_for('accident'))

    page_ctx = _accident_page_context()
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    upload_info = {
        'filename': session.get('last_accident_file'),
        'upload_time': session.get('upload_time'),
        'uploader_name': session.get('uploader_name')
    }

    return render_template(
        'accident.html',
        messages=messages,
        current_user=current_user,
        upload_info=upload_info,
        **page_ctx,
    )

@app.route('/add_message', methods=['POST'])
@login_required
def add_message():
    content = request.form.get('content')
    if content:
        message = Message(content=content, user_id=current_user.id)
        db.session.add(message)
        db.session.commit()
        print('메시지 저장됨:', message.content)
        print('DB 메시지 수:', Message.query.count())
        return {"success": True, "message": "메시지가 등록되었습니다."}
    return {"success": False, "message": "메시지 내용을 입력하세요."}

@app.route('/delete_message/<int:message_id>', methods=['POST'])
@login_required
def delete_message(message_id):
    message = Message.query.get_or_404(message_id)
    if message.user_id == current_user.id or current_user.role == 'admin':
        db.session.delete(message)
        db.session.commit()
        return {"success": True, "message": "메시지가 삭제되었습니다."}
    return {"success": False, "message": "삭제 권한이 없습니다."}

USER_DEPARTMENTS = ('임원', '경리과', '기획부', '배차과', '업무부', '정비부')


def ensure_user_department_column():
    """기존 DB에 소속(department) 컬럼이 없으면 추가."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if 'users' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('users')}
    if 'department' not in cols:
        try:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE users ADD COLUMN department VARCHAR(30)'))
        except Exception as e:
            err = str(e).lower()
            if 'duplicate column' not in err and 'already exists' not in err:
                raise


def init_app_database():
    """테이블 생성 및 스키마 마이그레이션 (로컬·gunicorn/Cloudtype 공통)."""
    with app.app_context():
        db.create_all()
        ensure_user_department_column()


# 데이터베이스 생성 (로컬 python app.py 실행용)
def create_database():
    init_app_database()
    init_accident_migrations()


# gunicorn은 __main__을 실행하지 않으므로 import 시 DB 초기화
init_app_database()

# 세션 유지 시간을 매우 길게 설정 (900일)
app.permanent_session_lifetime = timedelta(days=900)

# 허용할 파일 확장자 설정
ALLOWED_EXTENSIONS = {'xlsx', 'xls'}
ALLOWED_DAT_EXTENSIONS = {'dat'}

def allowed_file(filename):
    """허용된 파일 확장자인지 확인"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_dat_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_DAT_EXTENSIONS


def allowed_tmoney_csv_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'csv'

# 업로드·데이터 폴더 생성, .dat 파일은 uploads/dat 으로 정리
for folder in [app.config['UPLOAD_FOLDER'], app.config['DAT_FOLDER'], app.config['DATA_FOLDER']]:
    if not os.path.exists(folder):
        os.makedirs(folder)

upload_dir = app.config['UPLOAD_FOLDER']
dat_dir = app.config['DAT_FOLDER']


def _move_dat_files(src_dir, label):
    if not os.path.isdir(src_dir) or os.path.abspath(src_dir) == os.path.abspath(dat_dir):
        return
    for name in os.listdir(src_dir):
        if not name.lower().endswith('.dat'):
            continue
        src = os.path.join(src_dir, name)
        dst = os.path.join(dat_dir, name)
        if not os.path.isfile(src):
            continue
        if os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
        except OSError as e:
            print(f"{label} → uploads/dat 이동 실패 ({name}): {e}")


_move_dat_files(upload_dir, 'uploads')
_move_dat_files('dat', 'dat(legacy)')



DISPATCH_HEADER_RENAME = {
    '인정일수': '인정일',
    '근무일수': '승무일',
    '승무일수': '승무일',
    '결근일수': '결근일',
}
DISPATCH_LEGACY_STAT_KEYS = ('인정일수', '근무일수', '승무일수', '결근일수')
DISPATCH_MONTH_SHEET_NAMES = [
    '01월', '02월', '03월', '04월', '05월', '06월',
    '07월', '08월', '09월', '10월', '11월', '12월',
]
DISPATCH_MONTH_SHEET_RE = re.compile(r'^\d{2}월$')

_dispatch_data_cache = {'path': None, 'mtime': None, 'data': None}
_vehicle_lookup_cache = {}


def invalidate_dispatch_caches():
    """배차 JSON 저장·갱신 후 캐시 무효화."""
    _dispatch_data_cache['path'] = None
    _dispatch_data_cache['mtime'] = None
    _dispatch_data_cache['data'] = None
    _vehicle_lookup_cache.clear()


def extract_dispatch_year_from_filename(filename):
    match = re.search(r'(\d{4})', filename or '')
    return int(match.group(1)) if match else datetime.now().year


def _is_dispatch_month_store(data):
    if not isinstance(data, dict) or not data:
        return False
    return any(DISPATCH_MONTH_SHEET_RE.match(str(key)) for key in data.keys())


def _default_dispatch_year(year_keys):
    keys = [str(key) for key in year_keys]
    if not keys:
        return datetime.now().year
    current = str(datetime.now().year)
    if current in keys:
        return int(current)
    return int(sorted(keys, reverse=True)[0])


def _infer_legacy_dispatch_year():
    remarks_path = os.path.join(app.config['DATA_FOLDER'], 'dispatch_remarks.json')
    if os.path.exists(remarks_path):
        try:
            with open(remarks_path, 'r', encoding='utf-8') as f:
                raw = json.loads(f.read().strip() or '{}')
            if isinstance(raw, dict):
                if 'by_date' in raw and raw.get('year'):
                    return int(raw['year'])
                legacy_years = [int(key) for key in raw.keys() if str(key).isdigit() and len(str(key)) == 4]
                if len(legacy_years) == 1:
                    return legacy_years[0]
        except Exception:
            pass
    return datetime.now().year


def _coerce_dispatch_store(raw):
    if not raw or not isinstance(raw, dict):
        return OrderedDict()
    if _is_dispatch_month_store(raw):
        year = str(_infer_legacy_dispatch_year())
        return OrderedDict({year: OrderedDict(raw)})
    store = OrderedDict()
    for year_key, year_data in raw.items():
        if isinstance(year_data, dict):
            store[str(year_key)] = OrderedDict(year_data) if not isinstance(year_data, OrderedDict) else year_data
    return store


def _coerce_dispatch_remarks_store(raw):
    if not raw or not isinstance(raw, dict):
        return {}
    if 'by_date' in raw:
        year = raw.get('year') or datetime.now().year
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = datetime.now().year
        return {str(year): {'by_date': raw.get('by_date') or {}}}
    if raw and all(str(key).isdigit() and len(str(key)) == 4 for key in raw.keys()):
        store = {}
        for year_key, payload in raw.items():
            if isinstance(payload, dict) and 'by_date' in payload:
                store[str(year_key)] = {'by_date': payload.get('by_date') or {}}
            else:
                store[str(year_key)] = {'by_date': payload or {}}
        return store
    if raw:
        year = datetime.now().year
        try:
            year = int(next(iter(raw.keys()))[:4])
        except (ValueError, TypeError, StopIteration):
            pass
        return {str(year): {'by_date': raw}}
    return {}


def parse_dispatch_excel(file_path):
    """배차관리 엑셀 월별 시트 파싱."""
    dispatch_data = OrderedDict()
    for sheet in DISPATCH_MONTH_SHEET_NAMES:
        try:
            df = pd.read_excel(file_path, sheet_name=sheet)
            processed_data = []
            for _, row in df.iterrows():
                row_dict = {}
                for col in df.columns:
                    val = row[col]
                    row_dict[str(col)] = str(val) if pd.notna(val) else ''
                processed_data.append(enrich_dispatch_record(row_dict))
            dispatch_data[sheet] = normalize_dispatch_sheet({
                'headers': [str(col) for col in df.columns],
                'data': processed_data,
            })
        except Exception:
            continue
    return dispatch_data


def _schedule_page_context(selected_year=None, error=None):
    store = load_dispatch_data_store()
    years = sorted([int(year_key) for year_key in store.keys()], reverse=True) if store else []
    if selected_year is None:
        query_year = request.args.get('year', type=int)
        if query_year and query_year in years:
            selected_year = query_year
        elif years:
            selected_year = years[0]
        else:
            selected_year = datetime.now().year
    elif years and selected_year not in years:
        selected_year = years[0]
    dispatch_data = load_dispatch_data(selected_year) if store else None
    dispatch_remarks = load_dispatch_remarks(selected_year)
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    ctx = {
        'dispatch_data': dispatch_data,
        'dispatch_remarks': dispatch_remarks,
        'dispatch_years': years,
        'selected_dispatch_year': selected_year if years else None,
        'messages': messages,
        'current_user': current_user,
    }
    if error:
        ctx['error'] = error
    return ctx


def _dispatch_data_is_normalized(data):
    """이미 enrich된 배차 JSON이면 load 시 재계산 생략."""
    if not data:
        return True
    for sheet_data in data.values():
        headers = [str(h) for h in sheet_data.get('headers', [])]
        if any(h in DISPATCH_LEGACY_STAT_KEYS for h in headers):
            return False
        rows = sheet_data.get('data', [])
        if rows and '인정일' not in rows[0] and '승무일' not in rows[0]:
            return False
    return True


def _dispatch_val_matches(val, symbol):
    """배차 일자 심볼 비교 (O/X/H는 대·소문자 모두 인정)."""
    v = str(val).strip()
    if symbol in ('o', 'x'):
        return v.lower() == symbol
    if symbol == 'H':
        return v.upper() == 'H'
    return v == symbol


def compute_dispatch_row_stats(row):
    """배차 엑셀 수식: 승무일=COUNTIF(o), 결근일=x, 휴가=/, 인정일=승무일+휴가+COUNTIF(H)."""
    승무일 = 결근일 = 휴가 = h_count = 0
    for day in range(1, 32):
        val = str(row.get(str(day), '')).strip()
        if _dispatch_val_matches(val, 'o'):
            승무일 += 1
        elif _dispatch_val_matches(val, 'x'):
            결근일 += 1
        elif val == '/':
            휴가 += 1
        elif _dispatch_val_matches(val, 'H'):
            h_count += 1
    인정일 = 승무일 + 휴가 + h_count
    return {
        '인정일': str(인정일),
        '승무일': str(승무일),
        '결근일': str(결근일),
        '휴가': str(휴가),
    }


def enrich_dispatch_record(row):
    stats = compute_dispatch_row_stats(row)
    for old_key in DISPATCH_LEGACY_STAT_KEYS:
        row.pop(old_key, None)
    row.update(stats)
    if '사번' in row:
        row['사번'] = normalize_emp_id(row['사번'])
    return row


def normalize_dispatch_headers(headers):
    renamed = [DISPATCH_HEADER_RENAME.get(str(h), str(h)) for h in headers]
    if '휴가' not in renamed:
        insert_at = None
        for key in ('결근일', '승무일', '인정일'):
            if key in renamed:
                insert_at = renamed.index(key) + 1
                break
        if insert_at is not None:
            renamed.insert(insert_at, '휴가')
        else:
            day_idx = next((i for i, h in enumerate(renamed) if str(h).isdigit()), len(renamed))
            renamed.insert(day_idx, '휴가')
    return renamed


def normalize_dispatch_sheet(sheet_data):
    headers = normalize_dispatch_headers(sheet_data.get('headers', []))
    data = [enrich_dispatch_record(dict(row)) for row in sheet_data.get('data', [])]
    return {'headers': headers, 'data': data}


def normalize_dispatch_data(data):
    if not data:
        return data
    return OrderedDict(
        (sheet, normalize_dispatch_sheet(sheet_data))
        for sheet, sheet_data in data.items()
    )


def save_dispatch_data(data, year=None):
    print("=== save_dispatch_data 함수 시작 ===")
    filepath = os.path.join(app.config['DATA_FOLDER'], 'dispatch_data.json')
    print(f"JSON 저장 경로: {filepath}")
    if year is None:
        year = datetime.now().year
    store = load_dispatch_data_store() or OrderedDict()
    store[str(year)] = normalize_dispatch_data(data)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    invalidate_dispatch_caches()
    print("JSON 파일 저장 완료")


def parse_dispatch_remarks_excel(file_path):
    """배차관리 엑셀 [비고] 시트에서 날짜별 대체·결근·비고 텍스트 추출."""
    try:
        df = pd.read_excel(file_path, sheet_name='비고')
    except Exception:
        return {}, None
    df.columns = [str(c).strip() for c in df.columns]
    if '날짜' not in df.columns:
        return {}, None

    def _remark_cell(row, col_name):
        if col_name not in df.columns:
            return ''
        val = row.get(col_name)
        if pd.isna(val):
            return ''
        return str(val).strip()

    by_date = {}
    year_from_file = None
    fname = os.path.basename(file_path)
    year_match = re.search(r'(\d{4})', fname)
    if year_match:
        year_from_file = int(year_match.group(1))

    for _, row in df.iterrows():
        d = row.get('날짜')
        if pd.isna(d):
            continue
        if hasattr(d, 'strftime'):
            date_str = d.strftime('%Y-%m-%d')
        else:
            date_str = str(pd.Timestamp(d))[:10]
        if not date_str or len(date_str) < 10:
            continue
        if year_from_file is None:
            try:
                year_from_file = int(date_str[:4])
            except (ValueError, TypeError):
                pass
        by_date[date_str] = {
            '대체': _remark_cell(row, '대체'),
            '결근': _remark_cell(row, '결근'),
            '비고': _remark_cell(row, '비고'),
        }
    return by_date, year_from_file


def save_dispatch_remarks(by_date, year=None):
    if year is None and by_date:
        try:
            year = int(next(iter(by_date.keys()))[:4])
        except (ValueError, TypeError, StopIteration):
            year = datetime.now().year
    elif year is None:
        year = datetime.now().year
    store = load_dispatch_remarks_store()
    store[str(year)] = {'by_date': by_date or {}}
    filepath = os.path.join(app.config['DATA_FOLDER'], 'dispatch_remarks.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def load_dispatch_remarks_store():
    filepath = os.path.join(app.config['DATA_FOLDER'], 'dispatch_remarks.json')
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return {}
            raw = json.loads(content)
            return _coerce_dispatch_remarks_store(raw)
    except Exception:
        pass
    return {}


def load_dispatch_remarks(year=None):
    store = load_dispatch_remarks_store()
    if not store:
        fallback_year = year or datetime.now().year
        return {'year': fallback_year, 'by_date': {}}
    if year is None:
        year = _default_dispatch_year(store.keys())
    payload = store.get(str(year), {})
    return {
        'year': int(year),
        'by_date': payload.get('by_date') or {},
    }


def load_dispatch_data_store():
    """년도별 배차 데이터 전체 저장소 로드."""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'dispatch_data.json')
    if not os.path.exists(filepath):
        return OrderedDict()

    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        mtime = None

    if (
        _dispatch_data_cache['path'] == filepath
        and _dispatch_data_cache['mtime'] == mtime
        and _dispatch_data_cache['data'] is not None
    ):
        return _dispatch_data_cache['data']

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                print("dispatch_data.json 파일이 비어있습니다.")
                return OrderedDict()
            raw = json.loads(content)
            if not isinstance(raw, dict):
                return OrderedDict()
            store = _coerce_dispatch_store(raw)
            changed = _is_dispatch_month_store(raw)
            normalized_store = OrderedDict()
            for year_key, year_data in store.items():
                if not _dispatch_data_is_normalized(year_data):
                    normalized_store[str(year_key)] = normalize_dispatch_data(year_data)
                    changed = True
                else:
                    normalized_store[str(year_key)] = year_data
            if changed:
                with open(filepath, 'w', encoding='utf-8') as wf:
                    json.dump(normalized_store, wf, ensure_ascii=False, indent=2)
                try:
                    mtime = os.path.getmtime(filepath)
                except OSError:
                    mtime = None
            _dispatch_data_cache.update({'path': filepath, 'mtime': mtime, 'data': normalized_store})
            return normalized_store
    except json.JSONDecodeError as e:
        print(f"dispatch_data.json JSON 파싱 오류: {e}")
        return OrderedDict()
    except Exception as e:
        print(f"dispatch_data.json 읽기 오류: {e}")
        return OrderedDict()


def load_dispatch_data(year=None):
    """저장된 배차 데이터를 불러옴 (프로세스 내 캐시)."""
    store = load_dispatch_data_store()
    if not store:
        return None
    if year is None:
        year = _default_dispatch_year(store.keys())
    return store.get(str(year))

def save_lease_data(data):
    print("=== save_lease_data 함수 시작 ===")
    filepath = os.path.join(app.config['DATA_FOLDER'], 'lease_data.json')
    print(f"JSON 저장 경로: {filepath}")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("JSON 파일 저장 완료")


def load_lease_data():
    """저장된 리스 급여 데이터를 불러옴"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'lease_data.json')
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:  # 파일이 비어있지 않은 경우에만 파싱
                    return json.loads(content)
                else:
                    print(f"lease_data.json 파일이 비어있습니다.")
                    return None
        except json.JSONDecodeError as e:
            print(f"lease_data.json JSON 파싱 오류: {e}")
            return None
        except Exception as e:
            print(f"lease_data.json 읽기 오류: {e}")
            return None
    return None

DEFAULT_FUEL_PRICE = 1100


def extract_car_suffix(value):
    """차량번호에서 뒤 4자리(차번) 추출."""
    text = str(value or '').strip()
    if not text:
        return ''
    match = re.search(r'(\d{4})\s*$', text)
    if match:
        return match.group(1)
    digits = re.sub(r'\D', '', text)
    if len(digits) >= 4:
        return digits[-4:]
    return digits.zfill(4) if digits else ''


def normalize_emp_id(value):
    """사번을 정수 문자열로 통일 (6228.0 → 6228)."""
    if value is None:
        return ''
    s = str(value).strip()
    if not s or s.lower() in ('nan', 'none'):
        return ''
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    if re.fullmatch(r'\d+\.0+', s):
        return s.split('.')[0]
    try:
        num = float(s)
        if num == int(num):
            return str(int(num))
    except ValueError:
        pass
    return s


def sales_dispatch_month_key(business_date: str) -> str | None:
    """행 날짜 → 배차 데이터 월 키 (예: 2026-04-01 → 04월)."""
    if not business_date:
        return None
    date_part = str(business_date).strip()[:10]
    if len(date_part) < 7:
        return None
    return f"{date_part[5:7]}월"


def _dispatch_month_from_dat_filename(source_file: str) -> str | None:
    """`.dat` 파일명 앞 8자리(YYYYMMDD) → 배차 월."""
    basename = os.path.basename(str(source_file or '').replace('\\', '/'))
    match = re.match(r'^(\d{8})_', basename)
    if not match:
        return None
    raw = match.group(1)
    return sales_dispatch_month_key(f'{raw[:4]}-{raw[4:6]}-{raw[6:8]}')


def sales_dispatch_lookup_month(
    business_date=None,
    parsed=None,
    row=None,
    daily_date=None,
) -> str | None:
    """배차 조회 월 — 장기 마감·파일명은 마감월, 일반 행은 해당일 월."""
    if parsed and is_prolonged_closing(parsed):
        header = parsed.get('header') or {}
        closing_ref = (
            str(header.get('end') or '')[:10]
            or parsed.get('closing_date')
            or parsed.get('file_date')
        )
        if closing_ref:
            month = sales_dispatch_month_key(closing_ref)
            if month:
                return month
    if row and str(row.get('집계기준') or '') == 'daily_split':
        month = _dispatch_month_from_dat_filename(row.get('원본파일'))
        if month:
            return month
    day = str(daily_date or business_date or (row or {}).get('날짜') or '').strip()[:10]
    return sales_dispatch_month_key(day) if day else None


_DISPATCH_DAY_WORK_TYPES = ('주간', '일차', '리스')
_DISPATCH_NIGHT_WORK_TYPES = ('야간',)


def _dispatch_entry_info(record, source='dispatch'):
    vehicle_no = record.get('차량번호') or record.get('차번') or ''
    suffix = extract_car_suffix(vehicle_no)
    if not suffix:
        return None, None
    return suffix, {
        '차량번호': vehicle_no,
        '차번': suffix,
        '사번': normalize_emp_id(record.get('사번', '')),
        '이름': str(record.get('운전기사') or record.get('이름') or ''),
        '차종': str(record.get('차종', '') or ''),
        '근무유형': str(record.get('근무유형', '') or ''),
        'source': source,
    }


def _lookup_car_entries(lookup, car_suffix: str) -> list:
    """차번 suffix에 해당하는 배차·기사 후보 목록."""
    if not lookup or not car_suffix:
        return []
    if isinstance(lookup, dict) and 'suffixes' in lookup:
        return list(lookup['suffixes'].get(car_suffix, []))
    info = lookup.get(car_suffix)
    return [info] if info else []


def _pick_dispatch_entry(entries: list, shift_band: str = '', emp: str = '') -> dict:
    """동일 차번 다중 배차(주간/야간 등) 중 사번·시작 시각에 맞는 기사 선택."""
    if not entries:
        return {}
    if len(entries) == 1:
        return entries[0]

    emp = normalize_emp_id(emp)
    if emp:
        for entry in entries:
            if normalize_emp_id(entry.get('사번', '')) == emp:
                return entry

    work_type = lambda e: str(e.get('근무유형', '') or '').strip()

    if shift_band == 'day':
        for preferred in _DISPATCH_DAY_WORK_TYPES:
            for entry in entries:
                if work_type(entry) == preferred:
                    return entry
    elif shift_band == 'night':
        for preferred in _DISPATCH_NIGHT_WORK_TYPES:
            for entry in entries:
                if work_type(entry) == preferred:
                    return entry
        for entry in entries:
            if work_type(entry) == '교대':
                return entry

    return entries[0]


def build_vehicle_lookup(dispatch_month: str | None = None):
    """배차·정비·기사 데이터에서 차번(4자리) → 기사/차량 정보 매핑.

    동일 차번에 여러 근무(주간/야간) 행이 있으면 suffixes[차번]에 목록으로 보관.
    dispatch_month가 있으면 해당 월 배차만 사용 (수입금 행 날짜 기준).
    """
    cache_key = dispatch_month or '__all__'
    if cache_key in _vehicle_lookup_cache:
        return _vehicle_lookup_cache[cache_key]

    suffixes: dict[str, list] = {}

    dispatch_data = load_dispatch_data()
    if dispatch_data:
        if dispatch_month:
            month_data = dispatch_data.get(dispatch_month)
            month_sources = [(dispatch_month, month_data)] if month_data else []
        else:
            month_sources = list(dispatch_data.items())

        for _month_key, month_data in month_sources:
            for record in month_data.get('data', []):
                suffix, entry = _dispatch_entry_info(record)
                if not suffix or not entry:
                    continue
                bucket = suffixes.setdefault(suffix, [])
                work = entry.get('근무유형', '')
                entry_emp = normalize_emp_id(entry.get('사번', ''))
                replaced = False
                for idx, existing in enumerate(bucket):
                    if (existing.get('근무유형') == work
                            and normalize_emp_id(existing.get('사번', '')) == entry_emp):
                        bucket[idx] = entry
                        replaced = True
                        break
                if not replaced:
                    bucket.append(entry)

    events = load_all_car_maintenance_events()
    for event in events:
        suffix = extract_car_suffix(event.get('차번', ''))
        if not suffix or suffix in suffixes:
            continue
        suffixes[suffix] = [{
            '차량번호': event.get('차번', ''),
            '차번': suffix,
            '사번': '',
            '이름': '',
            '차종': str(event.get('차종', '') or ''),
            '근무유형': '',
            'source': 'maintenance',
        }]

    for driver in load_all_driver_records():
        for key in ('차번', '차량번호', '배정차량'):
            suffix = extract_car_suffix(driver.get(key, ''))
            if not suffix:
                continue
            if suffix not in suffixes:
                suffixes[suffix] = [{
                    '차량번호': driver.get(key, ''),
                    '차번': suffix,
                    '사번': normalize_emp_id(driver.get('사번', '')),
                    '이름': str(driver.get('이름', '') or ''),
                    '차종': str(driver.get('차종', '') or ''),
                    '근무유형': str(driver.get('근무유형', '') or ''),
                    'source': 'driver',
                }]
            else:
                for entry in suffixes[suffix]:
                    if not entry.get('사번') and driver.get('사번'):
                        entry['사번'] = normalize_emp_id(driver.get('사번'))
                    if not entry.get('이름') and driver.get('이름'):
                        entry['이름'] = str(driver.get('이름'))
                    if not entry.get('차종') and driver.get('차종'):
                        entry['차종'] = str(driver.get('차종'))

    lookup = {'suffixes': suffixes}
    _vehicle_lookup_cache[cache_key] = lookup
    return lookup


def _should_prefer_shift_dispatch_match(row=None, daily_date=None) -> bool:
    """장기 마감 일별 행 — 당일 클립 출고 시각으로 주·야 기사 매칭."""
    if daily_date:
        return True
    if row and str(row.get('집계기준') or '') == 'daily_split':
        return True
    return False


def _dispatch_shift_start_for_match(
    parsed=None,
    row=None,
    business_date=None,
    daily_date=None,
) -> str:
    """배차 주·야 매칭용 출고 시각. 장기 마감 일별 행은 해당 날짜로 클립한 시각 사용."""
    day = str(daily_date or business_date or (row or {}).get('날짜') or '').strip()[:10]
    header = (parsed or {}).get('header') or {}
    closing_start = str(header.get('start') or '').strip()
    prefer_shift = _should_prefer_shift_dispatch_match(row=row, daily_date=daily_date)
    if not prefer_shift and parsed and day and is_prolonged_closing(parsed):
        prefer_shift = True

    if prefer_shift and day and closing_start:
        clipped = clip_datetime_to_business_day(closing_start, day, 'start')
        if clipped:
            return clipped

    row_start = str((row or {}).get('영업시작') or '').strip()
    if row_start:
        return row_start
    return closing_start


def match_vehicle_record(
    car_suffix,
    plate='',
    lookup=None,
    business_date=None,
    parsed=None,
    row=None,
    daily_date=None,
):
    dispatch_month = sales_dispatch_lookup_month(
        business_date=business_date, parsed=parsed, row=row, daily_date=daily_date,
    )
    if lookup is None:
        lookup = build_vehicle_lookup(dispatch_month=dispatch_month)

    shift_start = _dispatch_shift_start_for_match(
        parsed=parsed, row=row, business_date=business_date, daily_date=daily_date,
    )
    shift_band = infer_shift_band_from_start(shift_start)

    prefer_shift = _should_prefer_shift_dispatch_match(row=row, daily_date=daily_date)
    if not prefer_shift and parsed and business_date and is_prolonged_closing(parsed):
        prefer_shift = True

    emp_hint = ''
    if row and _is_tmoney_csv_matched(row):
        emp_hint = normalize_emp_id(row.get('사번', ''))

    entries = _lookup_car_entries(lookup, car_suffix)
    if prefer_shift and shift_band and not emp_hint:
        info = _pick_dispatch_entry(entries, shift_band, emp='')
    else:
        info = _pick_dispatch_entry(entries, shift_band, emp=emp_hint)
    matched = bool(info.get('이름') or info.get('사번'))
    return {
        '차번': car_suffix,
        '차량번호': info.get('차량번호') or plate or car_suffix,
        '사번': normalize_emp_id(info.get('사번', '')),
        '이름': info.get('이름', ''),
        '차종': info.get('차종', ''),
        '근무유형': info.get('근무유형', ''),
        '매칭': '완료' if matched else '실패',
    }


def sales_record_key(row) -> tuple:
    """수입금 행 고유 키 — (날짜, 차번, 사번) 우선.

    T머니(CSV)와 .dat(일반·장기 마감)를 같은 기사·같은 날 한 행으로 병합.
    """
    date = str(row.get('날짜') or '').strip()
    car = str(row.get('차번') or '').strip()
    emp = normalize_emp_id(row.get('사번', ''))
    if emp:
        return (date, car, 'emp', emp)
    if str(row.get('집계기준') or '') == 'daily_split':
        return (date, car, 'daily_split')
    work = str(row.get('근무유형') or '').strip()
    if work:
        return (date, car, 'work', work)
    start = str(row.get('영업시작') or '').strip()[:16]
    if start:
        return (date, car, 'start', start)
    source = os.path.basename(str(row.get('원본파일') or '').replace('\\', '/'))
    if source:
        return (date, car, 'file', source)
    return (date, car, '', '')


def _sales_row_matches_update(row, item) -> bool:
    """저장 API — 수정 대상 행 일치 (다중 근무·동일 차번 구분)."""
    if row.get('날짜') != str(item.get('날짜', '')).strip():
        return False
    if str(row.get('차번', '')).strip() != str(item.get('차번', '')).strip():
        return False

    start = str(item.get('영업시작') or '').strip()[:16]
    row_start = str(row.get('영업시작') or '').strip()[:16]
    if start and row_start:
        return row_start == start

    emp = normalize_emp_id(item.get('사번', ''))
    if emp:
        return normalize_emp_id(row.get('사번', '')) == emp

    work = str(item.get('근무유형') or '').strip()
    row_work = str(row.get('근무유형') or '').strip()
    if work and row_work:
        return row_work == work

    return sales_record_key(row) == sales_record_key({
        '날짜': item.get('날짜', ''),
        '차번': item.get('차번', ''),
        '영업시작': item.get('영업시작', ''),
        '근무유형': item.get('근무유형', ''),
        '사번': item.get('사번', ''),
        '원본파일': item.get('원본파일', ''),
    })


def compute_sales_summary(data):
    if not data:
        return {'total_count': 0, 'total_income': 0, 'avg_income': 0}
    incomes = [int(r.get('실입금', 0) or 0) for r in data]
    return {
        'total_count': len(data),
        'total_income': sum(incomes),
        'avg_income': int(sum(incomes) / len(incomes)) if incomes else 0,
    }


def _load_sales_parsed_row(row):
    """원본 .dat 파일이 있으면 재파싱."""
    source_file = row.get('원본파일')
    business_date = row.get('날짜', '')
    if not source_file or not business_date:
        return None
    filename = os.path.basename(str(source_file).replace('/', '').replace('\\', ''))
    filepath = os.path.join(app.config['DAT_FOLDER'], filename)
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, 'rb') as dat_file:
            return parse_dat_bytes(dat_file.read(), filename)
    except OSError:
        return None


def _sales_fuel_unit_price(row, fuel_price=None):
    if fuel_price is not None:
        return float(fuel_price)
    try:
        stored = row.get('연료단가')
        if stored not in (None, ''):
            return float(stored)
    except (ValueError, TypeError):
        pass
    try:
        liters = float(row.get('충전량') or row.get('연료L') or 0)
        cost = float(row.get('연료비') or 0)
        if liters > 0 and cost > 0:
            return cost / liters
    except (ValueError, TypeError):
        pass
    return float(DEFAULT_FUEL_PRICE)


def resolve_dat_metrics(row, parsed=None, fuel_price=None, reparse_dat=False, daily_date=None):
    """.dat 마감 기준 운행 지표만 (실입금·건수는 T머니 CSV 전용).

    - 일반 마감(1~2일): #2/#3 파일 전체 (closing).
    - 장기 마감(3일+): daily_date(또는 행 날짜)에 해당하는 당일분만 (daily_split).
    """
    if parsed is None and reparse_dat:
        parsed = _load_sales_parsed_row(row)

    if parsed is not None:
        business_day = daily_date or str(row.get('날짜') or '').strip()[:10]
        if row.get('집계기준') == 'daily_split' or (
            is_prolonged_closing(parsed) and daily_date
        ):
            metrics = compute_daily_sales_metrics(parsed, business_day)
        else:
            metrics = compute_closing_sales_metrics(parsed)
        unit_price = _sales_fuel_unit_price(row, fuel_price=fuel_price)
        fuel_liters = metrics['fuel_l']
        return {
            '충전량': str(fuel_liters),
            '연료비': str(int(round(fuel_liters * unit_price))),
            '운행거리': str(metrics['distance_km']),
            '총거리': str(metrics.get('total_distance_km', 0)),
            '총시간': str(metrics.get('total_minutes', 0)),
            '빈차시간': str(metrics.get('empty_minutes', 0)),
            '빈차거리': str(metrics.get('empty_distance_km', 0)),
        }

    return {
        '충전량': str(row.get('충전량') or row.get('연료L') or '0'),
        '연료비': str(int(row.get('연료비') or 0)),
        '운행거리': str(row.get('운행거리') or '0'),
        '총거리': str(row.get('총거리') or '0'),
        '총시간': str(row.get('총시간') or '0'),
        '빈차시간': str(row.get('빈차시간') or '0'),
        '빈차거리': str(row.get('빈차거리') or '0'),
    }


def resolve_sales_metrics(row, parsed=None, fuel_price=None, reparse_dat=False, daily_date=None):
    """레거시 호환 — .dat 운행 지표 + 저장된 T머니 실입금·건수."""
    metrics = resolve_dat_metrics(
        row, parsed=parsed, fuel_price=fuel_price,
        reparse_dat=reparse_dat, daily_date=daily_date,
    )
    metrics['실입금'] = str(int(row.get('실입금') or 0))
    metrics['건수'] = str(int(row.get('건수') or 0))
    return metrics


def _is_blank_sales_text(value) -> bool:
    text = str(value or '').strip()
    if not text or text == '-':
        return True
    return text.lower() in ('nan', 'none')


_TMONEY_CSV_SOURCES = frozenset({'driver', 'driver_added', 'car_added', 'daily'})


def _is_tmoney_csv_matched(row) -> bool:
    """T머니 CSV와 실제 매칭된 행만 (no_driver_match 등 제외)."""
    return str(row.get('T머니출처') or '').strip() in _TMONEY_CSV_SOURCES


def _row_has_tmoney_source(row) -> bool:
    return _is_tmoney_csv_matched(row)


def _row_has_dat_source(row) -> bool:
    source = str(row.get('원본파일') or '').lower()
    if source and '.dat' in source:
        return True
    if str(row.get('집계기준') or '') in ('closing', 'daily_split'):
        return True
    return bool(str(row.get('영업시작') or '').strip())


_SALES_TABLE_CSV_COLS = (
    '날짜', '차번', '차종', '근무유형', '사번', '운전기사', '실입금', '건수',
)
_SALES_TABLE_DAT_COLS = (
    '출고일시', '입고일시', '영업시간', '빈차시간', '총시간',
    '연료비', '충전량', '운행거리', '빈차거리', '총거리',
)
_SALES_NUMERIC_ZERO_COLS = frozenset({
    '실입금', '건수', '영업시간', '빈차시간', '총시간',
    '연료비', '충전량', '운행거리', '빈차거리', '총거리',
})


def _sales_display_field_value(row, col: str) -> str:
    if col == '운전기사':
        return str(row.get('이름') or '').strip()
    if col == '출고일시':
        return str(row.get('영업시작') or row.get('마감시작') or '').strip()
    if col == '입고일시':
        return str(row.get('영업종료') or row.get('마감종료') or '').strip()
    if col == '충전량':
        return str(row.get('충전량') or row.get('연료L') or '').strip()
    return str(row.get(col) or '').strip()


def _sales_field_is_zero(row, col: str) -> bool:
    if col in ('날짜', '차번', '차종', '근무유형', '사번', '운전기사', '출고일시', '입고일시'):
        return _is_blank_sales_text(_sales_display_field_value(row, col))
    if col in _SALES_NUMERIC_ZERO_COLS:
        try:
            return float(_sales_display_field_value(row, col) or 0) <= 0
        except (TypeError, ValueError):
            return True
    return False


def _dispatch_shift_entry(row, lookup=None) -> dict:
    """출고 시각·근무 기준 배차 기사 (CSV 사번 힌트 없음)."""
    car = str(row.get('차번') or '').strip()
    if not car:
        return {}
    if lookup is None:
        lookup = build_vehicle_lookup(
            dispatch_month=sales_dispatch_lookup_month(
                business_date=row.get('날짜', ''), row=row,
            ),
        )
    entries = _lookup_car_entries(lookup, car)
    if not entries:
        return {}
    daily_date = (
        str(row.get('날짜') or '').strip()[:10]
        if str(row.get('집계기준') or '') == 'daily_split' else None
    )
    shift_start = _dispatch_shift_start_for_match(
        row=row, business_date=row.get('날짜'), daily_date=daily_date,
    )
    shift_band = infer_shift_band_from_start(shift_start)
    if not shift_band:
        work = str(row.get('근무유형') or '').strip()
        if work in _DISPATCH_DAY_WORK_TYPES:
            shift_band = 'day'
        elif work in _DISPATCH_NIGHT_WORK_TYPES or work == '교대':
            shift_band = 'night'
    return _pick_dispatch_entry(entries, shift_band, emp='') or {}


def _csv_field_dispatch_mismatch(row, col: str, dispatch_entry: dict) -> bool:
    if col == '날짜':
        return _is_blank_sales_text(row.get('날짜'))
    if col == '차번':
        if _is_blank_sales_text(row.get('차번')):
            return True
        return not dispatch_entry
    if col in ('실입금', '건수'):
        return False
    if not _is_tmoney_csv_matched(row):
        return False
    if col == '사번':
        val = normalize_emp_id(row.get('사번', ''))
        if _is_blank_sales_text(val):
            return False
        exp = normalize_emp_id(dispatch_entry.get('사번', ''))
        return bool(exp and val != exp)
    if col == '운전기사':
        val = str(row.get('이름') or '').strip()
        if _is_blank_sales_text(val):
            return False
        exp = str(dispatch_entry.get('이름') or '').strip()
        return bool(exp and val != exp)
    if col in ('차종', '근무유형'):
        val = str(row.get(col) or '').strip()
        if _is_blank_sales_text(val):
            return False
        exp = str(dispatch_entry.get(col) or '').strip()
        return bool(exp and val != exp)
    return False


def _dat_field_csv_mismatch(row, col: str) -> bool:
    """개별 .dat 셀 — CSV(근무·사번)와 내용이 어긋날 때만."""
    if not _is_tmoney_csv_matched(row) or _sales_field_is_zero(row, col):
        return False
    if col == '출고일시':
        start = _sales_display_field_value(row, '출고일시')
        dat_band = infer_shift_band_from_start(start)
        if not dat_band:
            return False
        csv_work = str(row.get('근무유형') or '').strip()
        if csv_work in _DISPATCH_DAY_WORK_TYPES:
            return dat_band != 'day'
        if csv_work in _DISPATCH_NIGHT_WORK_TYPES or csv_work == '교대':
            return dat_band != 'night'
    return False


def compute_sales_cell_warnings(row, lookup=None) -> dict[str, str]:
    """표 셀 강조 — 문제 있는 셀만 csv(주황)·dat(보라) 표시."""
    if not isinstance(row, dict):
        return {}
    dispatch_entry = _dispatch_shift_entry(row, lookup=lookup)
    has_csv = _is_tmoney_csv_matched(row)
    warns: dict[str, str] = {}

    for col in _SALES_TABLE_CSV_COLS:
        if col in ('실입금', '건수'):
            if not has_csv or _sales_field_is_zero(row, col):
                warns[col] = 'csv'
            continue
        if _sales_field_is_zero(row, col):
            warns[col] = 'csv'
        elif _csv_field_dispatch_mismatch(row, col, dispatch_entry):
            warns[col] = 'csv'

    for col in _SALES_TABLE_DAT_COLS:
        if _sales_field_is_zero(row, col):
            warns[col] = 'dat'
        elif _dat_field_csv_mismatch(row, col):
            warns[col] = 'dat'

    return warns


def _memo_sales_cell_warnings(row, lookup=None) -> dict[str, str]:
    cached = row.get('_cell_warn')
    if isinstance(cached, dict):
        return cached
    warns = compute_sales_cell_warnings(row, lookup=lookup)
    row['_cell_warn'] = warns
    return warns


@app.template_filter('sales_cell_warn')
def sales_cell_warn_filter(row, col):
    """표 열 이름 → 'csv' | 'dat' | ''."""
    if not isinstance(row, dict):
        return ''
    return _memo_sales_cell_warnings(row).get(str(col or '').strip(), '')


@app.template_filter('sales_match_class')
def sales_match_class_filter(row):
    """[매칭] 열 CSS — 완료(녹색)·실패(출처별 색)."""
    if not isinstance(row, dict):
        return 'match-fail'
    if str(row.get('매칭') or '').strip() == '완료':
        return 'match-ok'
    tier = str(row.get('_match_tier') or 'all').strip()
    return {
        'dispatch': 'match-fail-dispatch',
        'csv': 'match-fail-csv',
        'dat': 'match-fail-dat',
    }.get(tier, 'match-fail')


def _strip_ephemeral_sales_row_fields(data) -> None:
    if not data:
        return
    for month in data.values():
        for row in month.get('data', []):
            if isinstance(row, dict):
                row.pop('_cell_warn', None)
                row.pop('_dat_match_identity', None)
                row.pop('_match_tier', None)


def _sales_match_identity_tuple(date, car, emp, name):
    return (
        str(date or '').strip(),
        str(car or '').strip(),
        normalize_emp_id(emp),
        str(name or '').strip(),
    )


def _sales_match_identity_valid(identity) -> bool:
    if not identity or not isinstance(identity, (list, tuple)) or len(identity) < 4:
        return False
    date, car, emp, name = identity[:4]
    return bool(date and car and (emp or name))


def _snapshot_dat_match_identity(row, lookup=None) -> None:
    """.dat 쪽 기사 정보 스냅샷 — CSV 표시값 덮어쓰기 전·병합 후 대조용."""
    if not _row_has_dat_source(row):
        row.pop('_dat_match_identity', None)
        return
    date = str(row.get('날짜') or '').strip()
    car = str(row.get('차번') or '').strip()
    if not date or not car:
        row.pop('_dat_match_identity', None)
        return
    if _is_tmoney_csv_matched(row):
        entry = _dispatch_shift_entry(row, lookup=lookup)
        emp = normalize_emp_id(entry.get('사번', '')) if entry else ''
        name = str(entry.get('이름') or '').strip() if entry else ''
    else:
        emp = normalize_emp_id(row.get('사번', ''))
        name = str(row.get('이름') or '').strip()
    if not emp and not name:
        row.pop('_dat_match_identity', None)
        return
    row['_dat_match_identity'] = list(_sales_match_identity_tuple(date, car, emp, name))


def _extract_dispatch_match_identity(row, lookup=None):
    entry = _dispatch_shift_entry(row, lookup=lookup)
    if not entry:
        return None
    identity = _sales_match_identity_tuple(
        row.get('날짜'), row.get('차번'),
        entry.get('사번'), entry.get('이름'),
    )
    return identity if _sales_match_identity_valid(identity) else None


def _extract_csv_match_identity(row):
    if not _is_tmoney_csv_matched(row):
        return None
    identity = _sales_match_identity_tuple(
        row.get('날짜'), row.get('차번'),
        row.get('사번'), row.get('이름'),
    )
    return identity if _sales_match_identity_valid(identity) else None


def _extract_dat_match_identity(row, lookup=None):
    if not _row_has_dat_source(row):
        return None
    stored = row.get('_dat_match_identity')
    if _sales_match_identity_valid(stored):
        return tuple(stored[:4])
    entry = _dispatch_shift_entry(row, lookup=lookup)
    if entry:
        identity = _sales_match_identity_tuple(
            row.get('날짜'), row.get('차번'),
            entry.get('사번'), entry.get('이름'),
        )
        if _sales_match_identity_valid(identity):
            return identity
    identity = _sales_match_identity_tuple(
        row.get('날짜'), row.get('차번'),
        row.get('사번'), row.get('이름'),
    )
    return identity if _sales_match_identity_valid(identity) else None


def _resolve_sales_match_tier(source_identities: dict[str, tuple]) -> str:
    """배차·CSV·.dat 대조 결과 — ok | all | dispatch | csv | dat."""
    if len(source_identities) <= 1:
        return 'ok' if source_identities else 'all'

    identities = list(source_identities.values())
    if len(set(identities)) == 1:
        return 'ok'

    counts = {}
    for ident in identities:
        counts[ident] = counts.get(ident, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    top_ident, top_count = ranked[0]
    if top_count < 2:
        return 'all'
    if len(ranked) > 1 and ranked[1][1] == top_count:
        return 'all'

    dissenters = [
        source for source, ident in source_identities.items()
        if ident != top_ident
    ]
    if len(dissenters) != 1:
        return 'all'
    return dissenters[0]


def compute_sales_match_status(row, lookup=None) -> str:
    """배차·CSV·.dat의 날짜·차번·사번·이름 대조 → 완료/실패 및 색상 단계."""
    sources = {}
    dispatch_id = _extract_dispatch_match_identity(row, lookup=lookup)
    if dispatch_id:
        sources['dispatch'] = dispatch_id
    csv_id = _extract_csv_match_identity(row)
    if csv_id:
        sources['csv'] = csv_id
    dat_id = _extract_dat_match_identity(row, lookup=lookup)
    if dat_id:
        sources['dat'] = dat_id

    tier = _resolve_sales_match_tier(sources)
    if tier == 'ok':
        row['매칭'] = '완료'
    else:
        row['매칭'] = '실패'
    row['_match_tier'] = tier
    return row['매칭']


def resolve_daily_sales_metrics(row, parsed=None, fuel_price=None, reparse_dat=False):
    """레거시 alias — resolve_sales_metrics와 동일."""
    return resolve_sales_metrics(row, parsed=parsed, fuel_price=fuel_price, reparse_dat=reparse_dat)


def resolve_sales_work_minutes(row, parsed=None, reparse_dat=False, daily_date=None):
    """영업시간: .dat #3 interval 합(분), 없으면 #4 duty·#1 구간 폴백.

    장기 마감은 daily_date(또는 행 날짜) 당일분만.
    """
    if parsed is None and reparse_dat:
        parsed = _load_sales_parsed_row(row)
    if parsed is not None:
        business_day = daily_date or str(row.get('날짜') or '').strip()[:10]
        if row.get('집계기준') == 'daily_split' or (
            is_prolonged_closing(parsed) and daily_date
        ):
            return compute_daily_sales_metrics(parsed, business_day)['work_minutes']
        return compute_closing_sales_metrics(parsed)['work_minutes']

    try:
        return max(0, int(row.get('영업시간') or row.get('영업분') or 0))
    except (ValueError, TypeError):
        return 0


def resolve_sales_vehicle_match(row, parsed=None, lookup=None):
    """행 날짜 해당 월 배차 기준으로 사번·이름·차종·매칭 보정."""
    car_suffix = str(row.get('차번') or '').strip()
    if not car_suffix:
        return {}

    plate = ''
    if parsed:
        plate = parsed.get('header', {}).get('plate', '') or ''
    if not plate:
        plate = str(row.get('차량번호') or '')

    business_date = row.get('날짜', '')
    dispatch_month = sales_dispatch_lookup_month(
        business_date=business_date, parsed=parsed, row=row,
        daily_date=str(business_date or '').strip()[:10]
        if str(row.get('집계기준') or '') == 'daily_split' else None,
    )
    if lookup is None:
        lookup = build_vehicle_lookup(dispatch_month=dispatch_month)
    daily_date = None
    if str(row.get('집계기준') or '') == 'daily_split':
        daily_date = str(business_date or '').strip()[:10]
    return match_vehicle_record(
        car_suffix, plate, lookup=lookup, business_date=business_date,
        parsed=parsed, row=row, daily_date=daily_date,
    )


def enrich_sales_row(row, parsed=None, fuel_price=None, reparse_dat=False, lookup=None, daily_date=None):
    """저장 행 보정.

    - .dat 업로드: 운행 지표만 계산 (실입금·건수는 T머니 CSV에서).
    - 조회/저장: JSON 수치 유지, 배차·매칭 상태만 갱신.
    """
    row.update(resolve_dat_metrics(
        row, parsed=parsed, fuel_price=fuel_price, reparse_dat=reparse_dat,
        daily_date=daily_date,
    ))
    row['영업시간'] = str(resolve_sales_work_minutes(
        row, parsed=parsed, reparse_dat=reparse_dat, daily_date=daily_date,
    ))
    if parsed is not None:
        row.setdefault('실입금', '0')
        row.setdefault('건수', '0')

    match_info = resolve_sales_vehicle_match(row, parsed=parsed, lookup=lookup)
    tmoney_source = str(row.get('T머니출처') or '').strip()
    csv_emp = normalize_emp_id(row.get('사번', ''))
    csv_name = str(row.get('이름') or '').strip()
    if match_info:
        if _is_tmoney_csv_matched(row) and csv_emp:
            row['사번'] = csv_emp
            if csv_name:
                row['이름'] = csv_name
            elif match_info.get('이름'):
                row['이름'] = match_info.get('이름', '')
            if _is_blank_sales_text(row.get('차종')):
                row['차종'] = match_info.get('차종', '')
            if not str(row.get('근무유형') or '').strip():
                row['근무유형'] = match_info.get('근무유형', '')
            if not str(row.get('차량번호') or '').strip() and match_info.get('차량번호'):
                row['차량번호'] = match_info['차량번호']
        else:
            row['사번'] = normalize_emp_id(match_info.get('사번', ''))
            row['이름'] = match_info.get('이름', '')
            row['차종'] = match_info.get('차종', '')
            row['근무유형'] = match_info.get('근무유형', '')
            if not parsed and match_info.get('차량번호'):
                row['차량번호'] = match_info['차량번호']
            elif parsed:
                header_plate = parsed.get('header', {}).get('plate', '')
                row['차량번호'] = header_plate or match_info.get('차량번호') or str(row.get('차번') or '')
    else:
        row['사번'] = normalize_emp_id(row.get('사번', ''))

    _snapshot_dat_match_identity(row, lookup=lookup)

    if not _is_tmoney_csv_matched(row) and tmoney_source in ('no_driver_match', 'no_tmoney'):
        row.pop('T머니출처', None)

    row.pop('영업건수', None)
    row.pop('연료L', None)
    row.pop('영업분', None)
    if parsed:
        header = parsed.get('header') or {}
        if daily_date or row.get('집계기준') == 'daily_split':
            row['집계기준'] = 'daily_split'
            row['마감시작'] = header.get('start', '')
            row['마감종료'] = header.get('end', '')
        else:
            row['집계기준'] = 'closing'
        file_date = parsed.get('file_date') or ''
        closing_date = parsed.get('closing_date') or ''
        if file_date and closing_date and file_date != closing_date:
            row['파일명일'] = file_date
    if _row_has_dat_source(row) and not _row_has_tmoney_source(row):
        row['실입금'] = '0'
        row['건수'] = '0'
    if str(row.get('집계기준') or '') == 'tmoney':
        row.pop('집계기준', None)
    row.pop('_cell_warn', None)
    compute_sales_match_status(row, lookup=lookup)
    _memo_sales_cell_warnings(row, lookup=lookup)
    return row


def _shift_band_for_sales_row(row) -> str:
    """행의 주·야 구분 — 출고 시각 우선, 없으면 근무유형."""
    band = infer_shift_band_from_start(str(row.get('영업시작') or '').strip())
    if band:
        return band
    work = str(row.get('근무유형') or '').strip()
    if work in _DISPATCH_DAY_WORK_TYPES:
        return 'day'
    if work in _DISPATCH_NIGHT_WORK_TYPES or work == '교대':
        return 'night'
    return ''


def _align_dat_rows_to_month_csv(month_rows: list[dict]) -> None:
    """당월 T머니 CSV 사번 기준으로 .dat-only 행 정렬 (전 기사 공통)."""
    csv_index: dict[tuple, dict] = {}
    for row in month_rows:
        if not _is_tmoney_csv_matched(row):
            continue
        date = str(row.get('날짜') or '').strip()
        car = str(row.get('차번') or '').strip()
        if not date or not car:
            continue
        band = _shift_band_for_sales_row(row)
        if band:
            csv_index[(date, car, band)] = row
        work = str(row.get('근무유형') or '').strip()
        if work:
            csv_index[(date, car, work)] = row

    for row in month_rows:
        if _is_tmoney_csv_matched(row) or not _row_has_dat_source(row):
            continue
        date = str(row.get('날짜') or '').strip()
        car = str(row.get('차번') or '').strip()
        band = _shift_band_for_sales_row(row)
        work = str(row.get('근무유형') or '').strip()
        csv_row = None
        for key in ((date, car, band), (date, car, work)):
            if key[2] and key in csv_index:
                csv_row = csv_index[key]
                break
        if not csv_row:
            continue
        csv_emp = normalize_emp_id(csv_row.get('사번', ''))
        if not csv_emp:
            continue
        row['사번'] = csv_emp
        if str(csv_row.get('이름') or '').strip():
            row['이름'] = csv_row['이름']
        if str(csv_row.get('근무유형') or '').strip():
            row['근무유형'] = csv_row['근무유형']
        if _is_blank_sales_text(row.get('차종')) and csv_row.get('차종'):
            row['차종'] = csv_row['차종']


def normalize_sales_data(data, reparse_dat=False):
    """sales_data 전체 행 표시용 필드 보정 (기본: JSON 판, 배차·매칭·중복 병합)."""
    if not data:
        return data
    from sales_reconcile import dedupe_sales_rows

    _vehicle_lookup_cache.clear()
    lookup_cache = {}
    for month in data.values():
        for row in month.get('data', []):
            dispatch_month = sales_dispatch_lookup_month(
                business_date=row.get('날짜', ''), row=row,
            )
            if dispatch_month not in lookup_cache:
                lookup_cache[dispatch_month] = build_vehicle_lookup(
                    dispatch_month=dispatch_month,
                )
            enrich_sales_row(
                row,
                reparse_dat=reparse_dat,
                lookup=lookup_cache[dispatch_month],
            )
        _align_dat_rows_to_month_csv(month.get('data', []))
        month['data'] = dedupe_sales_rows(month.get('data', []))
        for row in month['data']:
            dispatch_month = sales_dispatch_lookup_month(
                business_date=row.get('날짜', ''), row=row,
            )
            compute_sales_match_status(
                row, lookup=lookup_cache.get(dispatch_month),
            )
        month['summary'] = compute_sales_summary(month['data'])
    return data


def dat_parsed_to_sales_row(parsed, fuel_price=DEFAULT_FUEL_PRICE, lookup=None):
    header = parsed.get('header', {})
    car_suffix = (
        parsed.get('file_car_suffix')
        or header.get('car_suffix')
        or extract_car_suffix(header.get('plate', ''))
    )
    business_date = (
        parsed.get('closing_date')
        or resolve_closing_business_date(parsed)
        or parsed.get('file_date')
        or header.get('end', '')[:10]
    )
    if not business_date and parsed.get('trips'):
        d = parsed['trips'][0].get('date', '')
        if len(d) == 8:
            business_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    if lookup is None:
        lookup = build_vehicle_lookup(dispatch_month=sales_dispatch_month_key(business_date))

    match_info = match_vehicle_record(
        car_suffix, header.get('plate', ''), lookup=lookup,
        business_date=business_date, parsed=parsed,
    )
    business_start = header.get('start', '')
    business_end = header.get('end', '')

    return enrich_sales_row({
        '날짜': business_date or '',
        '차번': car_suffix,
        '차량번호': header.get('plate') or match_info['차량번호'] or car_suffix,
        '사번': match_info['사번'],
        '이름': match_info['이름'],
        '차종': match_info['차종'],
        '근무유형': match_info.get('근무유형', ''),
        '매칭': match_info['매칭'],
        '원본파일': parsed.get('source_file', ''),
        '영업시작': business_start,
        '영업종료': business_end,
        '연료단가': str(float(fuel_price)),
    }, parsed=parsed, fuel_price=fuel_price, lookup=lookup)


def dat_parsed_to_sales_rows(parsed, fuel_price=DEFAULT_FUEL_PRICE, lookup=None, parsed_context=None):
    """.dat → 수입금 행 목록. 장기 마감(3일+)은 달력일별로 분할(파일당 1일 1행)."""
    context = parsed_context if parsed_context is not None else [parsed]
    if not is_prolonged_closing(parsed):
        return [dat_parsed_to_sales_row(parsed, fuel_price=fuel_price, lookup=lookup)]

    header = parsed.get('header') or {}
    car_suffix = (
        parsed.get('file_car_suffix')
        or header.get('car_suffix')
        or extract_car_suffix(header.get('plate', ''))
    )
    closing_start = header.get('start', '')
    closing_end = header.get('end', '')
    rows = []

    for day in iter_closing_calendar_dates(parsed):
        if is_handshake_closing_day(parsed, day, context):
            continue

        dispatch_month = sales_dispatch_lookup_month(
            parsed=parsed, business_date=day, daily_date=day,
        )
        if lookup is None or sales_dispatch_month_key(day) != dispatch_month:
            lookup = build_vehicle_lookup(dispatch_month=dispatch_month)

        clipped_start = clip_datetime_to_business_day(closing_start, day, 'start')
        clipped_end = clip_datetime_to_business_day(closing_end, day, 'end')
        match_info = match_vehicle_record(
            car_suffix, header.get('plate', ''), lookup=lookup,
            business_date=day, parsed=parsed, daily_date=day,
            row={'영업시작': clipped_start},
        )
        metrics = compute_daily_sales_metrics(parsed, day)
        if not any((
            metrics['income_won'],
            metrics['fare_count'],
            metrics['work_minutes'],
            metrics['distance_km'],
            metrics['fuel_l'],
        )):
            continue

        row = enrich_sales_row({
            '날짜': day,
            '차번': car_suffix,
            '차량번호': header.get('plate') or match_info['차량번호'] or car_suffix,
            '사번': match_info['사번'],
            '이름': match_info['이름'],
            '차종': match_info['차종'],
            '근무유형': match_info.get('근무유형', ''),
            '매칭': match_info['매칭'],
            '원본파일': parsed.get('source_file', ''),
            '영업시작': clipped_start,
            '영업종료': clipped_end,
            '연료단가': str(float(fuel_price)),
            '집계기준': 'daily_split',
        }, parsed=parsed, fuel_price=fuel_price, lookup=lookup, daily_date=day)
        rows.append(row)

    if rows:
        return rows
    return [dat_parsed_to_sales_row(parsed, fuel_price=fuel_price, lookup=lookup)]


def build_dat_upload_sales_rows(parsed_list, fuel_price=DEFAULT_FUEL_PRICE, lookup_cache=None):
    """배치 업로드 — 장기 마감 일별 행 생성(핸드셰이크일·중복일 제외)."""
    if lookup_cache is None:
        lookup_cache = {}
    rows = []
    for parsed in parsed_list:
        if not is_prolonged_closing(parsed):
            closing_date = (
                parsed.get('closing_date')
                or resolve_closing_business_date(parsed)
                or parsed.get('file_date')
                or (parsed.get('header') or {}).get('end', '')[:10]
            )
            month_key = sales_dispatch_month_key(closing_date)
            if month_key not in lookup_cache:
                lookup_cache[month_key] = build_vehicle_lookup(dispatch_month=month_key)
            rows.extend(dat_parsed_to_sales_rows(
                parsed, fuel_price=fuel_price,
                lookup=lookup_cache[month_key], parsed_context=parsed_list,
            ))
            continue

        header = parsed.get('header') or {}
        car_suffix = (
            parsed.get('file_car_suffix')
            or header.get('car_suffix')
            or extract_car_suffix(header.get('plate', ''))
        )
        closing_start = header.get('start', '')
        closing_end = header.get('end', '')

        for day in iter_closing_calendar_dates(parsed):
            if is_handshake_closing_day(parsed, day, parsed_list):
                continue
            dispatch_month = sales_dispatch_lookup_month(
                parsed=parsed, business_date=day, daily_date=day,
            )
            if dispatch_month not in lookup_cache:
                lookup_cache[dispatch_month] = build_vehicle_lookup(dispatch_month=dispatch_month)
            lookup = lookup_cache[dispatch_month]

            clipped_start = clip_datetime_to_business_day(closing_start, day, 'start')
            clipped_end = clip_datetime_to_business_day(closing_end, day, 'end')
            match_info = match_vehicle_record(
                car_suffix, header.get('plate', ''), lookup=lookup,
                business_date=day, parsed=parsed, daily_date=day,
                row={'영업시작': clipped_start},
            )
            metrics = compute_daily_sales_metrics(parsed, day)
            if not any((
                metrics['income_won'],
                metrics['fare_count'],
                metrics['work_minutes'],
                metrics['distance_km'],
                metrics['fuel_l'],
            )):
                continue

            rows.append(enrich_sales_row({
                '날짜': day,
                '차번': car_suffix,
                '차량번호': header.get('plate') or match_info['차량번호'] or car_suffix,
                '사번': match_info['사번'],
                '이름': match_info['이름'],
                '차종': match_info['차종'],
                '근무유형': match_info.get('근무유형', ''),
                '매칭': match_info['매칭'],
                '원본파일': parsed.get('source_file', ''),
                '영업시작': clipped_start,
                '영업종료': clipped_end,
                '연료단가': str(float(fuel_price)),
                '집계기준': 'daily_split',
            }, parsed=parsed, fuel_price=fuel_price, lookup=lookup, daily_date=day))

    return rows


def _sales_source_basename(row) -> str:
    return os.path.basename(str(row.get('원본파일') or '').replace('\\', '/'))


def _purge_sales_rows_by_source(data, source_file: str):
    """동일 .dat 재업로드 시 이전 행(장기 마감 분할·단일 마감 포함) 제거."""
    basename = os.path.basename(str(source_file or '').replace('\\', '/'))
    if not basename:
        return

    def row_has_source(row) -> bool:
        raw = str(row.get('원본파일') or '')
        parts = [os.path.basename(p.strip().replace('\\', '/')) for p in raw.split(',') if p.strip()]
        return basename in parts

    for month_data in data.values():
        rows = month_data.get('data', [])
        month_data['data'] = [r for r in rows if not row_has_source(r)]


def _daily_split_identity(row):
    if str(row.get('집계기준') or '') != 'daily_split':
        return None
    date = str(row.get('날짜') or '').strip()
    car = str(row.get('차번') or '').strip()
    if not date or not car:
        return None
    emp = normalize_emp_id(row.get('사번', ''))
    if emp:
        return (date, car, emp)
    return (date, car)



def _sales_month_sort_key(month_label):
    match = re.match(r'(\d{1,2})', str(month_label))
    return int(match.group(1)) if match else 99


def sort_sales_data_by_month(data):
    """월별 탭을 01월~12월 순으로 정렬."""
    if not data:
        return data
    return OrderedDict(sorted(data.items(), key=lambda item: _sales_month_sort_key(item[0])))


def save_sales_data(data, normalize=True):
    filepath = os.path.join(app.config['DATA_FOLDER'], 'sales_data.json')
    data = sort_sales_data_by_month(data)
    if normalize:
        data = normalize_sales_data(data)
    _strip_ephemeral_sales_row_fields(data)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


SALES_EDITABLE_FIELDS = (
    '실입금', '건수', '영업시간', '빈차시간', '연료비', '충전량', '운행거리', '빈차거리',
)


def _normalize_sales_edit_field(field, value):
    text = str(value or '').strip().replace(',', '')
    if field in ('충전량', '운행거리', '빈차거리', '총거리'):
        return str(round(float(text or 0), 2))
    if field in ('영업시간', '영업분', '총시간', '빈차시간'):
        return str(max(0, int(float(text or 0))))
    return str(max(0, int(float(text or 0))))


def _apply_sales_derived_totals(row):
    """수동 수정 후 총시간·총거리를 구성 항목 합으로 맞춤."""
    try:
        work_minutes = max(0, int(float(row.get('영업시간') or 0)))
    except (ValueError, TypeError):
        work_minutes = 0
    try:
        empty_minutes = max(0, int(float(row.get('빈차시간') or 0)))
    except (ValueError, TypeError):
        empty_minutes = 0
    row['총시간'] = str(work_minutes + empty_minutes)

    try:
        running_km = round(float(row.get('운행거리') or 0), 2)
    except (ValueError, TypeError):
        running_km = 0.0
    try:
        empty_km = round(float(row.get('빈차거리') or 0), 2)
    except (ValueError, TypeError):
        empty_km = 0.0
    row['총거리'] = str(round(running_km + empty_km, 2))


def _sales_edit_value_equal(field, old_val, new_val):
    try:
        return (
            _normalize_sales_edit_field(field, old_val)
            == _normalize_sales_edit_field(field, new_val)
        )
    except (ValueError, TypeError):
        return str(old_val or '').strip() == str(new_val or '').strip()


def _mark_sales_row_modified(row, fields):
    if not fields:
        return
    existing = set(row.get('_modified_fields') or [])
    existing.update(fields)
    row['_modified_fields'] = sorted(existing)


def _mark_sales_row_field_edits(row, fields, edit_entry):
    """필드별 수정 세션·편집자 메타데이터 저장."""
    if not fields or not edit_entry:
        return
    existing = row.get('_field_edits') or {}
    if not isinstance(existing, dict):
        existing = {}
    meta_base = {
        'session_id': str(edit_entry.get('id') or '').strip(),
        'editor': str(edit_entry.get('editor') or '').strip(),
        'date': str(edit_entry.get('date') or '').strip(),
        'time': str(edit_entry.get('time') or '').strip(),
    }
    for field in fields:
        existing[field] = dict(meta_base)
    row['_field_edits'] = existing
    _mark_sales_row_modified(row, fields)


def _apply_sales_row_field_updates(row, item, edit_entry=None):
    """수동 편집 필드를 행에 반영하고 이번 저장에서 변경된 필드 목록을 반환."""
    changed = []
    for field in SALES_EDITABLE_FIELDS:
        if field in item:
            new_val = _normalize_sales_edit_field(field, item[field])
        elif field == '영업시간' and '영업분' in item:
            new_val = _normalize_sales_edit_field('영업시간', item['영업분'])
        else:
            continue
        old_val = row.get(field, '')
        if not _sales_edit_value_equal(field, old_val, new_val):
            changed.append(field)
        row[field] = new_val
    _apply_sales_derived_totals(row)
    row.pop('영업분', None)
    derived = []
    if '영업시간' in changed or '빈차시간' in changed:
        derived.append('총시간')
    if '운행거리' in changed or '빈차거리' in changed:
        derived.append('총거리')
    all_changed = changed + derived
    if all_changed and edit_entry:
        _mark_sales_row_field_edits(row, all_changed, edit_entry)
    elif all_changed:
        _mark_sales_row_modified(row, all_changed)
    return all_changed


def _sales_editor_name(user=None):
    """수입금 수동 수정 편집자 표시명 — DB name 우선, 없으면 username."""
    user = user or current_user
    try:
        if not getattr(user, 'is_authenticated', False):
            return ''
    except Exception:
        return ''
    try:
        user_id = user.get_id()
        if user_id:
            db_user = User.query.get(int(user_id))
            if db_user:
                name = str(db_user.name or '').strip()
                if name:
                    return name
                return str(db_user.username or '').strip()
    except Exception:
        pass
    name = str(getattr(user, 'name', None) or '').strip()
    if name:
        return name
    return str(getattr(user, 'username', None) or '').strip()


def _record_sales_month_last_edit(month_data, editor_name):
    """월별 수입금 표 수동 수정 메타데이터 저장 (최근 2건)."""
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)
    entry = {
        'id': now.strftime('%Y%m%d%H%M%S%f'),
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M'),
        'editor': editor_name,
    }
    history = list(month_data.get('edit_history') or [])
    if not history:
        legacy = month_data.get('last_edit')
        if isinstance(legacy, dict) and (legacy.get('date') or legacy.get('editor')):
            legacy_entry = dict(legacy)
            if not legacy_entry.get('id'):
                legacy_entry['id'] = (
                    str(legacy_entry.get('date') or '').replace('-', '')
                    + str(legacy_entry.get('time') or '').replace(':', '')
                )
            history.append(legacy_entry)
    history.insert(0, entry)
    month_data['edit_history'] = history[:2]
    month_data['last_edit'] = month_data['edit_history'][0]
    return entry


def apply_sales_row_updates(updates, editor_name=None):
    """수입금 표에서 수정한 행을 sales_data.json에 반영 (raw load/save — normalize 생략)."""
    data = _read_sales_data_raw()
    if not data:
        return False, '저장된 수입금 데이터가 없습니다.'
    if not updates:
        return False, '변경된 항목이 없습니다.'

    updated_count = 0
    updated_months = set()
    field_changes = []
    month_edit_entries = {}
    for item in updates:
        month = str(item.get('month') or item.get('월') or '').strip()
        date = str(item.get('날짜', '')).strip()
        car = str(item.get('차번', '')).strip()
        if not month or not date or not car:
            continue

        month_data = data.get(month)
        if not month_data:
            continue

        item_matched = False
        for row in month_data.get('data', []):
            if not _sales_row_matches_update(row, item):
                continue
            item_matched = True
            changed = _apply_sales_row_field_updates(row, item)
            if changed:
                if month not in month_edit_entries:
                    editor = str(editor_name or _sales_editor_name()).strip() or '알 수 없음'
                    month_edit_entries[month] = _record_sales_month_last_edit(month_data, editor)
                _mark_sales_row_field_edits(row, changed, month_edit_entries[month])
                field_meta = {}
                field_edits = row.get('_field_edits') or {}
                if isinstance(field_edits, dict):
                    for field_name in changed:
                        if field_name in field_edits:
                            field_meta[field_name] = field_edits[field_name]
                field_changes.append({
                    'month': month,
                    '날짜': date,
                    '차번': car,
                    '사번': normalize_emp_id(item.get('사번', '')),
                    '영업시작': str(item.get('영업시작') or '').strip()[:16],
                    '근무유형': str(item.get('근무유형') or '').strip(),
                    'fields': changed,
                    'field_meta': field_meta,
                })
                updated_count += 1
                updated_months.add(month)
            break
        if not item_matched:
            return False, '일치하는 행을 찾지 못했습니다.'

    if updated_count == 0:
        return False, '변경된 항목이 없습니다.'

    last_edits = {}
    edit_histories = {}
    for month_key in updated_months:
        month_data = data.get(month_key)
        if month_data:
            month_data['summary'] = compute_sales_summary(month_data.get('data', []))
            last_edits[month_key] = month_data.get('last_edit')
            edit_histories[month_key] = _sales_month_edit_history(month_data)

    save_sales_data(data, normalize=False)
    return True, {
        'updated': updated_count,
        'last_edits': last_edits,
        'edit_histories': edit_histories,
        'field_changes': field_changes,
    }


def _read_sales_data_raw():
    """sales_data.json 읽기 (normalize 없음 — 업로드 배치용)."""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'sales_data.json')
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    return sort_sales_data_by_month(json.loads(content))
        except (json.JSONDecodeError, Exception) as e:
            print(f"sales_data.json 읽기 오류: {e}")
    return None


def load_sales_data():
    data = _read_sales_data_raw()
    if data is None:
        return None
    before_rows = sum(len(m.get('data', [])) for m in data.values())
    needs_repair = any(
        str(row.get('T머니출처') or '') in ('no_driver_match', 'no_tmoney')
        for month in data.values()
        for row in month.get('data', [])
    )
    normalized = normalize_sales_data(data)
    after_rows = sum(len(m.get('data', [])) for m in normalized.values())
    if needs_repair or after_rows < before_rows:
        save_sales_data(normalized, normalize=False)
    return normalized


def merge_sales_records(existing, new_rows):
    """월별 sales_data에 신규 행 병합.

    같은 (날짜·차번·사번)이면 T머니·.dat 항목을 한 행으로 합칩니다.
    """
    from sales_reconcile import merge_complementary_sales_rows

    if existing is None:
        existing = OrderedDict()
    if not isinstance(existing, OrderedDict):
        existing = OrderedDict(existing)

    for row in new_rows:
        date_str = row.get('날짜', '')
        if not date_str or len(date_str) < 7:
            continue
        month_key = f"{date_str[5:7]}월"
        if month_key not in existing:
            existing[month_key] = {'data': [], 'summary': {}}

        month_rows = existing[month_key]['data']
        daily_id = _daily_split_identity(row)
        if daily_id:
            replaced = False
            for idx, existing_row in enumerate(month_rows):
                if _daily_split_identity(existing_row) == daily_id:
                    month_rows[idx] = merge_complementary_sales_rows(existing_row, row)
                    replaced = True
                    break
            if not replaced:
                month_rows.append(row)
        else:
            key = sales_record_key(row)
            replaced = False
            for idx, existing_row in enumerate(month_rows):
                if sales_record_key(existing_row) == key:
                    month_rows[idx] = merge_complementary_sales_rows(existing_row, row)
                    replaced = True
                    break
            if not replaced:
                month_rows.append(row)

        existing[month_key]['data'] = sorted(
            month_rows,
            key=lambda r: (
                r.get('날짜', ''),
                r.get('차번', ''),
                r.get('영업시작', ''),
                r.get('근무유형', ''),
                r.get('사번', ''),
            ),
        )
        existing[month_key]['summary'] = compute_sales_summary(existing[month_key]['data'])

    return sort_sales_data_by_month(existing)


def _remove_dat_files(filenames):
    """JSON 저장 완료 후 uploads/dat 임시 .dat 파일 삭제."""
    for name in filenames:
        if not name:
            continue
        filepath = os.path.join(
            app.config['DAT_FOLDER'],
            os.path.basename(str(name).replace('/', '').replace('\\', '')),
        )
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except OSError as e:
            print(f'.dat 삭제 실패 ({name}): {e}')


def _remove_tmoney_csv_file(filename):
    """JSON 저장·CSV 대조 완료 후 uploads 폴더 임시 CSV 삭제."""
    if not filename:
        return
    filepath = os.path.join(
        app.config['UPLOAD_FOLDER'],
        os.path.basename(str(filename).replace('/', '').replace('\\', '')),
    )
    try:
        if os.path.isfile(filepath):
            os.remove(filepath)
    except OSError as e:
        print(f'T머니 CSV 삭제 실패 ({filename}): {e}')


def process_dat_files(files, fuel_price=DEFAULT_FUEL_PRICE, existing=None, persist=True, normalize_on_save=False):
    """dat 파일 목록 파싱·병합 → JSON 저장 후 .dat 임시 파일 삭제."""
    if existing is None:
        existing = _read_sales_data_raw()

    lookup_cache = {}
    parsed_list = []
    saved_names = []

    for file in files:
        if not file or not file.filename:
            continue
        if not allowed_dat_file(file.filename):
            continue
        filename = secure_filename(file.filename.replace('/', '').replace('\\', ''))
        filepath = os.path.join(app.config['DAT_FOLDER'], filename)
        raw = file.read()
        file.seek(0)
        with open(filepath, 'wb') as out:
            out.write(raw)

        parsed = parse_dat_bytes(raw, filename)
        parsed_list.append(parsed)
        _purge_sales_rows_by_source(existing, filename)
        saved_names.append(filename)

    new_rows = build_dat_upload_sales_rows(
        parsed_list, fuel_price=fuel_price, lookup_cache=lookup_cache,
    ) if parsed_list else []

    if not new_rows:
        return None, [], '유효한 .dat 파일이 없습니다.'

    merged = merge_sales_records(existing, new_rows)
    if persist:
        save_sales_data(merged, normalize=normalize_on_save)
        _remove_dat_files(saved_names)
    return merged, saved_names, None


def process_dat_upload(files, fuel_price=DEFAULT_FUEL_PRICE):
    return process_dat_files(files, fuel_price=fuel_price)

ACCIDENT_DEFAULT_YEAR_PREFIX = '26'


def normalize_accident_no(value, default_year_prefix=ACCIDENT_DEFAULT_YEAR_PREFIX):
    """사고번호를 YY-G01 / YY-P01 형식으로 통일 (레거시 G01·P01 → 26-G01)."""
    s = str(value or '').strip()
    if not s or s.lower() in ('nan', 'none'):
        return ''
    m = re.match(r'^(\d{2})-([GP])(\d+)$', s, re.I)
    if m:
        return f'{m.group(1)}-{m.group(2).upper()}{m.group(3)}'
    m = re.match(r'^([GP])(\d+)$', s, re.I)
    if m:
        return f'{default_year_prefix}-{m.group(1).upper()}{m.group(2)}'
    return s


def accident_map_key(accident_no):
    """약도 json/png 파일명 (소문자). 예: 26-G01 → 26-g01"""
    return normalize_accident_no(accident_no).lower()


def normalize_accident_record(record):
    if not record:
        return record
    if '사고번호' in record:
        record['사고번호'] = normalize_accident_no(record.get('사고번호'))
    return record


def normalize_accident_data(data):
    """at_fault / not_at_fault 목록의 사고번호 정규화."""
    if not data:
        return data
    changed = False
    for key in ('at_fault', 'not_at_fault'):
        rows = data.get(key, [])
        for row in rows:
            old = str(row.get('사고번호', '') or '')
            normalize_accident_record(row)
            if str(row.get('사고번호', '') or '') != old:
                changed = True
    return changed


def find_accident_by_no(accidents, accident_no):
    target = normalize_accident_no(accident_no)
    if not target:
        return None
    for row in accidents:
        if normalize_accident_no(row.get('사고번호')) == target:
            return row
    return None


def extract_accident_district(location):
    """사고장소 문자열에서 구(區) 이름 추출."""
    loc = str(location or '').strip()
    if not loc:
        return '기타'
    m = re.search(r'([가-힣]+구)', loc)
    return m.group(1) if m else '기타'


def build_accident_chart_stats(at_fault_data, not_at_fault_data):
    """사고 유형·사고장소(구)별 파이 차트용 집계."""
    type_chart = {
        'labels': ['가해사고', '피해사고'],
        'values': [len(at_fault_data), len(not_at_fault_data)],
    }
    district_counts = {}
    for accident in at_fault_data + not_at_fault_data:
        gu = extract_accident_district(accident.get('사고장소', ''))
        district_counts[gu] = district_counts.get(gu, 0) + 1
    sorted_districts = sorted(district_counts.items(), key=lambda item: (-item[1], item[0]))
    district_chart = {
        'labels': [name for name, _ in sorted_districts],
        'values': [count for _, count in sorted_districts],
    }
    return {'type_chart': type_chart, 'district_chart': district_chart}


def build_accident_stats_by_month(at_fault_data, not_at_fault_data, month_order=None):
    """월별 사고 현황(가해/피해) 및 사고원인별 분포 집계."""
    if month_order is None:
        month_order = ['01월', '02월', '03월', '04월', '05월', '06월', '07월', '08월', '09월', '10월', '11월', '12월']
    stats = {
        m: {'at_fault': 0, 'at_fault_causes': {}, 'not_at_fault': 0, 'not_at_fault_causes': {}}
        for m in month_order
    }
    for a in at_fault_data:
        dt = a.get('사고일시', '')
        if dt and '/' in dt:
            try:
                mm = dt.strip().split('/')[0].zfill(2)
                month_key = f'{mm}월'
                if month_key in stats:
                    stats[month_key]['at_fault'] += 1
                    cause = (a.get('사고원인', '') or '').strip() or '기타'
                    stats[month_key]['at_fault_causes'][cause] = stats[month_key]['at_fault_causes'].get(cause, 0) + 1
            except Exception:
                pass
    for a in not_at_fault_data:
        dt = a.get('사고일시', '')
        if dt and '/' in dt:
            try:
                mm = dt.strip().split('/')[0].zfill(2)
                month_key = f'{mm}월'
                if month_key in stats:
                    stats[month_key]['not_at_fault'] += 1
                    cause = (a.get('사고원인', '') or '').strip() or '기타'
                    stats[month_key]['not_at_fault_causes'][cause] = stats[month_key]['not_at_fault_causes'].get(cause, 0) + 1
            except Exception:
                pass
    return stats


def get_maps_dirs():
    dirs = [os.path.join(app.config['UPLOAD_FOLDER'], 'maps')]
    if os.environ.get('CLOUDTYPE_ENV'):
        dirs.append('/tmp/uploads/maps')
    return dirs


def migrate_legacy_map_files(default_year_prefix=ACCIDENT_DEFAULT_YEAR_PREFIX):
    """g01.png → 26-g01.png 등 레거시 약도 파일명 마이그레이션."""
    for maps_dir in get_maps_dirs():
        if not os.path.isdir(maps_dir):
            continue
        for name in os.listdir(maps_dir):
            m = re.match(r'^([gp])(\d+)\.(json|png)$', name, re.I)
            if not m:
                continue
            new_name = f'{default_year_prefix}-{m.group(1).lower()}{m.group(2)}.{m.group(3)}'
            if new_name == name:
                continue
            old_path = os.path.join(maps_dir, name)
            new_path = os.path.join(maps_dir, new_name)
            if os.path.exists(new_path):
                continue
            os.rename(old_path, new_path)
            print(f'약도 파일명 변경: {name} → {new_name}')


def migrate_accident_data_file():
    store = load_accident_data_store()
    if not store:
        return
    changed = False
    for payload in store.values():
        if normalize_accident_data(payload):
            changed = True
    if changed:
        filepath = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        print('accident_data.json 사고번호 형식 마이그레이션 완료')


def init_accident_migrations():
    """사고번호·약도 파일 레거시 마이그레이션."""
    with app.app_context():
        migrate_legacy_map_files()
        migrate_accident_data_file()


def _is_accident_payload(data):
    return isinstance(data, dict) and ('at_fault' in data or 'not_at_fault' in data)


def _coerce_accident_store(raw):
    if not raw or not isinstance(raw, dict):
        return {}
    if _is_accident_payload(raw):
        return {str(datetime.now().year): raw}
    store = {}
    for year_key, payload in raw.items():
        if isinstance(payload, dict) and _is_accident_payload(payload):
            store[str(year_key)] = payload
    return store


def load_accident_data_store():
    filepath = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return {}
            raw = json.loads(content)
            store = _coerce_accident_store(raw)
            if _is_accident_payload(raw):
                with open(filepath, 'w', encoding='utf-8') as wf:
                    json.dump(store, wf, ensure_ascii=False, indent=2)
            return store
    except Exception as e:
        print(f'accident_data.json 읽기 오류: {e}')
        return {}


def _enrich_accident_payload(data):
    if not data:
        return data
    normalize_accident_data(data)
    if not ('at_fault' in data or 'not_at_fault' in data):
        return data

    at_fault_data = data.get('at_fault', [])
    not_at_fault_data = data.get('not_at_fault', [])

    total_count = len(at_fault_data) + len(not_at_fault_data)
    at_fault_count = len(at_fault_data)
    not_at_fault_count = len(not_at_fault_data)
    at_fault_pending_count = sum(1 for a in at_fault_data if a.get('처리여부', '') == '미결')
    not_at_fault_pending_count = sum(1 for a in not_at_fault_data if a.get('처리여부', '') == '미결')

    def parse_amount(amount_str):
        if not amount_str or amount_str == '' or amount_str == '-':
            return 0
        try:
            return int(str(amount_str).replace(',', ''))
        except Exception:
            return 0

    at_fault_total_repair = sum(parse_amount(a.get('수리지급', 0)) for a in at_fault_data)
    at_fault_total_treatment = sum(parse_amount(a.get('치료지급', 0)) for a in at_fault_data)
    not_at_fault_total_damage = sum(parse_amount(a.get('피해견적', 0)) for a in not_at_fault_data)
    not_at_fault_total_payment = sum(parse_amount(a.get('금액', 0)) for a in not_at_fault_data)

    driver_stats = {}
    for accident in at_fault_data:
        driver_name = accident.get('기사명', '')
        if driver_name:
            if driver_name not in driver_stats:
                driver_stats[driver_name] = {
                    'name': driver_name,
                    'at_fault_count': 0,
                    'repair_payment': 0,
                    'treatment_payment': 0,
                    'not_at_fault_count': 0,
                    'damage_estimate': 0,
                }
            driver_stats[driver_name]['at_fault_count'] += 1
            driver_stats[driver_name]['repair_payment'] += parse_amount(accident.get('수리지급', 0))
            driver_stats[driver_name]['treatment_payment'] += parse_amount(accident.get('치료지급', 0))

    for accident in not_at_fault_data:
        driver_name = accident.get('기사명', '')
        if driver_name:
            if driver_name not in driver_stats:
                driver_stats[driver_name] = {
                    'name': driver_name,
                    'at_fault_count': 0,
                    'repair_payment': 0,
                    'treatment_payment': 0,
                    'not_at_fault_count': 0,
                    'damage_estimate': 0,
                }
            driver_stats[driver_name]['not_at_fault_count'] += 1
            driver_stats[driver_name]['damage_estimate'] += parse_amount(accident.get('피해견적', 0))

    vehicle_stats = {}
    for accident in at_fault_data:
        vehicle_number = accident.get('차번', '')
        if vehicle_number:
            if vehicle_number not in vehicle_stats:
                vehicle_stats[vehicle_number] = {
                    'number': vehicle_number,
                    'at_fault_count': 0,
                    'not_at_fault_count': 0,
                    'damage_estimate': 0,
                }
            vehicle_stats[vehicle_number]['at_fault_count'] += 1

    for accident in not_at_fault_data:
        vehicle_number = accident.get('차번', '')
        if vehicle_number:
            if vehicle_number not in vehicle_stats:
                vehicle_stats[vehicle_number] = {
                    'number': vehicle_number,
                    'at_fault_count': 0,
                    'not_at_fault_count': 0,
                    'damage_estimate': 0,
                }
            vehicle_stats[vehicle_number]['not_at_fault_count'] += 1
            vehicle_stats[vehicle_number]['damage_estimate'] += parse_amount(accident.get('피해견적', 0))

    def format_amount(amount):
        return f"{amount:,}" if amount > 0 else "0"

    for driver in driver_stats.values():
        driver['repair_payment'] = format_amount(driver['repair_payment'])
        driver['treatment_payment'] = format_amount(driver['treatment_payment'])
        driver['damage_estimate'] = format_amount(driver['damage_estimate'])

    for vehicle in vehicle_stats.values():
        vehicle['damage_estimate'] = format_amount(vehicle['damage_estimate'])

    chart_stats = build_accident_chart_stats(at_fault_data, not_at_fault_data)
    data['summary'] = {
        'total_count': total_count,
        'at_fault_count': at_fault_count,
        'not_at_fault_count': not_at_fault_count,
        'at_fault_pending_count': at_fault_pending_count,
        'not_at_fault_pending_count': not_at_fault_pending_count,
        'at_fault_total_repair': format_amount(at_fault_total_repair),
        'at_fault_total_treatment': format_amount(at_fault_total_treatment),
        'not_at_fault_total_damage': format_amount(not_at_fault_total_damage),
        'not_at_fault_total_payment': format_amount(not_at_fault_total_payment),
        'driver_stats': list(driver_stats.values()),
        'vehicle_stats': list(vehicle_stats.values()),
        'type_chart': chart_stats['type_chart'],
        'district_chart': chart_stats['district_chart'],
    }
    return data


def load_accident_data_merged():
    store = load_accident_data_store()
    merged = {'at_fault': [], 'not_at_fault': []}
    for payload in store.values():
        merged['at_fault'].extend(payload.get('at_fault', []))
        merged['not_at_fault'].extend(payload.get('not_at_fault', []))
    return merged


def find_accident_in_all_years(accident_no, list_type='at_fault'):
    merged = load_accident_data_merged()
    key = 'at_fault' if list_type == 'at_fault' else 'not_at_fault'
    return find_accident_by_no(merged.get(key, []), accident_no)


def _accident_page_context(selected_year=None):
    store = load_accident_data_store()
    years = sorted([int(year_key) for year_key in store.keys()], reverse=True) if store else []

    if selected_year is None:
        query_year = request.args.get('year', type=int)
        if not query_year and request.method == 'POST':
            query_year = request.form.get('view_year', type=int)
        if query_year and query_year in years:
            selected_year = query_year
        elif years:
            selected_year = years[0]
        else:
            selected_year = datetime.now().year
    elif years and selected_year not in years:
        selected_year = years[0]

    accident_data = load_accident_data(selected_year if store else None)
    month_order = ['01월', '02월', '03월', '04월', '05월', '06월', '07월', '08월', '09월', '10월', '11월', '12월']
    at_fault_rows = accident_data.get('at_fault', []) if accident_data else []
    not_at_fault_rows = accident_data.get('not_at_fault', []) if accident_data else []
    accident_stats_by_month = build_accident_stats_by_month(at_fault_rows, not_at_fault_rows, month_order)

    return {
        'accident_data': accident_data,
        'accident_years': years,
        'selected_accident_year': selected_year if years else None,
        'month_order': month_order,
        'accident_stats_by_month': accident_stats_by_month,
    }


def save_accident_data(data, year=None):
    print("=== save_accident_data 함수 시작 ===")
    if year is None:
        year = datetime.now().year
    enriched = _enrich_accident_payload(data)
    store = load_accident_data_store()
    store[str(year)] = enriched
    filepath = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
    print(f"JSON 저장 경로: {filepath}")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    print("JSON 파일 저장 완료")


def load_accident_data(year=None):
    """저장된 사고 데이터를 불러옴"""
    store = load_accident_data_store()
    if not store:
        return None
    if year is None:
        year = _default_dispatch_year(store.keys())
    data = store.get(str(year))
    if not data:
        return None
    return _enrich_accident_payload(data)

@app.route('/map')
@login_required
def map():
    return render_template('map.html')

# 운전기사 데이터 저장/불러오기 함수

DRIVER_DATA_COLUMNS = [
    '사번', '이름', '근무유형', '나이', '주민등록번호', '면허번호',
    '갱신시작', '갱신마감', '입사일자', '퇴사일자', '연락처', '거주지',
]


def normalize_driver_data(data):
    """기사 JSON 컬럼·사번 형식 보정 (레거시 데이터 포함)."""
    if not data or not isinstance(data, dict):
        return data
    data['columns'] = list(DRIVER_DATA_COLUMNS)
    for row in data.get('list', []):
        for col in DRIVER_DATA_COLUMNS:
            if col not in row:
                row[col] = ''
        if row.get('사번'):
            row['사번'] = normalize_emp_id(row['사번'])
        row['근무유형'] = str(row.get('근무유형', '') or '').strip()
    return data


def save_driver_data(data, year=None):
    print("=== save_driver_data 함수 시작 ===")
    filepath = os.path.join(app.config['DATA_FOLDER'], 'driver_data.json')
    print(f"JSON 저장 경로: {filepath}")
    if year is None:
        year = datetime.now().year
    store = load_driver_data_store()
    store[str(year)] = normalize_driver_data(data)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    print("JSON 파일 저장 완료")


def _coerce_driver_store(raw):
    if not raw or not isinstance(raw, dict):
        return {}
    if 'list' in raw:
        year = raw.get('year') or datetime.now().year
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = datetime.now().year
        return {str(year): normalize_driver_data(raw)}
    store = {}
    for year_key, payload in raw.items():
        if isinstance(payload, dict) and 'list' in payload:
            store[str(year_key)] = normalize_driver_data(payload)
    return store


def load_driver_data_store():
    filepath = os.path.join(app.config['DATA_FOLDER'], 'driver_data.json')
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                print("driver_data.json 파일이 비어있습니다.")
                return {}
            raw = json.loads(content)
            store = _coerce_driver_store(raw)
            if 'list' in raw:
                with open(filepath, 'w', encoding='utf-8') as wf:
                    json.dump(store, wf, ensure_ascii=False, indent=2)
            return store
    except json.JSONDecodeError as e:
        print(f"driver_data.json JSON 파싱 오류: {e}")
        return {}
    except Exception as e:
        print(f"driver_data.json 읽기 오류: {e}")
        return {}


def load_driver_data(year=None):
    store = load_driver_data_store()
    if not store:
        return None
    if year is None:
        year = _default_dispatch_year(store.keys())
    payload = store.get(str(year))
    if not payload:
        return None
    return normalize_driver_data(payload)


def load_all_driver_records():
    store = load_driver_data_store()
    records = []
    for payload in store.values():
        data = normalize_driver_data(payload)
        records.extend(data.get('list', []))
    return records


def find_driver_by_emp_id(driver_id):
    target = normalize_emp_id(driver_id)
    for driver in load_all_driver_records():
        if normalize_emp_id(driver.get('사번', '')) == target:
            return driver
    return None


def _driver_page_context(selected_year=None):
    store = load_driver_data_store()
    years = sorted([int(year_key) for year_key in store.keys()], reverse=True) if store else []

    if selected_year is None:
        query_year = request.args.get('year', type=int)
        if not query_year and request.method == 'POST':
            query_year = request.form.get('view_year', type=int)
        if query_year and query_year in years:
            selected_year = query_year
        elif years:
            selected_year = years[0]
        else:
            selected_year = datetime.now().year
    elif years and selected_year not in years:
        selected_year = years[0]

    return {
        'driver_data': load_driver_data(selected_year if store else None),
        'driver_years': years,
        'selected_driver_year': selected_year if years else None,
    }


def _coerce_maintenance_store(raw):
    if not raw or not isinstance(raw, dict):
        return {}
    if 'events' in raw:
        year = raw.get('year') or datetime.now().year
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = datetime.now().year
        return {str(year): {'events': raw.get('events') or []}}
    store = {}
    for year_key, payload in raw.items():
        if isinstance(payload, dict) and 'events' in payload:
            store[str(year_key)] = {'events': payload.get('events') or []}
        elif isinstance(payload, list):
            store[str(year_key)] = {'events': payload}
    return store


def load_car_maintenance_events_store():
    filepath = os.path.join(app.config['DATA_FOLDER'], 'car_maintenance_data.json')
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return {}
            raw = json.loads(content)
            store = _coerce_maintenance_store(raw)
            if 'events' in raw:
                with open(filepath, 'w', encoding='utf-8') as wf:
                    json.dump(store, wf, ensure_ascii=False, indent=2)
            return store
    except (json.JSONDecodeError, Exception):
        return {}


def save_car_maintenance_events(events, year=None):
    if year is None and events:
        try:
            year = int(events[0].get('date', '')[:4])
        except (ValueError, TypeError):
            year = datetime.now().year
    elif year is None:
        year = datetime.now().year
    store = load_car_maintenance_events_store()
    store[str(year)] = {'events': _normalize_maintenance_events(events or [])}
    filepath = os.path.join(app.config['DATA_FOLDER'], 'car_maintenance_data.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def load_car_maintenance_events(year=None):
    store = load_car_maintenance_events_store()
    if not store:
        return [], datetime.now().year
    if year is None:
        year = _default_dispatch_year(store.keys())
    payload = store.get(str(year), {})
    events = _normalize_maintenance_events(payload.get('events') or [])
    return events, int(year)


def load_all_car_maintenance_events():
    store = load_car_maintenance_events_store()
    events = []
    for payload in store.values():
        events.extend(_normalize_maintenance_events(payload.get('events') or []))
    return events


def _build_car_maintenance_context(selected_year=None, requested_month=None):
    store = load_car_maintenance_events_store()
    years = sorted([int(year_key) for year_key in store.keys()], reverse=True) if store else []

    if selected_year is None:
        query_year = request.args.get('year', type=int)
        if not query_year and request.method == 'POST':
            query_year = request.form.get('view_year', type=int)
        if query_year and query_year in years:
            selected_year = query_year
        elif years:
            selected_year = years[0]
        else:
            selected_year = datetime.now().year
    elif years and selected_year not in years:
        selected_year = years[0]

    events, maintenance_year = load_car_maintenance_events(selected_year if store else None)
    maintenance_headers = []
    maintenance_table_data = []
    maintenance_available_months = []
    maintenance_selected_month = None

    if events:
        maintenance_available_months = [f'{maintenance_year}-{m:02d}' for m in range(1, 13)]
        if requested_month is None:
            requested_month = request.args.get('month')
            if not requested_month and request.method == 'POST':
                requested_month = request.form.get('view_month')
        if requested_month and requested_month in maintenance_available_months:
            maintenance_selected_month = requested_month
        else:
            dates_with_data = sorted(set(e.get('date', '')[:7] for e in events if e.get('date')), reverse=True)
            maintenance_selected_month = dates_with_data[0] if dates_with_data else maintenance_available_months[0]
        if maintenance_selected_month and maintenance_selected_month in maintenance_available_months:
            maintenance_headers, maintenance_table_data = build_maintenance_table(events, maintenance_selected_month)

    maintenance_stats = build_maintenance_stats(events, maintenance_selected_month) if (events and maintenance_selected_month) else None
    maintenance_calendar_data = build_maintenance_calendar(events, maintenance_selected_month) if (events and maintenance_selected_month) else {}
    maintenance_memo_data = build_maintenance_memo_data(events, maintenance_selected_month) if (events and maintenance_selected_month) else {}

    return {
        'maintenance_headers': maintenance_headers,
        'maintenance_table_data': maintenance_table_data,
        'maintenance_available_months': maintenance_available_months,
        'maintenance_selected_month': maintenance_selected_month,
        'maintenance_stats': maintenance_stats,
        'maintenance_calendar_data': maintenance_calendar_data,
        'maintenance_memo_data': maintenance_memo_data,
        'maintenance_years': years,
        'selected_maintenance_year': selected_year if years else None,
    }


def _find_col_key(col_map, *candidates):
    """컬럼명 후보 중 col_map에 존재하는 키 반환 (공백/언더스코어 정규화)."""
    for c in candidates:
        if col_map.get(c) is not None:
            return col_map.get(c)
    for key in col_map:
        k = (key or '').strip().replace(' ', '_')
        for c in candidates:
            if (c or '').strip().replace(' ', '_') == k:
                return key
    return None


def _cell_has_value(val):
    if pd.isna(val):
        return False
    return bool(str(val).strip())


def _extract_maintenance_categories(row, col_map):
    """엑셀 행에서 정비 유형(예방·일반·사고·검사·엔진·미션·교체·펑크·명일) 목록 반환."""
    categories = []
    checks = [
        ('예방', ['예방정비'], lambda v: '○' in str(v)),
        ('일반', ['일반정비'], lambda v: '△' in str(v)),
        ('사고', ['사고수리'], lambda v: '□' in str(v)),
        ('검사', ['검사'], lambda v: '!' in str(v)),
        ('엔진', ['엔진오일(L)', '엔진오일'], lambda v: _cell_has_value(v)),
        ('미션', ['미션오일(L)', '미션오일'], lambda v: _cell_has_value(v)),
        ('교체', ['타이어_교체'], lambda v: _cell_has_value(v)),
        ('펑크', ['타이어_펑크'], lambda v: _cell_has_value(v)),
        ('명일', ['명일_정비예정', '명일 정비예정'], lambda v: 'm' in str(v).lower()),
    ]
    for cat_name, candidates, predicate in checks:
        key = _find_col_key(col_map, *candidates)
        if key is None:
            continue
        val = row.get(key)
        if pd.isna(val):
            continue
        if predicate(val):
            categories.append(cat_name)
    return categories


MAINTENANCE_CATEGORY_SYMBOL = {
    '예방': '○',
    '일반': '△',
    '사고': '□',
    '검사': '!',
    '명일': 'm',
}
MAINTENANCE_SYMBOL_CATEGORY = {v: k for k, v in MAINTENANCE_CATEGORY_SYMBOL.items()}
MAINTENANCE_CALENDAR_CATEGORIES = ['예방', '일반', '사고', '검사', '엔진', '미션', '교체', '펑크', '명일']


def _normalize_maintenance_symbol(symbol):
    """저장된 symbol 값을 표준 기호로 정규화."""
    if symbol is None:
        return ''
    s = str(symbol).strip()
    if not s:
        return ''
    symbol_map = {
        '○': '○', 'o': '○', 'O': '○',
        '△': '△',
        '□': '□',
        '!': '!',
        'm': 'm', 'M': 'm',
    }
    if s in symbol_map:
        return symbol_map[s]
    if '○' in s:
        return '○'
    if '△' in s:
        return '△'
    if '□' in s:
        return '□'
    if '!' in s:
        return '!'
    if 'm' in s.lower():
        return 'm'
    return s


def _normalize_maintenance_events(events):
    """레거시 events에 category/symbol 보정 적용."""
    if not events:
        return events
    normalized = []
    for e in events:
        item = dict(e)
        symbol = _normalize_maintenance_symbol(item.get('symbol', ''))
        item['symbol'] = symbol
        category = item.get('category') or MAINTENANCE_SYMBOL_CATEGORY.get(symbol, '')
        if category:
            item['category'] = category
        normalized.append(item)
    return normalized


def _maintenance_symbol(row, col_map):
    """엑셀 행에서 정비 마킹 컬럼(예방정비○, 일반정비△, 사고수리□, 검사!, 명일 정비예정m) 확인 후 기호 반환."""
    for cat in _extract_maintenance_categories(row, col_map):
        symbol = MAINTENANCE_CATEGORY_SYMBOL.get(cat)
        if symbol:
            return symbol
    return None


def _norm_cell_str(val):
    """엑셀 셀 값을 문자열로 정규화 (숫자 1801.0 -> '1801')."""
    if pd.isna(val):
        return ''
    if isinstance(val, (int, float)):
        try:
            if isinstance(val, float) and val == int(val):
                return str(int(val))
            return str(int(val)) if isinstance(val, float) else str(val)
        except (ValueError, TypeError):
            return str(val).strip()
    return str(val).strip()


def _extract_maintenance_details(row, col_map):
    """엑셀 행에서 메모판용 상세 값 추출."""

    def detail_val(*candidates):
        key = _find_col_key(col_map, *candidates)
        if key is None:
            return ''
        raw = row.get(key)
        if pd.isna(raw):
            return ''
        return _norm_cell_str(raw)

    return {
        '엔진오일': detail_val('엔진오일(L)', '엔진오일'),
        '미션오일': detail_val('미션오일(L)', '미션오일'),
        '타이어_교체': detail_val('타이어_교체'),
        '타이어_펑크': detail_val('타이어_펑크'),
        '기타사항': detail_val('기타사항'),
    }


def parse_maintenance_excel(file_path):
    """차량정비 엑셀 1월~12월 시트를 모두 파싱해 이벤트 리스트 반환."""
    fname = os.path.basename(file_path)
    year_match = re.search(r'(\d{4})', fname)
    year_from_file = int(year_match.group(1)) if year_match else datetime.now().year
    month_sheets = [f'{i}월' for i in range(1, 13)]
    xl = pd.ExcelFile(file_path)
    events = []
    for raw_sheet_name in xl.sheet_names:
        sheet_name = (raw_sheet_name.strip() if isinstance(raw_sheet_name, str) else str(raw_sheet_name)).strip()
        if sheet_name not in month_sheets:
            continue
        df = pd.read_excel(file_path, sheet_name=raw_sheet_name)
        df.columns = [str(c).strip() for c in df.columns]
        col_map = {c: c for c in df.columns}
        required = ['정비일', '차번', '차종', '등록일자']
        if not all(r in col_map for r in required):
            continue
        for _, row in df.iterrows():
            try:
                d = row.get('정비일')
                if pd.isna(d):
                    continue
                if hasattr(d, 'strftime'):
                    date_str = d.strftime('%Y-%m-%d')
                else:
                    date_str = str(pd.Timestamp(d))[:10]
                if not date_str or len(date_str) < 10:
                    continue
                차번 = _norm_cell_str(row.get('차번'))
                if not 차번:
                    continue
                차종 = _norm_cell_str(row.get('차종'))
                등록일자 = row.get('등록일자')
                if pd.notna(등록일자) and hasattr(등록일자, 'strftime'):
                    등록일자 = 등록일자.strftime('%Y-%m-%d')
                else:
                    등록일자 = _norm_cell_str(등록일자)
                categories = _extract_maintenance_categories(row.to_dict(), col_map)
                if not categories:
                    continue
                details = _extract_maintenance_details(row.to_dict(), col_map)
                for category in categories:
                    events.append({
                        'date': date_str,
                        '차번': 차번,
                        '차종': 차종,
                        '등록일자': 등록일자,
                        'category': category,
                        'symbol': MAINTENANCE_CATEGORY_SYMBOL.get(category, ''),
                        **details,
                    })
            except Exception:
                continue
    if not events:
        return None, None, '1월~12월 시트에서 정비 데이터를 찾을 수 없습니다.'
    return events, year_from_file, None


def build_maintenance_table(events, year_month):
    """events에서 year_month(YYYY-MM) 해당 월만 필터해 차번/차종/등록일자 기준 1~31일 마킹 테이블 생성."""
    if not events:
        return ['차번', '차종', '등록일자'] + [str(i) for i in range(1, 32)], []
    headers = ['차번', '차종', '등록일자'] + [str(i) for i in range(1, 32)]
    ym = year_month
    by_vehicle = {}
    for e in events:
        if not e.get('date', '').startswith(ym):
            continue
        try:
            day = int(e['date'].split('-')[2])
        except (IndexError, ValueError):
            continue
        if day < 1 or day > 31:
            continue
        key = (e.get('차번', ''), e.get('차종', ''), e.get('등록일자', ''))
        if key not in by_vehicle:
            by_vehicle[key] = {d: [] for d in range(1, 32)}
        by_vehicle[key][day].append(e.get('symbol', ''))
    rows = []
    for (차번, 차종, 등록일자), days in sorted(by_vehicle.items()):
        row = {'차번': 차번, '차종': 차종, '등록일자': 등록일자}
        for d in range(1, 32):
            row[str(d)] = ''.join(days[d]) if days[d] else ''
        rows.append(row)
    return headers, rows


def build_maintenance_stats(events, year_month):
    """해당 월(YYYY-MM) 정비 이벤트로 통계 생성: 전체 차량 수, 차종별 차량 수, 유형별 건수."""
    if not events or not year_month:
        return None
    ym = year_month
    vehicles = set()
    vehicles_by_차종 = {}
    category_counts = {cat: 0 for cat in MAINTENANCE_CALENDAR_CATEGORIES}
    total_maintenance = 0
    for e in events:
        if not e.get('date', '').startswith(ym):
            continue
        key = (e.get('차번', ''), e.get('차종', ''), e.get('등록일자', ''))
        vehicles.add(key)
        차종 = e.get('차종', '').strip() or '-'
        if 차종 not in vehicles_by_차종:
            vehicles_by_차종[차종] = set()
        vehicles_by_차종[차종].add(key)
        category = e.get('category') or MAINTENANCE_SYMBOL_CATEGORY.get(
            _normalize_maintenance_symbol(e.get('symbol', '')), ''
        )
        if category in category_counts:
            category_counts[category] += 1
            total_maintenance += 1
    by_차종 = [(차종, len(s)) for 차종, s in vehicles_by_차종.items()]
    by_차종.sort(key=lambda x: -x[1])
    month_label = ym.split('-')[1] if len(ym) >= 7 else ''
    return {
        'month_label': month_label,
        'total_vehicles': len(vehicles),
        'by_차종': by_차종,
        'total_maintenance': total_maintenance,
        '예방정비': category_counts['예방'],
        '일반정비': category_counts['일반'],
        '사고수리': category_counts['사고'],
        '검사': category_counts['검사'],
        '명일정비': category_counts['명일'],
        '엔진오일': category_counts['엔진'],
        '미션오일': category_counts['미션'],
        '타이어교체': category_counts['교체'],
        '타이어펑크': category_counts['펑크'],
    }


def build_maintenance_calendar(events, year_month):
    """해당 월(YYYY-MM) 정비 이벤트를 날짜·유형별 차번 목록으로 집계."""
    if not events or not year_month:
        return {}
    ym = year_month
    try:
        year_str, month_str = ym.split('-')
        year_num, month_num = int(year_str), int(month_str)
        days_in_month = calendar.monthrange(year_num, month_num)[1]
    except (ValueError, TypeError):
        return {}
    day_map = {
        str(day): {cat: [] for cat in MAINTENANCE_CALENDAR_CATEGORIES}
        for day in range(1, days_in_month + 1)
    }
    seen = set()
    for e in events:
        if not e.get('date', '').startswith(ym):
            continue
        try:
            day = int(e['date'].split('-')[2])
        except (IndexError, ValueError):
            continue
        if day < 1 or day > days_in_month:
            continue
        category = e.get('category') or MAINTENANCE_SYMBOL_CATEGORY.get(_normalize_maintenance_symbol(e.get('symbol', '')), '')
        if category not in MAINTENANCE_CALENDAR_CATEGORIES:
            continue
        car_no = (e.get('차번') or '').strip()
        if not car_no:
            continue
        dedupe_key = (day, category, car_no)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        day_map[str(day)][category].append(car_no)
    for day_key in day_map:
        for category in MAINTENANCE_CALENDAR_CATEGORIES:
            day_map[day_key][category].sort(key=lambda x: (len(x), x))
    return day_map


def build_maintenance_memo_data(events, year_month):
    """해당 월 날짜별 차량 정비 상세(오일·타이어·기타사항) 목록."""
    if not events or not year_month:
        return {}
    ym = year_month
    memo_map = {}
    seen = set()
    for e in events:
        if not e.get('date', '').startswith(ym):
            continue
        try:
            day = str(int(e['date'].split('-')[2]))
        except (IndexError, ValueError):
            continue
        car_no = (e.get('차번') or '').strip()
        if not car_no:
            continue
        dedupe_key = (day, car_no)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        memo_map.setdefault(day, []).append({
            '차번': car_no,
            '차종': e.get('차종', '') or '',
            '등록일자': e.get('등록일자', '') or '',
            '엔진오일': e.get('엔진오일', '') or '',
            '미션오일': e.get('미션오일', '') or '',
            '타이어_교체': e.get('타이어_교체', '') or '',
            '타이어_펑크': e.get('타이어_펑크', '') or '',
            '기타사항': e.get('기타사항', '') or '',
        })
    for day_key in memo_map:
        memo_map[day_key].sort(key=lambda x: (len(x['차번']), x['차번']))
    return memo_map


@app.route('/driver', methods=['GET', 'POST'])
@login_required
def driver():
    print("=== /driver 라우트 호출됨 ===")
    print(f"요청 메서드: {request.method}")
    print(f"현재 사용자: {current_user.username if current_user else 'None'}")
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    required_columns = DRIVER_DATA_COLUMNS
    if request.method == 'POST':
        print("POST 요청 받음")
        view_year = request.form.get('view_year', type=int)
        if 'excel_file' not in request.files:
            return render_template(
                'driver.html',
                error='파일이 선택되지 않았습니다.',
                messages=messages,
                current_user=current_user,
                **_driver_page_context(selected_year=view_year),
            )
        file = request.files['excel_file']
        print(f"파일명: {file.filename}")
        if file.filename == '':
            return render_template(
                'driver.html',
                error='파일이 선택되지 않았습니다.',
                messages=messages,
                current_user=current_user,
                **_driver_page_context(selected_year=view_year),
            )
        if file and allowed_file(file.filename):
            filename = file.filename.replace('/', '').replace('\\', '')
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            print(f"저장 경로: {file_path}")
            file.save(file_path)
            print(f"파일 저장 완료: {file_path}")
            try:
                df = pd.read_excel(file_path, sheet_name=0)
                df.columns = [str(col).strip() for col in df.columns]
                missing = [col for col in required_columns if col not in df.columns]
                if missing:
                    error_msg = '다음 필수 컬럼이 누락되었습니다: ' + ', '.join(missing)
                    return render_template(
                        'driver.html',
                        error=error_msg,
                        messages=messages,
                        current_user=current_user,
                        **_driver_page_context(selected_year=view_year),
                    )
                for col in required_columns:
                    if col not in df.columns:
                        df[col] = ''
                date_cols = ['갱신시작', '갱신마감', '입사일자', '퇴사일자']
                for col in date_cols:
                    if col in df.columns:
                        try:
                            df[col] = pd.to_datetime(df[col], format='%Y-%m-%d', errors='coerce').dt.strftime('%Y-%m-%d').fillna('')
                        except Exception:
                            df[col] = df[col].astype(str).str.strip()
                driver_list = df[required_columns].fillna('').astype(str).to_dict('records')
                driver_data = normalize_driver_data({
                    'list': driver_list,
                    'columns': required_columns,
                })
                driver_year = extract_dispatch_year_from_filename(filename)
                save_driver_data(driver_data, year=driver_year)
                flask_url = url_for('uploaded_file', filename=os.path.basename(file_path), _external=True)
                record = UploadRecord(filename=filename, uploader=current_user.name, github_url=flask_url, upload_type='driver')
                db.session.add(record)
                db.session.commit()
                if view_year:
                    return redirect(url_for('driver', year=view_year))
                return redirect(url_for('driver'))
            except Exception as e:
                return render_template(
                    'driver.html',
                    error=f'파일 처리 중 오류: {str(e)}',
                    messages=messages,
                    current_user=current_user,
                    **_driver_page_context(selected_year=view_year),
                )
        else:
            return render_template(
                'driver.html',
                error='허용되지 않은 파일 형식입니다.',
                messages=messages,
                current_user=current_user,
                **_driver_page_context(selected_year=view_year),
            )
    return render_template('driver.html', messages=messages, current_user=current_user, **_driver_page_context())

@app.route('/car', methods=['GET', 'POST'])
@login_required
def car():
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    car_data = None
    if request.method == 'POST':
        upload_type = None
        file = None
        if 'excel_file_repair' in request.files and request.files['excel_file_repair'].filename:
            file = request.files['excel_file_repair']
            upload_type = 'car_repair'
        elif 'excel_file_maintenance' in request.files and request.files['excel_file_maintenance'].filename:
            file = request.files['excel_file_maintenance']
            upload_type = 'car_maintenance'
        if not file or not upload_type:
            return render_template('car.html', error='파일이 선택되지 않았습니다.', car_data=car_data, messages=messages, current_user=current_user)
        if not allowed_file(file.filename):
            return render_template('car.html', error='허용되지 않은 파일 형식입니다.', car_data=car_data, messages=messages, current_user=current_user)
        filename = file.filename.replace('/', '').replace('\\', '')
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        try:
            view_year = request.form.get('view_year', type=int)
            view_month = request.form.get('view_month')
            if upload_type == 'car_maintenance':
                events, year_from_file, parse_err = parse_maintenance_excel(file_path)
                if parse_err:
                    return render_template(
                        'car.html',
                        error=parse_err,
                        car_data=car_data,
                        messages=messages,
                        current_user=current_user,
                        **_build_car_maintenance_context(selected_year=view_year, requested_month=view_month),
                        repair_headers=[],
                        repair_table_data=[],
                        repair_available_months=[],
                        repair_selected_month=None,
                        repair_stats=None,
                    )
                save_car_maintenance_events(events, year_from_file)
            flask_url = url_for('uploaded_file', filename=os.path.basename(file_path), _external=True)
            record = UploadRecord(filename=filename, uploader=current_user.name, github_url=flask_url, upload_type=upload_type)
            db.session.add(record)
            db.session.commit()
            if upload_type == 'car_maintenance':
                if view_year and view_month:
                    return redirect(url_for('car', year=view_year, month=view_month))
                if view_year:
                    return redirect(url_for('car', year=view_year))
                return redirect(url_for('car'))
        except Exception as e:
            view_year = request.form.get('view_year', type=int)
            view_month = request.form.get('view_month')
            return render_template(
                'car.html',
                error=f'파일 처리 중 오류: {str(e)}',
                car_data=car_data,
                messages=messages,
                current_user=current_user,
                **_build_car_maintenance_context(selected_year=view_year, requested_month=view_month),
                repair_headers=[],
                repair_table_data=[],
                repair_available_months=[],
                repair_selected_month=None,
                repair_stats=None,
            )
    maintenance_ctx = _build_car_maintenance_context()
    repair_headers = []
    repair_table_data = []
    repair_available_months = []
    repair_selected_month = None
    repair_stats = None
    return render_template('car.html', car_data=car_data, messages=messages, current_user=current_user,
        **maintenance_ctx,
        repair_headers=repair_headers, repair_table_data=repair_table_data,
        repair_available_months=repair_available_months, repair_selected_month=repair_selected_month,
        repair_stats=repair_stats)

@app.route('/driver/profile/<driver_id>')
@login_required
def driver_profile(driver_id):
    driver_info = find_driver_by_emp_id(driver_id)
    if not driver_info:
        return '<h3>운전기사 정보를 찾을 수 없습니다.</h3>'
    
    # 배차 데이터 로드
    dispatch_data = load_dispatch_data()
    lease_data = load_lease_data()
    
    # 매출/급여 데이터 집계
    salary_summary = ''
    if dispatch_data and lease_data:
        driver_name = driver_info.get('이름', '')
        driver_emp_id = driver_info.get('사번', '')
        
        # 월별 데이터 집계
        monthly_stats = {}
        work_type = str(driver_info.get('근무유형', '') or '').strip()
        vehicle_types = set()
        
        # 배차 데이터에서 승무일 수 집계
        for month_key in dispatch_data.keys():
            month_data = dispatch_data[month_key].get('data', [])
            for record in month_data:
                if record.get('운전기사', '') == driver_name:
                    if not work_type:
                        work_type = record.get('근무유형', '')
                    vehicle_types.add(record.get('차종', ''))
                    
                    stats = compute_dispatch_row_stats(record)
                    work_days = int(stats['승무일'])
                    
                    if month_key not in monthly_stats:
                        monthly_stats[month_key] = {'승무일': 0}
                    monthly_stats[month_key]['승무일'] += work_days
        
        # 리스 데이터에서 급여 정보 집계
        for month_key in lease_data.keys():
            month_data = lease_data[month_key].get('data', [])
            for record in month_data:
                if record.get('사번', '') == driver_emp_id or record.get('이름', '') == driver_name:
                    if month_key not in monthly_stats:
                        monthly_stats[month_key] = {}
                    
                    monthly_stats[month_key]['실입금'] = int(record.get('실입금', 0))
                    monthly_stats[month_key]['연료비'] = int(record.get('연료비', 0))
                    monthly_stats[month_key]['급여'] = float(record.get('급여', 0))
                    monthly_stats[month_key]['차종'] = record.get('차종', '')
        
        # 월 정렬 (01월~12월)
        sorted_months = sorted(monthly_stats.keys())
        
        # 테이블 생성
        table_rows = []
        chart_labels = []
        chart_income = []
        chart_fuel = []
        chart_salary = []
        chart_workdays = []
        
        for month in sorted_months:
            stats = monthly_stats[month]
            work_days = stats.get('승무일', stats.get('승무일수', 0))
            income = stats.get('실입금', 0)
            fuel = stats.get('연료비', 0)
            salary = stats.get('급여', 0)
            
            table_rows.append(f'''
                <tr>
                    <td>{month}</td>
                    <td>{work_days}일</td>
                    <td>{income:,}원</td>
                    <td>{fuel:,}원</td>
                    <td>{salary:,.0f}원</td>
                </tr>
            ''')
            
            chart_labels.append(month)
            chart_workdays.append(work_days)
            chart_income.append(income)
            chart_fuel.append(fuel)
            chart_salary.append(int(salary))
        
        vehicle_types_str = ', '.join(sorted(vehicle_types)) if vehicle_types else '-'
        
        # 매출/급여 섹션 HTML
        salary_summary = f'''
        <div class="profile-section">
            <h3>매출/급여</h3>
            <table class="profile-table" style="margin-bottom:20px;">
                <tr><td class="label">근무유형</td><td>{work_type or '-'}</td></tr>
                <tr><td class="label">배차된 차종</td><td>{vehicle_types_str}</td></tr>
            </table>
            
            <div style="margin-top:20px;">
                <b>월별 현황</b>
                <table class="profile-table" style="margin-top:10px;">
                    <tr style="background:#f8f8f8;font-weight:600;">
                        <td>월</td>
                        <td>승무일</td>
                        <td>매출(실입금)</td>
                        <td>연료비</td>
                        <td>급여</td>
                    </tr>
                    {''.join(table_rows) if table_rows else '<tr><td colspan=5>데이터 없음</td></tr>'}
                </table>
            </div>
            
            <div style="margin-top:30px;">
                <b>월별 추이 그래프</b>
                <div style="margin-top:15px;">
                    <canvas id="combinedChart" style="max-height:300px;"></canvas>
                </div>
            </div>
        </div>
        
        <script>
        const months = {chart_labels};
        const workDays = {chart_workdays};
        const income = {chart_income};
        const fuel = {chart_fuel};
        const salary = {chart_salary};
        
        // 혼합 차트 (막대 + 라인)
        new Chart(document.getElementById('combinedChart'), {{
            type: 'bar',
            data: {{
                labels: months,
                datasets: [
                    {{
                        type: 'bar',
                        label: '승무일',
                        data: workDays,
                        backgroundColor: 'rgba(76, 175, 80, 0.2)',
                        borderColor: 'rgba(76, 175, 80, 0.5)',
                        borderWidth: 1,
                        yAxisID: 'y'
                    }},
                    {{
                        type: 'line',
                        label: '매출(실입금)',
                        data: income,
                        borderColor: 'rgba(33, 150, 243, 1)',
                        backgroundColor: 'rgba(33, 150, 243, 0.1)',
                        borderWidth: 2,
                        fill: false,
                        tension: 0.01,
                        yAxisID: 'y1'
                    }},
                    {{
                        type: 'line',
                        label: '연료비',
                        data: fuel,
                        borderColor: 'rgba(255, 152, 0, 1)',
                        backgroundColor: 'rgba(255, 152, 0, 0.1)',
                        borderWidth: 2,
                        fill: false,
                        tension: 0.01,
                        yAxisID: 'y1'
                    }},
                    {{
                        type: 'line',
                        label: '급여',
                        data: salary,
                        borderColor: 'rgba(156, 39, 176, 1)',
                        backgroundColor: 'rgba(156, 39, 176, 0.1)',
                        borderWidth: 2,
                        fill: false,
                        tension: 0.01,
                        yAxisID: 'y1'
                    }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: true,
                interaction: {{
                    mode: 'index',
                    intersect: false
                }},
                plugins: {{
                    legend: {{ 
                        display: true, 
                        position: 'top'
                    }},
                    title: {{ 
                        display: true, 
                        text: '월별 승무일 및 매출/연료비/급여 추이'
                    }}
                }},
                scales: {{
                    y: {{
                        type: 'linear',
                        position: 'left',
                        beginAtZero: true,
                        title: {{
                            display: true,
                            text: '승무일'
                        }}
                    }},
                    y1: {{
                        type: 'linear',
                        position: 'right',
                        beginAtZero: true,
                        title: {{
                            display: true,
                            text: '금액 (원)'
                        }},
                        grid: {{
                            drawOnChartArea: false
                        }}
                    }}
                }}
            }}
        }});
        </script>
        '''
    
    # 사고 데이터 로드 및 요약
    accident_data = load_accident_data_merged()
    accident_summary = ''
    if accident_data:
        name = driver_info.get('이름','')
        # 가해사고
        at_fault = [a for a in accident_data.get('at_fault', []) if a.get('기사명','') == name]
        not_at_fault = [a for a in accident_data.get('not_at_fault', []) if a.get('기사명','') == name]
        # 가해사고 요약
        at_count = len(at_fault)
        at_pending = sum(1 for a in at_fault if a.get('처리여부','') == '미결')
        at_repair = sum(int(str(a.get('수리지급','0')).replace(',','')) if str(a.get('수리지급','')).replace(',','').isdigit() else 0 for a in at_fault)
        at_treat = sum(int(str(a.get('치료지급','0')).replace(',','')) if str(a.get('치료지급','')).replace(',','').isdigit() else 0 for a in at_fault)
        at_dates = [a.get('사고일시','') for a in at_fault if a.get('사고일시','')]
        # 피해사고 요약
        not_count = len(not_at_fault)
        not_pending = sum(1 for a in not_at_fault if a.get('처리여부','') == '미결')
        not_damage = sum(int(str(a.get('피해견적','0')).replace(',','')) if str(a.get('피해견적','')).replace(',','').isdigit() else 0 for a in not_at_fault)
        not_dates = [a.get('사고일시','') for a in not_at_fault if a.get('사고일시','')]
        # 최근 사고일
        all_dates = at_dates + not_dates
        recent_date = max(all_dates) if all_dates else ''
        # 사고 리스트 테이블 생성 (사고일시 내림차순 정렬)
        import html
        from datetime import datetime

        def parse_dt(x):
            try:
                return datetime.strptime(x.get('사고일시', ''), '%Y-%m-%d %H:%M')
            except Exception:
                return datetime.min

        tagged_accidents = [(a, 'at_fault') for a in at_fault] + [(a, 'not_at_fault') for a in not_at_fault]
        all_accidents_sorted = sorted(tagged_accidents, key=lambda item: parse_dt(item[0]), reverse=True)
        accident_rows = []
        for a, acc_type in all_accidents_sorted:
            accident_no = str(a.get('사고번호', '') or '').strip()
            if accident_no:
                print_url = url_for('accident_print', type=acc_type, accident_no=accident_no)
                no_cell = (
                    f'<a href="{html.escape(print_url)}" target="_blank" rel="noopener">'
                    f'{html.escape(accident_no)}</a>'
                )
            else:
                no_cell = ''
            status = str(a.get('처리여부', '') or '').strip()
            if status == '종결':
                status_cell = f'<td class="accident-status-done">{html.escape(status)}</td>'
            elif status == '미결':
                status_cell = f'<td class="accident-status-pending">{html.escape(status)}</td>'
            else:
                status_cell = f'<td>{html.escape(status)}</td>'
            accident_rows.append(
                f'<tr><td>{no_cell}</td>'
                f'<td>{html.escape(str(a.get("사고일시", "")))}</td>'
                f'<td>{html.escape(str(a.get("차번", "")))}</td>'
                f'<td>{html.escape(str(a.get("접보사항", "")))}</td>'
                f'{status_cell}</tr>'
            )
        accident_table = f'''
        <div style="margin-top:18px;">
            <b>사고 리스트</b>
            <table class="profile-table" style="margin-top:8px;">
                <tr style="background:#f8f8f8;font-weight:600;">
                    <td>사고번호</td><td>사고일시</td><td>차번</td><td>접보사항</td><td>처리여부</td>
                </tr>
                {''.join(accident_rows) if accident_rows else '<tr><td colspan=5>사고 내역 없음</td></tr>'}
            </table>
        </div>
        '''
        # 사고 요약 HTML
        accident_summary = f'''
        <div class="profile-section">
            <h3>사고 요약</h3>
            <table class="profile-table">
                <tr><td class="label">가해사고</td><td>{at_count}건 (미결 {at_pending}건), &nbsp;&nbsp;&nbsp;&nbsp; 누적 수리비: {at_repair:,}원, &nbsp;&nbsp;&nbsp;&nbsp; 누적 치료비: {at_treat:,}원</td></tr>
                <tr><td class="label">피해사고</td><td>{not_count}건 (미결 {not_pending}건), &nbsp;&nbsp;&nbsp;&nbsp; 누적 피해견적: {not_damage:,}원</td></tr>
            </table>
            {accident_table}
        </div>
        '''
    renewal_start = str(driver_info.get('갱신시작', '') or '').split(' ')[0]
    renewal_end = str(driver_info.get('갱신마감', '') or '').split(' ')[0]
    if renewal_start and renewal_end:
        renewal_period = f'{renewal_start} ~ {renewal_end}'
    elif renewal_start:
        renewal_period = renewal_start
    elif renewal_end:
        renewal_period = renewal_end
    else:
        renewal_period = '-'
    # 상세 페이지 카드형 디자인 (이미지 예시 참고)
    return f'''
    <html lang="ko"><head><meta charset="utf-8"><title>운전기사 인사정보</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
    body {{ background:#f5f5f5; font-family:'Noto Sans KR',sans-serif; margin:0; }}
    .profile-wrap {{ max-width:800px; margin:40px auto; background:#fff; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.08); padding:40px 32px; }}
    .profile-header {{ display:flex; align-items:center; border-bottom:1px solid #eee; padding-bottom:24px; margin-bottom:32px; }}
    .profile-photo {{ width:90px; height:90px; border-radius:50%; background:#e0e0e0; display:flex; align-items:center; justify-content:center; font-size:40px; color:#888; margin-right:32px; }}
    .profile-maininfo h2 {{ margin:0 0 8px 0; font-size:2rem; font-weight:700; }}
    .profile-maininfo .sub {{ color:#666; font-size:1.1rem; margin-bottom:4px; }}
    .profile-maininfo .id {{ color:#aaa; font-size:1rem; }}
    .profile-section {{ margin-bottom:28px; }}
    .profile-section h3 {{ font-size:1.1rem; color:#4CAF50; margin-bottom:12px; border-bottom:1px solid #e0e0e0; padding-bottom:6px; }}
    .profile-row {{ display:flex; gap:20px; margin-bottom:28px; }}
    .profile-row .profile-section {{ flex:1; margin-bottom:0; }}
    .profile-table {{ width:100%; border-collapse:collapse; }}
    .profile-table td {{ padding:7px 10px; color:#333; font-size:1rem; border-bottom:1px solid #f2f2f2; }}
    .profile-table tr:last-child td {{ border-bottom:none; }}
    .profile-table a {{ color:#1976d2; text-decoration:none; }}
    .profile-table a:hover {{ text-decoration:underline; }}
    .profile-table .accident-status-done {{ color:#008000; font-weight:600; }}
    .profile-table .accident-status-pending {{ color:#ff0000; font-weight:600; }}
    .profile-table .label {{ color:#888; width:140px; font-weight:500; }}
    .profile-actions {{ position:absolute; top:40px; right:40px; }}
    .profile-actions button {{ margin-left:8px; padding:6px 18px; border-radius:5px; border:none; background:#eee; color:#333; font-weight:500; cursor:pointer; }}
    .profile-actions button.edit {{ background:#4CAF50; color:#fff; }}
    @media (max-width: 900px) {{ 
        .profile-wrap {{ padding:20px 5vw; }}
        .profile-row {{ flex-direction:column; }}
        .profile-row .profile-section {{ margin-bottom:28px; }}
    }}
    </style></head><body>
    <div class="profile-wrap">
        <div class="profile-header">
            <div class="profile-photo">
                <span>🧑🏼‍✈️</span>
            </div>
            <div class="profile-maininfo">
                <h2>{driver_info.get('이름','')}</h2>
                <div class="sub">사번: {driver_info.get('사번','')}</div>
                <div class="id">면허번호: {driver_info.get('면허번호','')}</div>
            </div>
        </div>
        <div class="profile-row">
            <div class="profile-section">
                <h3>기본 정보</h3>
                <table class="profile-table">
                    <tr><td class="label">이름</td><td>{driver_info.get('이름','')}</td></tr>
                    <tr><td class="label">사번</td><td>{driver_info.get('사번','')}</td></tr>
                    <tr><td class="label">나이</td><td>{driver_info.get('나이','')}</td></tr>
                    <tr><td class="label">주민등록번호</td><td>{driver_info.get('주민등록번호','')}</td></tr>
                    <tr><td class="label">연락처</td><td>{driver_info.get('연락처','')}</td></tr>
                </table>
            </div>
            <div class="profile-section">
                <h3>근무 정보</h3>
                <table class="profile-table">
                    <tr><td class="label">근무유형</td><td>{driver_info.get('근무유형','') or '-'}</td></tr>
                    <tr><td class="label">면허번호</td><td>{driver_info.get('면허번호','')}</td></tr>
                    <tr><td class="label">갱신기간</td><td>{renewal_period}</td></tr>
                    <tr><td class="label">입사일자</td><td>{driver_info.get('입사일자','').split(' ')[0] if driver_info.get('입사일자') else ''}</td></tr>
                    <tr><td class="label">퇴사일자</td><td>{driver_info.get('퇴사일자','').split(' ')[0] if driver_info.get('퇴사일자') else ''}</td></tr>
                </table>
            </div>
        </div>
        <div class="profile-section">
            <h3>거주지</h3>
            <table class="profile-table">
                <tr><td class="label">거주지</td><td>{driver_info.get('거주지','')}</td></tr>
            </table>
        </div>
        {salary_summary}
        {accident_summary}
    </div>
    </body></html>
    '''

@app.route('/accident/print/<type>/<accident_no>')
@login_required
def accident_print(type, accident_no):
    lease_data = load_lease_data()

    source_list_name = 'at_fault' if type == 'at_fault' else 'not_at_fault'
    template = 'accident_print_gahae.html' if type == 'at_fault' else 'accident_print_pihae.html'

    accident_info = find_accident_in_all_years(accident_no, source_list_name)
    
    if not accident_info:
        return '해당 사고 정보를 찾을 수 없습니다.', 404

    context = accident_info.copy()
    context['map_key'] = accident_map_key(context.get('사고번호'))

    driver_name = context.get('기사명')
    driver_info = {}
    if driver_name:
        driver_info = next((d for d in load_all_driver_records() if d.get('이름') == driver_name), {})
    
    context.update(driver_info)
    
    # context['차종']는 accident_info(원본 데이터)의 '차종' 값만 사용
    context['차종'] = accident_info.get('차종', '')

    # Cloudtype 환경 설정을 템플릿에 전달
    config = {
        'CLOUDTYPE_ENV': os.environ.get('CLOUDTYPE_ENV')
    }
    
    return render_template(template, accident=context, config=config)

@app.route('/save_map_image', methods=['POST'])
@login_required
def save_map_image():
    print("=== 🗺️ 사고지도 이미지 저장 시작 ===")
    print(f"📅 저장 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"👤 사용자: {current_user.username} (ID: {current_user.id})")
    
    data = request.get_json()
    version = accident_map_key(data.get('version'))
    image_data = data.get('image')
    
    print(f"📋 요청 데이터 - 버전: {version}")
    print(f"📊 이미지 데이터 길이: {len(image_data) if image_data else 0} characters")
    
    if not version or not image_data:
        print("❌ 저장 실패: 버전명 또는 이미지 데이터 누락")
        return {'success': False, 'error': '버전명 또는 이미지 데이터 누락'}, 400
    
    try:
        header, encoded = image_data.split(',', 1)
        img_bytes = base64.b64decode(encoded)
        print(f"🖼️ 이미지 디코딩 완료: {len(img_bytes)} bytes")
        
        # Cloudtype 환경에서는 절대 경로 사용
        if os.environ.get('CLOUDTYPE_ENV'):
            # Cloudtype 환경변수가 설정된 경우 절대 경로 사용
            save_dir = '/tmp/uploads/maps'
            print(f"☁️ Cloudtype 환경 감지: 절대 경로 사용")
        else:
            # 로컬 개발 환경에서는 상대 경로 사용
            save_dir = os.path.join('uploads', 'maps')
            print(f"💻 로컬 환경: 상대 경로 사용")
        
        print(f"📁 저장 디렉토리: {save_dir}")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f'{version}.png')
        
        with open(save_path, 'wb') as f:
            f.write(img_bytes)
        
        print(f"✅ 이미지 저장 성공: {save_path}")
        print(f"📏 파일 크기: {os.path.getsize(save_path)} bytes")
        print("=== 🗺️ 사고지도 이미지 저장 완료 ===\n")
        
        return {'success': True}
        
    except Exception as e:
        print(f"❌ 이미지 저장 중 오류 발생: {str(e)}")
        print("=== 🗺️ 사고지도 이미지 저장 실패 ===\n")
        return {'success': False, 'error': f'이미지 저장 실패: {str(e)}'}, 500

@app.route('/uploads/maps/<filename>')
def uploaded_map(filename):
    print("=== 🖼️ 사고지도 이미지 서빙 시작 ===")
    print(f"📅 서빙 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📁 요청 파일: {filename}")
    
    try:
        # Cloudtype 환경에서는 절대 경로 사용
        if os.environ.get('CLOUDTYPE_ENV'):
            # Cloudtype 환경변수가 설정된 경우 절대 경로 사용
            serve_dir = '/tmp/uploads/maps'
            print(f"☁️ Cloudtype 환경 감지: 절대 경로 사용")
        else:
            # 로컬 개발 환경에서는 상대 경로 사용
            serve_dir = os.path.join('uploads', 'maps')
            print(f"💻 로컬 환경: 상대 경로 사용")
        
        print(f"📁 서빙 디렉토리: {serve_dir}")
        file_path = os.path.join(serve_dir, filename)
        
        if os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            print(f"✅ 이미지 서빙 성공: {file_path}")
            print(f"📏 파일 크기: {file_size} bytes")
            print("=== 🖼️ 사고지도 이미지 서빙 완료 ===\n")
        else:
            print(f"⚠️ 파일이 존재하지 않음: {file_path}")
        
        return send_from_directory(serve_dir, filename)
        
    except Exception as e:
        print(f"❌ 이미지 서빙 중 오류 발생: {str(e)}")
        print("=== 🖼️ 사고지도 이미지 서빙 실패 ===\n")
        return jsonify({'error': f'이미지 서빙 실패: {str(e)}'}), 500

@app.route('/save_map_json', methods=['POST'])
@login_required
def save_map_json():
    print("=== 📄 사고지도 JSON 저장 시작 ===")
    print(f"📅 저장 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"👤 사용자: {current_user.username} (ID: {current_user.id})")
    
    data = request.get_json()
    version = accident_map_key(data.get('version'))
    json_data = data.get('json')
    
    print(f"📋 요청 데이터 - 버전: {version}")
    print(f"📊 JSON 데이터 길이: {len(json_data) if json_data else 0} characters")
    
    if not version or not json_data:
        print("❌ 저장 실패: 버전명 또는 JSON 데이터 누락")
        return {'success': False, 'error': '버전명 또는 JSON 데이터 누락'}, 400
    
    try:
        # Cloudtype 환경에서는 절대 경로 사용
        if os.environ.get('CLOUDTYPE_ENV'):
            # Cloudtype 환경변수가 설정된 경우 절대 경로 사용
            save_dir = '/tmp/uploads/maps'
            print(f"☁️ Cloudtype 환경 감지: 절대 경로 사용")
        else:
            # 로컬 개발 환경에서는 상대 경로 사용
            save_dir = os.path.join('uploads', 'maps')
            print(f"💻 로컬 환경: 상대 경로 사용")
        
        print(f"📁 저장 디렉토리: {save_dir}")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f'{version}.json')
        
        with open(save_path, 'w', encoding='utf-8') as f:
            f.write(json_data)
        
        print(f"✅ JSON 저장 성공: {save_path}")
        print(f"📏 파일 크기: {os.path.getsize(save_path)} bytes")
        print("=== 📄 사고지도 JSON 저장 완료 ===\n")
        
        return {'success': True}
        
    except Exception as e:
        print(f"❌ JSON 저장 중 오류 발생: {str(e)}")
        print("=== 📄 사고지도 JSON 저장 실패 ===\n")
        return {'success': False, 'error': f'JSON 저장 실패: {str(e)}'}, 500

@app.route('/load_map_json')
@login_required
def load_map_json():
    print("=== 📖 사고지도 JSON 불러오기 시작 ===")
    print(f"📅 불러오기 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"👤 사용자: {current_user.username} (ID: {current_user.id})")
    
    version = request.args.get('version', '').strip()
    if version.lower() == 'intro':
        version = 'intro'
    else:
        version = accident_map_key(version) or version
    print(f"📋 요청 데이터 - 버전: {version}")
    
    if not version:
        print("❌ 불러오기 실패: 버전명 누락")
        return jsonify({'success': False, 'error': '버전명 누락'}), 400
    
    try:
        # Cloudtype 환경에서는 절대 경로 사용
        if os.environ.get('CLOUDTYPE_ENV'):
            # Cloudtype 환경변수가 설정된 경우 절대 경로 사용
            load_path = os.path.join('/tmp/uploads/maps', f'{version}.json')
            print(f"☁️ Cloudtype 환경 감지: 절대 경로 사용")
        else:
            # 로컬 개발 환경에서는 상대 경로 사용
            load_path = os.path.join('uploads', 'maps', f'{version}.json')
            print(f"💻 로컬 환경: 상대 경로 사용")
        
        print(f"📁 파일 경로: {load_path}")
        
        if not os.path.exists(load_path):
            print(f"❌ 파일이 존재하지 않음: {load_path}")
            return jsonify({'success': False, 'error': '해당 버전의 지도 데이터가 없습니다.'}), 404
        
        with open(load_path, 'r', encoding='utf-8') as f:
            json_data = f.read()
        
        print(f"✅ JSON 불러오기 성공: {load_path}")
        print(f"📏 파일 크기: {len(json_data)} characters")
        print("=== 📖 사고지도 JSON 불러오기 완료 ===\n")
        
        return jsonify({'success': True, 'json': json_data})
        
    except Exception as e:
        print(f"❌ JSON 불러오기 중 오류 발생: {str(e)}")
        print("=== 📖 사고지도 JSON 불러오기 실패 ===\n")
        return jsonify({'success': False, 'error': f'JSON 불러오기 실패: {str(e)}'}), 500

@app.route('/settings')
@login_required
def settings():
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    return render_template('settings.html', messages=messages, current_user=current_user)


@app.route('/build_note')
@login_required
def build_note():
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    return render_template('buildnote.html', messages=messages, current_user=current_user)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    from sqlalchemy.orm import joinedload
    if request.method == 'POST':
        user = User.query.get(current_user.id)
        # 폼 데이터 받기
        email = request.form.get('email')
        name = request.form.get('name')
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        phone = request.form.get('phone')
        department = request.form.get('department')
        position = request.form.get('position')
        messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
        profile_ctx = dict(user=user, messages=messages, current_user=current_user, departments=USER_DEPARTMENTS)

        if department not in USER_DEPARTMENTS:
            flash('소속을 선택해주세요.', 'error')
            return render_template('profile.html', **profile_ctx)

        # 이메일 중복 체크 (자신 제외)
        if email != user.email:
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                flash('이미 사용 중인 이메일입니다.', 'error')
                return render_template('profile.html', **profile_ctx)
        # 이메일과 이름 업데이트
        user.email = email
        user.name = name
        user.phone = phone
        user.department = department
        user.position = position
        # 비밀번호 변경 요청이 있는 경우
        if current_password and new_password:
            if not user.check_password(current_password):
                flash('현재 비밀번호가 올바르지 않습니다.', 'error')
                return render_template('profile.html', **profile_ctx)
            if new_password != confirm_password:
                flash('새 비밀번호가 일치하지 않습니다.', 'error')
                return render_template('profile.html', **profile_ctx)
            user.set_password(new_password)
            flash('비밀번호가 변경되었습니다.', 'success')
        # 데이터베이스에 저장
        db.session.commit()
        flash('프로필이 업데이트되었습니다.', 'success')
        return redirect(url_for('profile'))
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
    return render_template(
        'profile.html',
        user=current_user,
        messages=messages,
        current_user=current_user,
        departments=USER_DEPARTMENTS,
    )

@app.route('/admin/users')
@login_required
def admin_users():
    if current_user.role != 'admin':
        flash('관리자만 접근할 수 있습니다.', 'error')
        return redirect(url_for('calculate_salary'))
    
    users = User.query.all()
    return render_template('admin_users.html', users=users, departments=USER_DEPARTMENTS)

@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_user(user_id):
    if current_user.role != 'admin':
        flash('관리자만 접근할 수 있습니다.', 'error')
        return redirect(url_for('calculate_salary'))
    
    user = User.query.get_or_404(user_id)
    
    if request.method == 'POST':
        user.email = request.form.get('email')
        user.name = request.form.get('name')
        user.phone = request.form.get('phone')
        department = request.form.get('department')
        if department in USER_DEPARTMENTS:
            user.department = department
        user.position = request.form.get('position')
        user.role = request.form.get('role')
        
        new_password = request.form.get('new_password')
        if new_password:
            user.set_password(new_password)
        
        db.session.commit()
        flash('사용자 정보가 업데이트되었습니다.', 'success')
        return redirect(url_for('admin_users'))
    
    return render_template('admin_edit_user.html', user=user)

@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    if current_user.role != 'admin':
        flash('관리자만 접근할 수 있습니다.', 'error')
        return redirect(url_for('calculate_salary'))
    
    user = User.query.get_or_404(user_id)
    
    # 자신을 삭제하려고 하는 경우 방지
    if user.id == current_user.id:
        flash('자신의 계정은 삭제할 수 없습니다.', 'error')
        return redirect(url_for('admin_users'))
    
    # 사용자와 관련된 메시지도 삭제
    Message.query.filter_by(user_id=user.id).delete()
    
    db.session.delete(user)
    db.session.commit()
    
    flash(f'사용자 {user.username}이(가) 삭제되었습니다.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/uploads/dat/<filename>')
def uploaded_dat_file(filename):
    return send_from_directory(app.config['DAT_FOLDER'], filename)

@app.route('/api/latest-upload')
def latest_upload():
    upload_type = request.args.get('type')
    q = UploadRecord.query
    if upload_type:
        q = q.filter_by(upload_type=upload_type)
    record = q.order_by(UploadRecord.upload_time.desc()).first()
    if record:
        import pytz
        from datetime import datetime
        kst = pytz.timezone('Asia/Seoul')
        
        # upload_time 처리 - 더 확실한 KST 변환
        if hasattr(record, 'upload_time') and isinstance(record.upload_time, datetime):
            # datetime 객체인 경우 KST로 변환
            if record.upload_time.tzinfo is None:
                # timezone이 없는 경우 UTC로 가정하고 KST로 변환
                utc = pytz.timezone('UTC')
                utc_time = utc.localize(record.upload_time)
                upload_time_kst = utc_time.astimezone(kst).strftime('%Y-%m-%d %H:%M:%S')
            else:
                # timezone이 있는 경우 KST로 변환
                upload_time_kst = record.upload_time.astimezone(kst).strftime('%Y-%m-%d %H:%M:%S')
        else:
            # 문자열인 경우 그대로 사용 (이미 KST로 저장되어 있음)
            upload_time_kst = record.upload_time
            
        return jsonify({
            "filename": record.filename,
            "uploader": record.uploader,
            "upload_time": upload_time_kst,
            "github_url": record.github_url,
            "upload_type": record.upload_type
        })
    else:
        return jsonify({"message": "No upload record found"}), 404

# gunicorn import 시 사고번호·약도 파일 마이그레이션
init_accident_migrations()

if __name__ == '__main__':
    print("=== Flask 앱 시작 ===")
    
    create_database()  # 데이터베이스 생성
    print("=== 데이터베이스 생성 완료 ===")
    print("=== Flask 앱 실행 중... ===")
    app.run(host='127.0.0.1', port=5000, debug=True)