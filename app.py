from flask import Flask, render_template, request, session, redirect, url_for, flash, send_from_directory, jsonify
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

from dotenv import load_dotenv
import pytz

# .env 파일 로드 (배포 환경에서는 환경변수 직접 사용)
try:
    load_dotenv()
except:
    pass  # .env 파일이 없어도 계속 진행

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config['UPLOAD_FOLDER'] = 'uploads'
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
        position = request.form.get('position')
        
        if password != confirm_password:
            return render_template('register.html', error='비밀번호가 일치하지 않습니다.')
            
        if User.query.filter_by(username=username).first():
            return render_template('register.html', error='이미 존재하는 아이디입니다.')
            
        if User.query.filter_by(email=email).first():
            return render_template('register.html', error='이미 존재하는 이메일입니다.')
            
        user = User(username=username, email=email, name=name, phone=phone, position=position)
        user.set_password(password)
        
        db.session.add(user)
        db.session.commit()
        
        flash('회원가입이 완료되었습니다. 로그인해주세요.', 'success')
        return redirect(url_for('login'))
        
    return render_template('register.html')

# 로그아웃 라우트
@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('로그아웃되었습니다.', 'info')
    return redirect(url_for('login'))

# 기존 라우트들에 @login_required 데코레이터 추가
@app.route('/', methods=['GET', 'POST'])
@login_required
def calculate_salary():
    # lease_data.json에서 월별 실입금 및 연료비 데이터 합산
    total_income = 0
    total_fuel_cost = 0
    monthly_incomes = {}
    monthly_fuel_costs = {}
    
    try:
        lease_data_path = os.path.join(app.config['DATA_FOLDER'], 'lease_data.json')
        if os.path.exists(lease_data_path):
            with open(lease_data_path, 'r', encoding='utf-8') as f:
                lease_data = json.load(f)
                
                for month, month_data in lease_data.items():
                    if 'data' in month_data:
                        month_income_total = 0
                        month_fuel_total = 0
                        for driver_data in month_data['data']:
                            try:
                                income = int(driver_data.get('실입금', '0'))
                                fuel_cost = int(driver_data.get('연료비', '0'))
                                month_income_total += income
                                month_fuel_total += fuel_cost
                            except (ValueError, TypeError):
                                continue
                        monthly_incomes[month] = month_income_total
                        monthly_fuel_costs[month] = month_fuel_total
                        total_income += month_income_total
                        total_fuel_cost += month_fuel_total
    except Exception as e:
        print(f"lease_data.json 읽기 오류: {e}")
        total_income = 0
        total_fuel_cost = 0
        monthly_incomes = {}
        monthly_fuel_costs = {}
    
    # 월 평균 수입금 계산
    monthly_avg_income = 0
    if monthly_incomes:
        monthly_avg_income = total_income // len(monthly_incomes)
    
    # 현재 선택된 월 (기본값: 현재 월)
    import datetime
    current_month = f"{datetime.datetime.now().month:02d}월"
    selected_month = request.args.get('month', current_month)
    current_month_income = monthly_incomes.get(selected_month, 0)
    current_month_fuel_cost = monthly_fuel_costs.get(selected_month, 0)
    
    # 이전 달 수입금 및 연료비 계산
    month_order = ['01월', '02월', '03월', '04월', '05월', '06월', '07월', '08월', '09월', '10월', '11월', '12월']
    try:
        current_index = month_order.index(selected_month)
        previous_month = month_order[current_index - 1] if current_index > 0 else month_order[-1]
        previous_month_income = monthly_incomes.get(previous_month, 0)
        previous_month_fuel_cost = monthly_fuel_costs.get(previous_month, 0)
        
        # 수입금 변화량과 변화율 계산
        income_diff = current_month_income - previous_month_income
        income_diff_percent = round((income_diff / previous_month_income) * 100, 2) if previous_month_income > 0 else 0
        
        # 연료비 변화량과 변화율 계산
        fuel_diff = current_month_fuel_cost - previous_month_fuel_cost
        fuel_diff_percent = round((fuel_diff / previous_month_fuel_cost) * 100, 2) if previous_month_fuel_cost > 0 else 0
    except:
        previous_month_income = 0
        previous_month_fuel_cost = 0
        income_diff = 0
        income_diff_percent = 0
        fuel_diff = 0
        fuel_diff_percent = 0
    
    # 이전 달 대비 변화율 계산 (간단한 예시)
    income_change = 0
    income_percent = 0
    if len(monthly_incomes) >= 2:
        months = list(monthly_incomes.keys())
        if len(months) >= 2:
            current_month = months[-1]
            previous_month = months[-2]
            current_income = monthly_incomes.get(current_month, 0)
            previous_income = monthly_incomes.get(previous_month, 0)
            
            if previous_income > 0:
                income_change = current_income - previous_income
                income_percent = round((income_change / previous_income) * 100, 2)
    
    # 월별 배차 현황 통계
    dispatch_data_path = os.path.join(app.config['DATA_FOLDER'], 'dispatch_data.json')
    dispatch_stats = {}  # {월: {카테고리: 운행수}}
    categories = ['주간', '야간', '일차', '교대', '리스']
    month_order = ['01월', '02월', '03월', '04월', '05월', '06월', '07월', '08월', '09월', '10월', '11월', '12월']
    driver_counts = {}  # {월: 운전기사수}
    driver_counts_by_category = {}  # {월: {카테고리: 기사수}}
    dispatch_data = load_dispatch_data()
    if dispatch_data:
        for month in month_order:
            month_data = dispatch_data.get(month, {}).get('data', [])
            cat_counts = {cat: 0 for cat in categories}
            drivers = set()
            cat_drivers = {cat: set() for cat in categories}  # 각 카테고리별 기사 집합
            for row in month_data:
                cat = row.get('근무유형', '')
                if cat in categories:
                    for day in range(1, 32):
                        val = str(row.get(str(day), '')).strip()
                        if _dispatch_val_matches(val, 'o') or val == '/' or _dispatch_val_matches(val, 'H'):
                            cat_counts[cat] += 1
                    # 근무유형별 기사 집계
                    name = row.get('운전기사', '').strip()
                    if name:
                        cat_drivers[cat].add(name)
                # 전체 운전기사 집계
                name = row.get('운전기사', '').strip()
                if name:
                    drivers.add(name)
            dispatch_stats[month] = cat_counts
            driver_counts[month] = len(drivers)
            driver_counts_by_category[month] = {cat: len(cat_drivers[cat]) for cat in categories}

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
                                            accident_stats_by_month=empty_accident_stats)
                    
                    session['salary_data'] = salary_data
                    session['salary_calculated'] = True
                    
                    # 사고현황 통계 계산
                    accident_data_path = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
                    total_at_fault = 0
                    total_not_at_fault = 0
                    total_at_fault_repair = 0
                    total_not_at_fault_payment = 0
                    unresolved_at_fault = 0
                    unresolved_not_at_fault = 0
                    unpaid_at_fault_estimate = 0
                    unpaid_not_at_fault_estimate = 0
                    if os.path.exists(accident_data_path):
                        with open(accident_data_path, 'r', encoding='utf-8') as f:
                            accident_data = json.load(f)
                            at_fault = accident_data.get('at_fault', [])
                            not_at_fault = accident_data.get('not_at_fault', [])
                            total_at_fault = len(at_fault)
                            total_not_at_fault = len(not_at_fault)
                            # 가해보상금(수리): '수리지급'의 총합
                            def parse_amount(amount_str):
                                if not amount_str or amount_str == '' or amount_str == '-':
                                    return 0
                                try:
                                    return int(str(amount_str).replace(',', ''))
                                except:
                                    return 0
                            total_at_fault_repair = sum(parse_amount(a.get('수리지급', 0)) for a in at_fault)
                            # 피해보상금: '금액'의 총합
                            total_not_at_fault_payment = sum(parse_amount(a.get('금액', 0)) for a in not_at_fault)
                            for a in at_fault:
                                # 미결 가해사고
                                if a.get('처리여부', '').strip() == '미결':
                                    unresolved_at_fault += 1
                                # 미지급 가해보상금(견적)
                                try:
                                    unpaid_at_fault_estimate += int(str(a.get('견적', 0)).replace(',', ''))
                                except:
                                    pass
                            for a in not_at_fault:
                                # 미결 피해사고
                                if a.get('처리여부', '').strip() == '미결':
                                    unresolved_not_at_fault += 1
                                # 미입금 피해보상금(피해견적)
                                try:
                                    unpaid_not_at_fault_estimate += int(str(a.get('피해견적', 0)).replace(',', ''))
                                except:
                                    pass
                            # 월별 사고 통계 (라인차트용) + 사고원인별 분포
                            accident_stats_by_month = {m: {'at_fault': 0, 'at_fault_causes': {}, 'not_at_fault': 0, 'not_at_fault_causes': {}} for m in month_order}
                            for a in at_fault:
                                dt = a.get('사고일시', '')
                                if dt and '/' in dt:
                                    try:
                                        mm = dt.strip().split('/')[0].zfill(2)
                                        month_key = f'{mm}월'
                                        if month_key in accident_stats_by_month:
                                            accident_stats_by_month[month_key]['at_fault'] += 1
                                            cause = (a.get('사고원인', '') or '').strip() or '기타'
                                            accident_stats_by_month[month_key]['at_fault_causes'][cause] = accident_stats_by_month[month_key]['at_fault_causes'].get(cause, 0) + 1
                                    except:
                                        pass
                            for a in not_at_fault:
                                dt = a.get('사고일시', '')
                                if dt and '/' in dt:
                                    try:
                                        mm = dt.strip().split('/')[0].zfill(2)
                                        month_key = f'{mm}월'
                                        if month_key in accident_stats_by_month:
                                            accident_stats_by_month[month_key]['not_at_fault'] += 1
                                            cause = (a.get('사고원인', '') or '').strip() or '기타'
                                            accident_stats_by_month[month_key]['not_at_fault_causes'][cause] = accident_stats_by_month[month_key]['not_at_fault_causes'].get(cause, 0) + 1
                                    except:
                                        pass
                    else:
                        accident_stats_by_month = {m: {'at_fault': 0, 'at_fault_causes': {}, 'not_at_fault': 0, 'not_at_fault_causes': {}} for m in month_order}
                    
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
                                        accident_stats_by_month=accident_stats_by_month)
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
                                        accident_stats_by_month=empty_accident_stats)
    
    # GET 요청이거나 세션에 저장된 데이터가 있는 경우
    salary_data = session.get('salary_data', None)
    calculated = session.get('salary_calculated', False)
    
    # 사고현황 통계 계산
    accident_data_path = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
    total_at_fault = 0
    total_not_at_fault = 0
    total_at_fault_repair = 0
    total_not_at_fault_payment = 0
    unresolved_at_fault = 0
    unresolved_not_at_fault = 0
    unpaid_at_fault_estimate = 0
    unpaid_not_at_fault_estimate = 0
    if os.path.exists(accident_data_path):
        with open(accident_data_path, 'r', encoding='utf-8') as f:
            accident_data = json.load(f)
            at_fault = accident_data.get('at_fault', [])
            not_at_fault = accident_data.get('not_at_fault', [])
            total_at_fault = len(at_fault)
            total_not_at_fault = len(not_at_fault)
            # 가해보상금(수리): '수리지급'의 총합
            def parse_amount(amount_str):
                if not amount_str or amount_str == '' or amount_str == '-':
                    return 0
                try:
                    return int(str(amount_str).replace(',', ''))
                except:
                    return 0
            total_at_fault_repair = sum(parse_amount(a.get('수리지급', 0)) for a in at_fault)
            # 피해보상금: '금액'의 총합
            total_not_at_fault_payment = sum(parse_amount(a.get('금액', 0)) for a in not_at_fault)
            for a in at_fault:
                # 미결 가해사고
                if a.get('처리여부', '').strip() == '미결':
                    unresolved_at_fault += 1
                # 미지급 가해보상금(견적)
                try:
                    unpaid_at_fault_estimate += int(str(a.get('견적', 0)).replace(',', ''))
                except:
                    pass
            for a in not_at_fault:
                # 미결 피해사고
                if a.get('처리여부', '').strip() == '미결':
                    unresolved_not_at_fault += 1
                # 미입금 피해보상금(피해견적)
                try:
                    unpaid_not_at_fault_estimate += int(str(a.get('피해견적', 0)).replace(',', ''))
                except:
                    pass
    
    # 월별 사고 현황 통계 (라인차트용) + 사고원인별 분포
    accident_stats_by_month = {}
    for m in month_order:
        accident_stats_by_month[m] = {'at_fault': 0, 'at_fault_causes': {}, 'not_at_fault': 0, 'not_at_fault_causes': {}}
    if os.path.exists(accident_data_path):
        with open(accident_data_path, 'r', encoding='utf-8') as f:
            accident_data = json.load(f)
            for a in accident_data.get('at_fault', []):
                dt = a.get('사고일시', '')
                if dt and '/' in dt:
                    try:
                        mm = dt.strip().split('/')[0].zfill(2)
                        month_key = f'{mm}월'
                        if month_key in accident_stats_by_month:
                            accident_stats_by_month[month_key]['at_fault'] += 1
                            cause = (a.get('사고원인', '') or '').strip() or '기타'
                            accident_stats_by_month[month_key]['at_fault_causes'][cause] = accident_stats_by_month[month_key]['at_fault_causes'].get(cause, 0) + 1
                    except:
                        pass
            for a in accident_data.get('not_at_fault', []):
                dt = a.get('사고일시', '')
                if dt and '/' in dt:
                    try:
                        mm = dt.strip().split('/')[0].zfill(2)
                        month_key = f'{mm}월'
                        if month_key in accident_stats_by_month:
                            accident_stats_by_month[month_key]['not_at_fault'] += 1
                            cause = (a.get('사고원인', '') or '').strip() or '기타'
                            accident_stats_by_month[month_key]['not_at_fault_causes'][cause] = accident_stats_by_month[month_key]['not_at_fault_causes'].get(cause, 0) + 1
                    except:
                        pass
    
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
                        accident_stats_by_month=accident_stats_by_month)

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
                    # 시트별 데이터 처리
                    sheet_names = ['01월', '02월', '03월', '04월', '05월', '06월', 
                                 '07월', '08월', '09월', '10월', '11월', '12월']
                    dispatch_data = OrderedDict()
                    
                    for sheet in sheet_names:
                        try:
                            df = pd.read_excel(filepath, sheet_name=sheet)
                            # 데이터 전처리
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
                        except:
                            continue
                    
                    if not dispatch_data:
                        return render_template('schedule.html', 
                                            error="엑셀 파일에서 읽을 수 있는 시트가 없습니다.")
                    
                    # 파일로 저장
                    save_dispatch_data(dispatch_data)
                    
                    # UploadRecord 데이터베이스 저장
                    flask_url = url_for('uploaded_file', filename=os.path.basename(filepath), _external=True)
                    record = UploadRecord(filename=filename, uploader=current_user.name, github_url=flask_url, upload_type='schedule')
                    db.session.add(record)
                    db.session.commit()
                    
                    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
                    return render_template('schedule.html', dispatch_data=dispatch_data, messages=messages, current_user=current_user)
                except Exception as e:
                    return render_template('schedule.html', 
                                        error=f"엑셀 파일 처리 중 오류가 발생했습니다: {str(e)}")
    
    # GET 요청이거나 저장된 데이터가 있는 경우
    dispatch_data = load_dispatch_data()
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    return render_template('schedule.html', dispatch_data=dispatch_data, messages=messages, current_user=current_user)

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

@app.route('/accident', methods=['GET', 'POST'])
@login_required
def accident():
    print("=== /accident 라우트 호출됨 ===")
    print(f"요청 메서드: {request.method}")
    print(f"현재 사용자: {current_user.username if current_user else 'None'}")
    if request.method == 'POST':
        print("POST 요청 받음")
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
                        if col == '사고일시':
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
                save_accident_data(accident_data)
                
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
                
            return redirect(url_for('accident'))

    accident_data = load_accident_data()
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    upload_info = {
        'filename': session.get('last_accident_file'),
        'upload_time': session.get('upload_time'),
        'uploader_name': session.get('uploader_name')
    }

    return render_template('accident.html', accident_data=accident_data, messages=messages, current_user=current_user, upload_info=upload_info)

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

# 데이터베이스 생성
def create_database():
    with app.app_context():
        db.create_all()

# 세션 유지 시간을 매우 길게 설정 (900일)
app.permanent_session_lifetime = timedelta(days=900)

# 허용할 파일 확장자 설정
ALLOWED_EXTENSIONS = {'xlsx', 'xls'}

def allowed_file(filename):
    """허용된 파일 확장자인지 확인"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 업로드 폴더와 데이터 폴더가 없으면 생성
for folder in [app.config['UPLOAD_FOLDER'], app.config['DATA_FOLDER']]:
    if not os.path.exists(folder):
        os.makedirs(folder)



DISPATCH_HEADER_RENAME = {
    '인정일수': '인정일',
    '근무일수': '승무일',
    '승무일수': '승무일',
    '결근일수': '결근일',
}
DISPATCH_LEGACY_STAT_KEYS = ('인정일수', '근무일수', '승무일수', '결근일수')


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


def save_dispatch_data(data):
    print("=== save_dispatch_data 함수 시작 ===")
    filepath = os.path.join(app.config['DATA_FOLDER'], 'dispatch_data.json')
    print(f"JSON 저장 경로: {filepath}")
    data = normalize_dispatch_data(data)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("JSON 파일 저장 완료")


def load_dispatch_data():
    """저장된 배차 데이터를 불러옴"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'dispatch_data.json')
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:  # 파일이 비어있지 않은 경우에만 파싱
                    raw = json.loads(content)
                    if isinstance(raw, dict):
                        return normalize_dispatch_data(
                            OrderedDict(raw) if not isinstance(raw, OrderedDict) else raw
                        )
                    return raw
                else:
                    print(f"dispatch_data.json 파일이 비어있습니다.")
                    return None
        except json.JSONDecodeError as e:
            print(f"dispatch_data.json JSON 파싱 오류: {e}")
            return None
        except Exception as e:
            print(f"dispatch_data.json 읽기 오류: {e}")
            return None
    return None

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

def save_accident_data(data):
    print("=== save_accident_data 함수 시작 ===")
    # 요약 데이터 생성
    if data and ('at_fault' in data or 'not_at_fault' in data):
        at_fault_data = data.get('at_fault', [])
        not_at_fault_data = data.get('not_at_fault', [])
        
        # 기본 통계
        total_count = len(at_fault_data) + len(not_at_fault_data)
        at_fault_count = len(at_fault_data)
        not_at_fault_count = len(not_at_fault_data)
        at_fault_pending_count = sum(1 for a in at_fault_data if a.get('처리여부', '') == '미결')
        not_at_fault_pending_count = sum(1 for a in not_at_fault_data if a.get('처리여부', '') == '미결')
        
        # 금액 통계
        def parse_amount(amount_str):
            if not amount_str or amount_str == '' or amount_str == '-':
                return 0
            try:
                return int(str(amount_str).replace(',', ''))
            except:
                return 0
        
        at_fault_total_repair = sum(parse_amount(a.get('수리지급', 0)) for a in at_fault_data)
        at_fault_total_treatment = sum(parse_amount(a.get('치료지급', 0)) for a in at_fault_data)
        not_at_fault_total_damage = sum(parse_amount(a.get('피해견적', 0)) for a in not_at_fault_data)
        not_at_fault_total_payment = sum(parse_amount(a.get('금액', 0)) for a in not_at_fault_data)
        
        # 기사별 통계
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
                        'damage_estimate': 0
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
                        'damage_estimate': 0
                    }
                driver_stats[driver_name]['not_at_fault_count'] += 1
                driver_stats[driver_name]['damage_estimate'] += parse_amount(accident.get('피해견적', 0))
        
        # 차량별 통계
        vehicle_stats = {}
        for accident in at_fault_data:
            vehicle_number = accident.get('차번', '')
            if vehicle_number:
                if vehicle_number not in vehicle_stats:
                    vehicle_stats[vehicle_number] = {
                        'number': vehicle_number,
                        'at_fault_count': 0,
                        'not_at_fault_count': 0,
                        'damage_estimate': 0
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
                        'damage_estimate': 0
                    }
                vehicle_stats[vehicle_number]['not_at_fault_count'] += 1
                vehicle_stats[vehicle_number]['damage_estimate'] += parse_amount(accident.get('피해견적', 0))
        
        # 금액 포맷팅
        def format_amount(amount):
            return f"{amount:,}" if amount > 0 else "0"
        
        for driver in driver_stats.values():
            driver['repair_payment'] = format_amount(driver['repair_payment'])
            driver['treatment_payment'] = format_amount(driver['treatment_payment'])
            driver['damage_estimate'] = format_amount(driver['damage_estimate'])
        
        for vehicle in vehicle_stats.values():
            vehicle['damage_estimate'] = format_amount(vehicle['damage_estimate'])
        
        # 요약 데이터 추가
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
            'vehicle_stats': list(vehicle_stats.values())
        }
    
    filepath = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
    print(f"JSON 저장 경로: {filepath}")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("JSON 파일 저장 완료")


def load_accident_data():
    """저장된 사고 데이터를 불러옴"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:  # 파일이 비어있지 않은 경우에만 파싱
                    data = json.loads(content)
                else:
                    print(f"accident_data.json 파일이 비어있습니다.")
                    return None
        except json.JSONDecodeError as e:
            print(f"accident_data.json JSON 파싱 오류: {e}")
            return None
        except Exception as e:
            print(f"accident_data.json 읽기 오류: {e}")
            return None
            
        # 요약 데이터 생성
        if data and ('at_fault' in data or 'not_at_fault' in data):
            at_fault_data = data.get('at_fault', [])
            not_at_fault_data = data.get('not_at_fault', [])
            
            # 기본 통계
            total_count = len(at_fault_data) + len(not_at_fault_data)
            at_fault_count = len(at_fault_data)
            not_at_fault_count = len(not_at_fault_data)
            at_fault_pending_count = sum(1 for a in at_fault_data if a.get('처리여부', '') == '미결')
            not_at_fault_pending_count = sum(1 for a in not_at_fault_data if a.get('처리여부', '') == '미결')
            
            # 금액 통계
            def parse_amount(amount_str):
                if not amount_str or amount_str == '' or amount_str == '-':
                    return 0
                try:
                    return int(str(amount_str).replace(',', ''))
                except:
                    return 0
            
            at_fault_total_repair = sum(parse_amount(a.get('수리지급', 0)) for a in at_fault_data)
            at_fault_total_treatment = sum(parse_amount(a.get('치료지급', 0)) for a in at_fault_data)
            not_at_fault_total_damage = sum(parse_amount(a.get('피해견적', 0)) for a in not_at_fault_data)
            not_at_fault_total_payment = sum(parse_amount(a.get('금액', 0)) for a in not_at_fault_data)
            
            # 기사별 통계
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
                            'damage_estimate': 0
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
                            'damage_estimate': 0
                        }
                    driver_stats[driver_name]['not_at_fault_count'] += 1
                    driver_stats[driver_name]['damage_estimate'] += parse_amount(accident.get('피해견적', 0))
            
            # 차량별 통계
            vehicle_stats = {}
            for accident in at_fault_data:
                vehicle_number = accident.get('차번', '')
                if vehicle_number:
                    if vehicle_number not in vehicle_stats:
                        vehicle_stats[vehicle_number] = {
                            'number': vehicle_number,
                            'at_fault_count': 0,
                            'not_at_fault_count': 0,
                            'damage_estimate': 0
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
                            'damage_estimate': 0
                        }
                    vehicle_stats[vehicle_number]['not_at_fault_count'] += 1
                    vehicle_stats[vehicle_number]['damage_estimate'] += parse_amount(accident.get('피해견적', 0))
            
            # 금액 포맷팅
            def format_amount(amount):
                return f"{amount:,}" if amount > 0 else "0"
            
            for driver in driver_stats.values():
                driver['repair_payment'] = format_amount(driver['repair_payment'])
                driver['treatment_payment'] = format_amount(driver['treatment_payment'])
                driver['damage_estimate'] = format_amount(driver['damage_estimate'])
            
            for vehicle in vehicle_stats.values():
                vehicle['damage_estimate'] = format_amount(vehicle['damage_estimate'])
            
            # 요약 데이터 추가
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
                'vehicle_stats': list(vehicle_stats.values())
            }
        
        return data
    return None

@app.route('/map')
@login_required
def map():
    return render_template('map.html')

# 운전기사 데이터 저장/불러오기 함수

def save_driver_data(data):
    print("=== save_driver_data 함수 시작 ===")
    filepath = os.path.join(app.config['DATA_FOLDER'], 'driver_data.json')
    print(f"JSON 저장 경로: {filepath}")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("JSON 파일 저장 완료")


def load_driver_data():
    filepath = os.path.join(app.config['DATA_FOLDER'], 'driver_data.json')
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:  # 파일이 비어있지 않은 경우에만 파싱
                    return json.loads(content)
                else:
                    print(f"driver_data.json 파일이 비어있습니다.")
                    return None
        except json.JSONDecodeError as e:
            print(f"driver_data.json JSON 파싱 오류: {e}")
            return None
        except Exception as e:
            print(f"driver_data.json 읽기 오류: {e}")
            return None
    return None


def save_car_maintenance_events(events, year=None):
    if year is None and events:
        try:
            year = int(events[0].get('date', '')[:4])
        except (ValueError, TypeError):
            year = datetime.now().year
    filepath = os.path.join(app.config['DATA_FOLDER'], 'car_maintenance_data.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump({'year': year, 'events': events}, f, ensure_ascii=False, indent=2)


def load_car_maintenance_events():
    filepath = os.path.join(app.config['DATA_FOLDER'], 'car_maintenance_data.json')
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    events = data.get('events', [])
                    year = data.get('year')
                    if year is None and events:
                        try:
                            year = int(events[0].get('date', '')[:4])
                        except (ValueError, TypeError):
                            year = datetime.now().year
                    elif year is None:
                        year = datetime.now().year
                    return events, year
                return [], datetime.now().year
        except (json.JSONDecodeError, Exception):
            return [], datetime.now().year
    return [], datetime.now().year


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


def _maintenance_symbol(row, col_map):
    """엑셀 행에서 정비 마킹 컬럼(예방정비○, 일반정비△, 사고수리□, 검사!, 명일 정비예정m) 확인 후 기호 반환."""
    symbol_cols = [
        ('예방정비', '○', ['예방정비']),
        ('일반정비', '△', ['일반정비']),
        ('사고수리', '□', ['사고수리']),
        ('검사', '!', ['검사']),
        ('명일_정비예정', 'm', ['명일_정비예정', '명일 정비예정']),
    ]
    for col_name, symbol, candidates in symbol_cols:
        key = _find_col_key(col_map, *candidates)
        if key is None:
            continue
        val = row.get(key)
        if pd.isna(val):
            continue
        s = str(val).strip()
        if not s:
            continue
        if symbol in s or (col_name == '검사' and '!' in s) or ('명일' in col_name and 'm' in s.lower()):
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


def parse_maintenance_excel(file_path):
    """차량정비 엑셀 1월~12월 시트를 모두 파싱해 이벤트 리스트 반환. 엔진오일/미션오일/타이어_교체/타이어_펑크 컬럼은 사용하지 않음."""
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
                차종 = _norm_cell_str(row.get('차종'))
                등록일자 = row.get('등록일자')
                if pd.notna(등록일자) and hasattr(등록일자, 'strftime'):
                    등록일자 = 등록일자.strftime('%Y-%m-%d')
                else:
                    등록일자 = _norm_cell_str(등록일자)
                symbol = _maintenance_symbol(row.to_dict(), col_map)
                if not symbol:
                    continue
                events.append({
                    'date': date_str,
                    '차번': 차번,
                    '차종': 차종,
                    '등록일자': 등록일자,
                    'symbol': symbol
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
    count_예방정비 = count_일반정비 = count_사고수리 = count_검사 = count_명일정비 = 0
    for e in events:
        if not e.get('date', '').startswith(ym):
            continue
        key = (e.get('차번', ''), e.get('차종', ''), e.get('등록일자', ''))
        vehicles.add(key)
        차종 = e.get('차종', '').strip() or '-'
        if 차종 not in vehicles_by_차종:
            vehicles_by_차종[차종] = set()
        vehicles_by_차종[차종].add(key)
        s = e.get('symbol', '')
        if s == '○':
            count_예방정비 += 1
        elif s == '△':
            count_일반정비 += 1
        elif s == '□':
            count_사고수리 += 1
        elif s == '!':
            count_검사 += 1
        elif s == 'm':
            count_명일정비 += 1
    by_차종 = [(차종, len(s)) for 차종, s in vehicles_by_차종.items()]
    by_차종.sort(key=lambda x: -x[1])
    month_label = ym.split('-')[1] if len(ym) >= 7 else ''
    return {
        'month_label': month_label,
        'total_vehicles': len(vehicles),
        'by_차종': by_차종,
        '예방정비': count_예방정비,
        '일반정비': count_일반정비,
        '사고수리': count_사고수리,
        '검사': count_검사,
        '명일정비': count_명일정비,
    }


@app.route('/driver', methods=['GET', 'POST'])
@login_required
def driver():
    print("=== /driver 라우트 호출됨 ===")
    print(f"요청 메서드: {request.method}")
    print(f"현재 사용자: {current_user.username if current_user else 'None'}")
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
    required_columns = ['사번', '이름', '나이', '주민등록번호', '면허번호', '갱신시작', '갱신마감', '입사일자', '퇴사일자', '연락처', '거주지']
    if request.method == 'POST':
        print("POST 요청 받음")
        if 'excel_file' not in request.files:
            return render_template('driver.html', error='파일이 선택되지 않았습니다.', driver_data=load_driver_data(), messages=messages, current_user=current_user)
        file = request.files['excel_file']
        print(f"파일명: {file.filename}")
        if file.filename == '':
            return render_template('driver.html', error='파일이 선택되지 않았습니다.', driver_data=load_driver_data(), messages=messages, current_user=current_user)
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
                    return render_template('driver.html', error=error_msg, driver_data=load_driver_data(), messages=messages, current_user=current_user)
                for col in required_columns:
                    if col not in df.columns:
                        df[col] = ''
                # 기사관리 날짜 컬럼별 포맷 지정
                date_cols = ['갱신시작', '갱신마감', '입사일자', '퇴사일자']
                for col in date_cols:
                    if col in df.columns:
                        try:
                            df[col] = pd.to_datetime(df[col], format='%Y-%m-%d', errors='coerce').dt.strftime('%Y-%m-%d').fillna('')
                        except:
                            df[col] = df[col].astype(str).str.strip()
                kst = pytz.timezone('Asia/Seoul')
                upload_time = pd.Timestamp.now(tz=kst).strftime('%Y-%m-%d %H:%M:%S')
                driver_list = df[required_columns].fillna('').astype(str).to_dict('records')
                driver_data = {
                    'list': driver_list,
                    'columns': required_columns
                }
                # 파일로 저장 
                save_driver_data(driver_data)
                # UploadRecord 데이터베이스 저장
                flask_url = url_for('uploaded_file', filename=os.path.basename(file_path), _external=True)
                record = UploadRecord(filename=filename, uploader=current_user.name, github_url=flask_url, upload_type='driver')
                db.session.add(record)
                db.session.commit()
                return render_template('driver.html', driver_data=driver_data, messages=messages, current_user=current_user)
            except Exception as e:
                return render_template('driver.html', error=f'파일 처리 중 오류: {str(e)}', driver_data=load_driver_data(), messages=messages, current_user=current_user)
        else:
            return render_template('driver.html', error='허용되지 않은 파일 형식입니다.', driver_data=load_driver_data(), messages=messages, current_user=current_user)
    # GET 요청
    return render_template('driver.html', driver_data=load_driver_data(), messages=messages, current_user=current_user)

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
            if upload_type == 'car_maintenance':
                events, year_from_file, parse_err = parse_maintenance_excel(file_path)
                if parse_err:
                    return render_template('car.html', error=parse_err, car_data=car_data, messages=messages, current_user=current_user,
                        maintenance_headers=[], maintenance_table_data=[], maintenance_available_months=[], maintenance_selected_month=None)
                save_car_maintenance_events(events, year_from_file)
            flask_url = url_for('uploaded_file', filename=os.path.basename(file_path), _external=True)
            record = UploadRecord(filename=filename, uploader=current_user.name, github_url=flask_url, upload_type=upload_type)
            db.session.add(record)
            db.session.commit()
        except Exception as e:
            return render_template('car.html', error=f'파일 처리 중 오류: {str(e)}', car_data=car_data, messages=messages, current_user=current_user,
                maintenance_headers=[], maintenance_table_data=[], maintenance_available_months=[], maintenance_selected_month=None)
    # GET 또는 POST 성공 후: 차량정비 월별 테이블용 데이터 (1월~12월 시트에 맞춰 12개 월 버튼 표시)
    events, maintenance_year = load_car_maintenance_events()
    maintenance_headers = []
    maintenance_table_data = []
    maintenance_available_months = []
    maintenance_selected_month = None
    if events:
        maintenance_available_months = [f'{maintenance_year}-{m:02d}' for m in range(1, 13)]
        # URL에 month가 있으면 사용, 없으면 데이터가 있는 월 중 가장 최근 월을 기본 선택
        requested_month = request.args.get('month')
        if requested_month and requested_month in maintenance_available_months:
            maintenance_selected_month = requested_month
        else:
            dates_with_data = sorted(set(e.get('date', '')[:7] for e in events if e.get('date')), reverse=True)
            maintenance_selected_month = dates_with_data[0] if dates_with_data else (maintenance_available_months[0] if maintenance_available_months else None)
            if not maintenance_selected_month and maintenance_available_months:
                maintenance_selected_month = maintenance_available_months[0]
        if maintenance_selected_month and maintenance_selected_month in maintenance_available_months:
            maintenance_headers, maintenance_table_data = build_maintenance_table(events, maintenance_selected_month)
    maintenance_stats = build_maintenance_stats(events, maintenance_selected_month) if (events and maintenance_selected_month) else None
    return render_template('car.html', car_data=car_data, messages=messages, current_user=current_user,
        maintenance_headers=maintenance_headers, maintenance_table_data=maintenance_table_data,
        maintenance_available_months=maintenance_available_months, maintenance_selected_month=maintenance_selected_month,
        maintenance_stats=maintenance_stats)

@app.route('/driver/profile/<driver_id>')
@login_required
def driver_profile(driver_id):
    driver_data = load_driver_data()
    driver_info = None
    if driver_data and 'list' in driver_data:
        for d in driver_data['list']:
            if d['사번'] == driver_id:
                driver_info = d
                break
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
        work_type = ''
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
    accident_data = None
    try:
        with open('data/accident_data.json', 'r', encoding='utf-8') as f:
            accident_data = json.load(f)
    except:
        accident_data = None
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
        all_accidents = at_fault + not_at_fault
        from datetime import datetime
        def parse_dt(x):
            try:
                return datetime.strptime(x.get('사고일시',''), '%Y-%m-%d %H:%M')
            except:
                return datetime.min
        all_accidents_sorted = sorted(all_accidents, key=parse_dt, reverse=True)
        accident_rows = []
        for a in all_accidents_sorted:
            accident_rows.append(f"<tr><td>{a.get('사고번호','')}</td><td>{a.get('사고일시','')}</td><td>{a.get('차번','')}</td><td>{a.get('접보사항','')}</td><td>{a.get('처리여부','')}</td></tr>")
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
                <span>👤</span>
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
                    <tr><td class="label">면허번호</td><td>{driver_info.get('면허번호','')}</td></tr>
                    <tr><td class="label">갱신시작</td><td>{driver_info.get('갱신시작','').split(' ')[0] if driver_info.get('갱신시작') else ''}</td></tr>
                    <tr><td class="label">갱신마감</td><td>{driver_info.get('갱신마감','').split(' ')[0] if driver_info.get('갱신마감') else ''}</td></tr>
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
    accident_data = load_accident_data()
    driver_data = load_driver_data()
    lease_data = load_lease_data()

    source_list_name = 'at_fault' if type == 'at_fault' else 'not_at_fault'
    template = 'accident_print_gahae.html' if type == 'at_fault' else 'accident_print_pihae.html'

    accident_info = next((a for a in accident_data.get(source_list_name, []) if str(a.get('사고번호')) == str(accident_no)), None)
    
    if not accident_info:
        return '해당 사고 정보를 찾을 수 없습니다.', 404

    context = accident_info.copy()

    driver_name = context.get('기사명')
    driver_info = {}
    if driver_name and driver_data:
        driver_info = next((d for d in driver_data.get('list', []) if d.get('이름') == driver_name), {})
    
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
    version = data.get('version')
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
    version = data.get('version')
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
    
    version = request.args.get('version')
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
        position = request.form.get('position')
        # 이메일 중복 체크 (자신 제외)
        if email != user.email:
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                flash('이미 사용 중인 이메일입니다.', 'error')
                messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
                return render_template('profile.html', user=user, messages=messages, current_user=current_user)
        # 이메일과 이름 업데이트
        user.email = email
        user.name = name
        user.phone = phone
        user.position = position
        # 비밀번호 변경 요청이 있는 경우
        if current_password and new_password:
            if not user.check_password(current_password):
                flash('현재 비밀번호가 올바르지 않습니다.', 'error')
                messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
                return render_template('profile.html', user=user, messages=messages, current_user=current_user)
            if new_password != confirm_password:
                flash('새 비밀번호가 일치하지 않습니다.', 'error')
                messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
                return render_template('profile.html', user=user, messages=messages, current_user=current_user)
            user.set_password(new_password)
            flash('비밀번호가 변경되었습니다.', 'success')
        # 데이터베이스에 저장
        db.session.commit()
        flash('프로필이 업데이트되었습니다.', 'success')
        return redirect(url_for('profile'))
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
    return render_template('profile.html', user=current_user, messages=messages, current_user=current_user)

@app.route('/admin/users')
@login_required
def admin_users():
    if current_user.role != 'admin':
        flash('관리자만 접근할 수 있습니다.', 'error')
        return redirect(url_for('calculate_salary'))
    
    users = User.query.all()
    return render_template('admin_users.html', users=users)

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

if __name__ == '__main__':
    print("=== Flask 앱 시작 ===")
    
    create_database()  # 데이터베이스 생성
    print("=== 데이터베이스 생성 완료 ===")
    print("=== Flask 앱 실행 중... ===")
    app.run(host='127.0.0.1', port=5000, debug=True)