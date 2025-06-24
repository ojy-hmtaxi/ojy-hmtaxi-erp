from flask import Flask, render_template, request, session, redirect, url_for, flash, send_from_directory, jsonify
import pandas as pd
import os
import json
from datetime import timedelta
from collections import OrderedDict
from werkzeug.utils import secure_filename
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from models import db, User, Message
from sqlalchemy.orm import joinedload
import base64

app = Flask(__name__)
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
                        return render_template('index.html', 
                                            error="엑셀 파일에 '실입금', '리스료', '연료비' 컬럼이 있는 시트가 없습니다.")
                    
                    session['salary_data'] = salary_data
                    session['salary_calculated'] = True
                    
                    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(100).all()
                    return render_template('index.html', 
                                        salary_data=salary_data,
                                        calculated=True,
                                        messages=messages,
                                        current_user=current_user)
                except Exception as e:
                    return render_template('index.html', 
                                        error=f"엑셀 파일 처리 중 오류가 발생했습니다: {str(e)}")
    
    # GET 요청이거나 세션에 저장된 데이터가 있는 경우
    salary_data = session.get('salary_data', None)
    calculated = session.get('salary_calculated', False)
    
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
    return render_template('index.html',
                        salary_data=salary_data,
                        calculated=calculated,
                        messages=messages,
                        current_user=current_user)

@app.route('/schedule', methods=['GET', 'POST'])
@login_required
def schedule():
    if request.method == 'POST':
        if 'excel_file' in request.files:
            file = request.files['excel_file']
            if file.filename != '':
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
                file.save(filepath)
                
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
                                processed_data.append(row_dict)
                            
                            dispatch_data[sheet] = {
                                'headers': [str(col) for col in df.columns],
                                'data': processed_data
                            }
                        except:
                            continue
                    
                    if not dispatch_data:
                        return render_template('schedule.html', 
                                            error="엑셀 파일에서 읽을 수 있는 시트가 없습니다.")
                    
                    # 파일로 저장
                    save_dispatch_data(dispatch_data)
                    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
                    return render_template('schedule.html', dispatch_data=dispatch_data, messages=messages, current_user=current_user)
                except Exception as e:
                    return render_template('schedule.html', 
                                        error=f"엑셀 파일 처리 중 오류가 발생했습니다: {str(e)}")
    
    # GET 요청이거나 저장된 데이터가 있는 경우
    dispatch_data = load_dispatch_data()
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
    return render_template('schedule.html', dispatch_data=dispatch_data, messages=messages, current_user=current_user)

@app.route('/pay_lease', methods=['GET', 'POST'])
@login_required
def pay_lease():
    if request.method == 'POST':
        if 'excel_file' in request.files:
            file = request.files['excel_file']
            if file.filename != '':
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
                        return render_template('pay_lease.html', 
                                            error="엑셀 파일에 '실입금', '리스료', '연료비' 컬럼이 있는 시트가 없습니다.")
                    
                    # 파일로 저장
                    save_lease_data(salary_data)
                    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
                    return render_template('pay_lease.html', salary_data=salary_data, messages=messages, current_user=current_user)
                except Exception as e:
                    return render_template('pay_lease.html', 
                                        error=f"엑셀 파일 처리 중 오류가 발생했습니다: {str(e)}")
    
    # GET 요청이거나 저장된 데이터가 있는 경우
    salary_data = load_lease_data()
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
    return render_template('pay_lease.html', salary_data=salary_data, messages=messages, current_user=current_user)

@app.route('/accident', methods=['GET', 'POST'])
@login_required
def accident():
    if request.method == 'POST':
        if 'excel_file' not in request.files:
            flash('파일이 선택되지 않았습니다.', 'error')
            return redirect(request.url)
        
        file = request.files['excel_file']
        if file.filename == '':
            flash('파일이 선택되지 않았습니다.', 'error')
            return redirect(request.url)
        
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            
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
                        # 날짜/시간 컬럼 변환
                        if '일시' in col or '일' in col:
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
                
                save_accident_data(accident_data)
                
                # 업로드 정보 저장
                session['last_accident_file'] = filename
                session['upload_time'] = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
                session['uploader_name'] = current_user.name if hasattr(current_user, 'name') else current_user.username
                
                flash(f'<{filename}> 파일이 성공적으로 업로드되었습니다. (업로드 일시: {session.get("upload_time")})', 'success')

            except Exception as e:
                flash(f'파일 처리 중 오류 발생: {e}', 'error')
                
            return redirect(url_for('accident'))

    accident_data = load_accident_data()
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
    upload_info = {
        'filename': session.get('last_accident_file'),
        'upload_time': session.get('upload_time'),
        'uploader_name': session.get('uploader_name')
    }

    return render_template('accident.html', accident_data=accident_data, messages=messages, current_user=current_user, upload_info=upload_info)

@app.route('/add_message', methods=['POST'])
@login_required
def add_message():
    content = request.form.get('content', '').strip()
    if len(content) > 60:
        flash('메시지는 60자 이내로 작성해주세요.', 'error')
        return redirect(url_for('calculate_salary'))
    
    if content:
        message = Message(content=content, user_id=current_user.id)
        db.session.add(message)
        db.session.commit()
        print('메시지 저장됨:', message.content)
        print('DB 메시지 수:', Message.query.count())
        flash('메시지가 등록되었습니다.', 'success')
    return redirect(url_for('calculate_salary'))

@app.route('/delete_message/<int:message_id>', methods=['POST'])
@login_required
def delete_message(message_id):
    message = Message.query.get_or_404(message_id)
    if message.user_id == current_user.id or current_user.role == 'admin':
        db.session.delete(message)
        db.session.commit()
        flash('메시지가 삭제되었습니다.', 'success')
    return redirect(url_for('calculate_salary'))

# 데이터베이스 생성
def create_database():
    with app.app_context():
        db.create_all()

# 세션 유지 시간을 매우 길게 설정 (365일)
app.permanent_session_lifetime = timedelta(days=365)

# 허용할 파일 확장자 설정
ALLOWED_EXTENSIONS = {'xlsx', 'xls'}

def allowed_file(filename):
    """허용된 파일 확장자인지 확인"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 업로드 폴더와 데이터 폴더가 없으면 생성
for folder in [app.config['UPLOAD_FOLDER'], app.config['DATA_FOLDER']]:
    if not os.path.exists(folder):
        os.makedirs(folder)

def save_dispatch_data(data):
    """배차 데이터를 JSON 파일로 저장"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'dispatch_data.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_dispatch_data():
    """저장된 배차 데이터를 불러옴"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'dispatch_data.json')
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def save_lease_data(data):
    """리스 급여 데이터를 JSON 파일로 저장"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'lease_data.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_lease_data():
    """저장된 리스 급여 데이터를 불러옴"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'lease_data.json')
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def save_accident_data(data):
    """사고 데이터를 JSON 파일로 저장"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_accident_data():
    """저장된 사고 데이터를 불러옴"""
    filepath = os.path.join(app.config['DATA_FOLDER'], 'accident_data.json')
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
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
    filepath = os.path.join(app.config['DATA_FOLDER'], 'driver_data.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_driver_data():
    filepath = os.path.join(app.config['DATA_FOLDER'], 'driver_data.json')
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

@app.route('/driver', methods=['GET', 'POST'])
@login_required
def driver():
    required_columns = ['사번', '이름', '나이', '주민등록번호', '면허번호', '갱신시작', '갱신마감', '입사일자', '퇴사일자', '연락처', '거주지']
    if request.method == 'POST':
        if 'excel_file' not in request.files:
            return render_template('driver.html', error='파일이 선택되지 않았습니다.', driver_data=load_driver_data())
        file = request.files['excel_file']
        if file.filename == '':
            return render_template('driver.html', error='파일이 선택되지 않았습니다.', driver_data=load_driver_data())
        if file and allowed_file(file.filename):
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(file_path)
            try:
                df = pd.read_excel(file_path, sheet_name=0)
                # 컬럼명 전처리 - 공백 제거
                df.columns = [str(col).strip() for col in df.columns]
                # 필수 컬럼 누락 체크
                missing = [col for col in required_columns if col not in df.columns]
                if missing:
                    error_msg = '다음 필수 컬럼이 누락되었습니다: ' + ', '.join(missing)
                    return render_template('driver.html', error=error_msg, driver_data=load_driver_data())
                # 누락 컬럼은 빈 값으로 추가
                for col in required_columns:
                    if col not in df.columns:
                        df[col] = ''
                # 표출 컬럼 순서 고정
                driver_list = df[required_columns].fillna('').astype(str).to_dict('records')
                driver_data = {
                    'list': driver_list,
                    'columns': required_columns
                }
                save_driver_data(driver_data)
                return render_template('driver.html', driver_data=driver_data, current_user=current_user)
            except Exception as e:
                return render_template('driver.html', error=f'파일 처리 중 오류: {str(e)}', driver_data=load_driver_data())
        else:
            return render_template('driver.html', error='허용되지 않은 파일 형식입니다.', driver_data=load_driver_data())
    # GET 요청
    return render_template('driver.html', driver_data=load_driver_data(), current_user=current_user)

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
    .profile-table {{ width:100%; border-collapse:collapse; }}
    .profile-table td {{ padding:7px 10px; color:#333; font-size:1rem; border-bottom:1px solid #f2f2f2; }}
    .profile-table tr:last-child td {{ border-bottom:none; }}
    .profile-table .label {{ color:#888; width:140px; font-weight:500; }}
    .profile-actions {{ position:absolute; top:40px; right:40px; }}
    .profile-actions button {{ margin-left:8px; padding:6px 18px; border-radius:5px; border:none; background:#eee; color:#333; font-weight:500; cursor:pointer; }}
    .profile-actions button.edit {{ background:#4CAF50; color:#fff; }}
    @media (max-width: 900px) {{ .profile-wrap {{ padding:20px 5vw; }} }}
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
        <div class="profile-section">
            <h3>기본 정보</h3>
            <table class="profile-table">
                <tr><td class="label">이름</td><td>{driver_info.get('이름','')}</td></tr>
                <tr><td class="label">사번</td><td>{driver_info.get('사번','')}</td></tr>
                <tr><td class="label">나이</td><td>{driver_info.get('나이','')}</td></tr>
                <tr><td class="label">주민등록번호</td><td>{driver_info.get('주민등록번호','')}</td></tr>
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
        <div class="profile-section">
            <h3>연락처</h3>
            <table class="profile-table">
                <tr><td class="label">연락처</td><td>{driver_info.get('연락처','')}</td></tr>
            </table>
        </div>
        <div class="profile-section">
            <h3>거주지</h3>
            <table class="profile-table">
                <tr><td class="label">거주지</td><td>{driver_info.get('거주지','')}</td></tr>
            </table>
        </div>
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
    
    driver_id = context.get('사번')
    our_car_model = '' # 운전자 차종
    if driver_id and lease_data:
        for month, month_data in lease_data.items():
            if not isinstance(month_data, dict): continue
            lease_info = next((l for l in month_data.get('data', []) if str(l.get('사번')) == str(driver_id)), None)
            if lease_info and '차종' in lease_info:
                our_car_model = lease_info.get('차종')
                break
    
    context['차종'] = our_car_model

    return render_template(template, accident=context)

@app.route('/save_map_image', methods=['POST'])
@login_required
def save_map_image():
    data = request.get_json()
    version = data.get('version')
    image_data = data.get('image')
    if not version or not image_data:
        return {'success': False, 'error': '버전명 또는 이미지 데이터 누락'}, 400
    header, encoded = image_data.split(',', 1)
    img_bytes = base64.b64decode(encoded)
    save_dir = os.path.join('uploads', 'maps')
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, f'{version}.png')
    with open(save_path, 'wb') as f:
        f.write(img_bytes)
    return {'success': True}

@app.route('/uploads/maps/<filename>')
def uploaded_map(filename):
    return send_from_directory(os.path.join('uploads', 'maps'), filename)

@app.route('/save_map_json', methods=['POST'])
@login_required
def save_map_json():
    data = request.get_json()
    version = data.get('version')
    json_data = data.get('json')
    if not version or not json_data:
        return {'success': False, 'error': '버전명 또는 JSON 데이터 누락'}, 400
    save_dir = os.path.join('uploads', 'maps')
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, f'{version}.json')
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write(json_data)
    return {'success': True}

@app.route('/load_map_json')
@login_required
def load_map_json():
    version = request.args.get('version')
    if not version:
        return jsonify({'success': False, 'error': '버전명 누락'}), 400
    load_path = os.path.join('uploads', 'maps', f'{version}.json')
    if not os.path.exists(load_path):
        return jsonify({'success': False, 'error': '해당 버전의 지도 데이터가 없습니다.'}), 404
    with open(load_path, 'r', encoding='utf-8') as f:
        json_data = f.read()
    return jsonify({'success': True, 'json': json_data})

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
        user.role = request.form.get('role')
        
        new_password = request.form.get('new_password')
        if new_password:
            user.set_password(new_password)
        
        db.session.commit()
        flash('사용자 정보가 업데이트되었습니다.', 'success')
        return redirect(url_for('admin_users'))
    
    return render_template('admin_edit_user.html', user=user)

if __name__ == '__main__':
    create_database()  # 데이터베이스 생성
    app.run(host='127.0.0.1', port=5000, debug=True)