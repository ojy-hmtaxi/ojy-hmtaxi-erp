from flask import Flask, render_template, request, session, redirect, url_for, flash
import pandas as pd
import os
import json
from datetime import timedelta
from collections import OrderedDict
from werkzeug.utils import secure_filename
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from models import db, User, Message
from sqlalchemy.orm import joinedload

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
        
        if password != confirm_password:
            return render_template('register.html', error='비밀번호가 일치하지 않습니다.')
            
        if User.query.filter_by(username=username).first():
            return render_template('register.html', error='이미 존재하는 아이디입니다.')
            
        if User.query.filter_by(email=email).first():
            return render_template('register.html', error='이미 존재하는 이메일입니다.')
            
        user = User(username=username, email=email, name=name)
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
                            
                            # 사번, 드라이버, 차종 컬럼이 있는 경우 포함, 없는 경우 빈 문자열로 처리
                            additional_columns = ['사번', '드라이버', '차종']
                            for col in additional_columns:
                                if col not in df.columns:
                                    df[col] = ''
                            
                            # 데이터 저장 시 추가 컬럼 포함
                            columns_to_save = ['사번', '드라이버', '차종', '실입금', '리스료', '연료비', '급여']
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
                    
                    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
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
                            
                            # 사번, 드라이버, 차종 컬럼이 있는 경우 포함, 없는 경우 빈 문자열로 처리
                            additional_columns = ['사번', '드라이버', '차종']
                            for col in additional_columns:
                                if col not in df.columns:
                                    df[col] = ''
                            
                            # 데이터 저장 시 추가 컬럼 포함
                            columns_to_save = ['사번', '드라이버', '차종', '실입금', '리스료', '연료비', '급여']
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
            return render_template('accident.html', error='파일이 선택되지 않았습니다.', 
                                accident_data=load_accident_data())
        
        file = request.files['excel_file']
        if file.filename == '':
            return render_template('accident.html', error='파일이 선택되지 않았습니다.',
                                accident_data=load_accident_data())
        
        if file and allowed_file(file.filename):
            # 원본 파일명 그대로 사용
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(file_path)
            
            try:
                # 가해사고와 피해사고 시트 읽기
                at_fault_df = pd.read_excel(file_path, sheet_name='가해사고')
                not_at_fault_df = pd.read_excel(file_path, sheet_name='피해사고')
                
                print("\n=== 디버그 정보 ===")
                print("피해사고 시트 컬럼 목록:")
                for idx, col in enumerate(not_at_fault_df.columns):
                    print(f"{idx+1}. '{col}' (타입: {type(col)})")
                
                # 컬럼명 전처리 - 공백 제거 및 정규화
                not_at_fault_df.columns = [str(col).strip() for col in not_at_fault_df.columns]
                
                # 피해사고 필수 컬럼 - 실제 엑셀 파일의 컬럼명과 정확히 일치하도록 수정
                not_at_fault_columns = ['사고번호', '사고일시', '사고장소', '사고원인', '자차과실', '경찰서', 
                                      '차번', '기사명', '피해견적', '수리처', '차번', '차종', '운전자', 
                                      '연락처', '보험사', '접보번호', '담당자', '연락처', '입금일', 
                                      '금액', '처리여부', '비고']
                
                print("\n처리된 컬럼 목록:")
                print("엑셀 파일의 실제 컬럼:", list(not_at_fault_df.columns))
                print("필요한 컬럼:", not_at_fault_columns)
                
                # 컬럼 존재 여부 확인 (공백을 제거하고 비교)
                missing_not_at_fault = [col for col in not_at_fault_columns 
                                      if not any(existing.strip() == col.strip() 
                                               for existing in not_at_fault_df.columns)]
                
                print("누락된 컬럼:", missing_not_at_fault)
                
                if missing_not_at_fault:
                    error_msg = "다음 필수 컬럼이 누락되었습니다:\n"
                    error_msg += "\n피해사고 시트 누락 컬럼: " + ", ".join(missing_not_at_fault)
                    return render_template('accident.html', error=error_msg)
                
                # 입금일 컬럼을 datetime 타입으로 변환
                not_at_fault_df['입금일'] = pd.to_datetime(not_at_fault_df['입금일'], errors='coerce')
                # 가해사고 시간 컬럼을 datetime 타입으로 변환
                at_fault_df['사고일시'] = pd.to_datetime(at_fault_df['사고일시'], errors='coerce')
                
                # 데이터 처리
                accident_data = {
                    'at_fault': [],
                    'not_at_fault': [],
                    'summary': {
                        'total_count': len(at_fault_df) + len(not_at_fault_df),
                        'at_fault_count': len(at_fault_df),
                        'not_at_fault_count': len(not_at_fault_df),
                        'at_fault_pending_count': len(at_fault_df[at_fault_df['처리여부'] == '미결']),
                        'not_at_fault_pending_count': len(not_at_fault_df[not_at_fault_df['처리여부'] == '미결']),
                        'total_pending_count': len(at_fault_df[at_fault_df['처리여부'] == '미결']) + 
                                            len(not_at_fault_df[not_at_fault_df['처리여부'] == '미결'])
                    }
                }
                
                # 기사별 통계
                driver_stats = {}
                for _, row in at_fault_df.iterrows():
                    driver = str(row['기사명'])
                    if driver not in driver_stats:
                        driver_stats[driver] = {
                            'at_fault_count': 0,
                            'not_at_fault_count': 0,
                            'treatment_payment': 0,
                            'repair_payment': 0,
                            'damage_estimate': 0
                        }
                    driver_stats[driver]['at_fault_count'] += 1
                    try:
                        treatment = str(row['치료지급']).replace(',', '')
                        if treatment.strip() and treatment != '-':
                            driver_stats[driver]['treatment_payment'] += float(treatment)
                    except (ValueError, TypeError):
                        pass
                    try:
                        repair = str(row['수리지급']).replace(',', '')
                        if repair.strip() and repair != '-':
                            driver_stats[driver]['repair_payment'] += float(repair)
                    except (ValueError, TypeError):
                        pass

                for _, row in not_at_fault_df.iterrows():
                    driver = str(row['기사명'])
                    if driver not in driver_stats:
                        driver_stats[driver] = {
                            'at_fault_count': 0,
                            'not_at_fault_count': 0,
                            'treatment_payment': 0,
                            'repair_payment': 0,
                            'damage_estimate': 0
                        }
                    driver_stats[driver]['not_at_fault_count'] += 1
                    try:
                        damage = str(row['피해견적']).replace(',', '')
                        if damage.strip() and damage != '-':
                            driver_stats[driver]['damage_estimate'] += float(damage)
                    except (ValueError, TypeError):
                        pass

                # 차량별 통계
                vehicle_stats = {}
                for _, row in at_fault_df.iterrows():
                    vehicle = str(row['차량번호']).split('.')[0]
                    if vehicle not in vehicle_stats:
                        vehicle_stats[vehicle] = {
                            'at_fault_count': 0,
                            'not_at_fault_count': 0,
                            'damage_estimate': 0
                        }
                    vehicle_stats[vehicle]['at_fault_count'] += 1

                for _, row in not_at_fault_df.iterrows():
                    vehicle = str(row['차번']).split('.')[0]
                    if vehicle not in vehicle_stats:
                        vehicle_stats[vehicle] = {
                            'at_fault_count': 0,
                            'not_at_fault_count': 0,
                            'damage_estimate': 0
                        }
                    vehicle_stats[vehicle]['not_at_fault_count'] += 1
                    try:
                        damage = str(row['피해견적']).replace(',', '')
                        if damage.strip() and damage != '-':
                            vehicle_stats[vehicle]['damage_estimate'] += float(damage)
                    except (ValueError, TypeError):
                        pass

                # 통계 정보를 정렬하여 저장
                accident_data['summary']['driver_stats'] = [
                    {
                        'name': driver,
                        'at_fault_count': stats['at_fault_count'],
                        'not_at_fault_count': stats['not_at_fault_count'],
                        'treatment_payment': format(int(stats['treatment_payment']) if stats['treatment_payment'] > 0 else 0, ','),
                        'repair_payment': format(int(stats['repair_payment']) if stats['repair_payment'] > 0 else 0, ','),
                        'damage_estimate': format(int(stats['damage_estimate']) if stats['damage_estimate'] > 0 else 0, ',')
                    }
                    for driver, stats in sorted(driver_stats.items(), 
                                             key=lambda x: (x[1]['at_fault_count'] + x[1]['not_at_fault_count']), 
                                             reverse=True)
                ]

                accident_data['summary']['vehicle_stats'] = [
                    {
                        'number': vehicle,
                        'at_fault_count': stats['at_fault_count'],
                        'not_at_fault_count': stats['not_at_fault_count'],
                        'damage_estimate': format(int(stats['damage_estimate']) if stats['damage_estimate'] > 0 else 0, ',')
                    }
                    for vehicle, stats in sorted(vehicle_stats.items(), 
                                             key=lambda x: (x[1]['at_fault_count'] + x[1]['not_at_fault_count']), 
                                             reverse=True)
                ]
                
                # 가해사고 데이터 처리
                for _, row in at_fault_df.iterrows():
                    # 차량번호에서 소수점 제거
                    car_number = str(row['차량번호']).split('.')[0] if pd.notna(row['차량번호']) else ''
                    # 사고번호에서 소수점 제거
                    accident_number = str(row['사고번호']).split('.')[0] if pd.notna(row['사고번호']) else ''
                    # 시간에서 초를 제외하고 표시 (YYYY-MM-DD HH:mm 형식)
                    accident_time = row['사고일시'].strftime('%Y-%m-%d %H:%M') if pd.notna(row['사고일시']) else ''
                    
                    accident_data['at_fault'].append({
                        '사고번호': accident_number,
                        '사고일시': accident_time,
                        '차량번호': car_number,
                        '기사명': str(row['기사명']),
                        '사고원인': str(row['사고원인']),
                        '접보사항': str(row['접보사항']) if pd.notna(row['접보사항']) else '',
                        '상해': str(row['상해']) if pd.notna(row['상해']) else '',
                        '피해자': str(row['피해자']) if pd.notna(row['피해자']) else '',
                        '치료지급': str(row['치료지급']) if pd.notna(row['치료지급']) else '',
                        '운전자': str(row['운전자']) if pd.notna(row['운전자']) else '',
                        '차종': str(row['차종']) if pd.notna(row['차종']) else '',
                        '수리지급': str(row['수리지급']) if pd.notna(row['수리지급']) else '',
                        '처리여부': str(row['처리여부']) if pd.notna(row['처리여부']) else ''
                    })
                
                # 피해사고 데이터 처리
                for _, row in not_at_fault_df.iterrows():
                    # 차량번호에서 소수점 제거
                    car_number = str(row['차번']).split('.')[0] if pd.notna(row['차번']) else ''
                    # 사고번호에서 소수점 제거
                    accident_number = str(row['사고번호']).split('.')[0] if pd.notna(row['사고번호']) else ''
                    # 입금일 날짜만 표시 (YYYY-MM-DD 형식)
                    payment_date = row['입금일'].strftime('%Y-%m-%d') if pd.notna(row['입금일']) else ''
                    
                    # 사고일시 형식 변경 (YYYY-MM-DD HH:mm)
                    try:
                        accident_time = pd.to_datetime(row['사고일시']).strftime('%Y-%m-%d %H:%M') if pd.notna(row['사고일시']) else ''
                    except:
                        accident_time = str(row['사고일시'])
                    
                    # 피해견적 처리
                    try:
                        damage_val = str(row['피해견적']).strip()
                        damage_estimate = format(int(float(damage_val)), ',') if damage_val != '-' else '-'
                    except (ValueError, TypeError):
                        damage_estimate = '-'
                        
                    # 금액 처리
                    try:
                        amount_val = str(row['금액']).strip()
                        payment_amount = format(int(float(amount_val)), ',') if amount_val != '-' else '-'
                    except (ValueError, TypeError):
                        payment_amount = '-'
                    
                    accident_data['not_at_fault'].append({
                        '사고번호': accident_number,
                        '사고일시': accident_time,
                        '차량번호': car_number,
                        '기사명': str(row['기사명']),
                        '사고원인': str(row['사고원인']),
                        '피해견적': damage_estimate,
                        '입금일': payment_date,
                        '금액': payment_amount,
                        '처리여부': str(row['처리여부']) if pd.notna(row['처리여부']) else ''
                    })
                
                # 데이터를 파일로 저장
                save_accident_data(accident_data)
                messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
                return render_template('accident.html', accident_data=accident_data, messages=messages, current_user=current_user)
                
            except Exception as e:
                return render_template('accident.html', 
                                    error=f'파일 처리 중 오류가 발생했습니다: {str(e)}',
                                    accident_data=load_accident_data())
            
        else:
            return render_template('accident.html', 
                                error='허용되지 않은 파일 형식입니다.',
                                accident_data=load_accident_data())
    
    # GET 요청이거나 저장된 데이터가 있는 경우
    messages = Message.query.options(joinedload(Message.author)).order_by(Message.timestamp.desc()).limit(30).all()
    return render_template('accident.html', accident_data=load_accident_data(), messages=messages, current_user=current_user)

@app.route('/add_message', methods=['POST'])
@login_required
def add_message():
    content = request.form.get('content', '').strip()
    if len(content) > 50:
        flash('메시지는 50자 이내로 작성해주세요.', 'error')
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
            return json.load(f)
    return None

if __name__ == '__main__':
    create_database()  # 데이터베이스 생성
    app.run(host='127.0.0.1', port=5000, debug=True) 