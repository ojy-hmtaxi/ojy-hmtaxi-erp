from app import app, db
from models import User

def create_admin():
    with app.app_context():
        # 기존 관리자 계정이 있는지 확인
        existing_admin = User.query.filter_by(username='hmmaster').first()
        if existing_admin:
            print("관리자 계정이 이미 존재합니다.")
            return
        
        # 새 관리자 계정 생성
        admin = User(
            username='hmmaster',
            email='admin@hanmi.com',
            name='관리자',
            role='admin',
            phone='',
            position='관리자'
        )
        admin.set_password('hmtaxi1234!')
        
        db.session.add(admin)
        db.session.commit()
        print("관리자 계정이 생성되었습니다.")
        print("아이디: hmmaster")
        print("비밀번호: hmtaxi1234!")

if __name__ == '__main__':
    create_admin() 