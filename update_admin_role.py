from app import app, db
from models import User

def update_admin_role():
    with app.app_context():
        # hmmaster 사용자 찾기
        user = User.query.filter_by(username='hmmaster').first()
        
        if user:
            print(f"현재 사용자 정보:")
            print(f"  - ID: {user.id}")
            print(f"  - Username: {user.username}")
            print(f"  - Email: {user.email}")
            print(f"  - Name: {user.name}")
            print(f"  - Role: {user.role}")
            print(f"  - Position: {user.position}")
            
            # role이 admin이 아니면 업데이트
            if user.role != 'admin':
                print(f"\n⚠️  Role이 'admin'이 아닙니다. 'admin'으로 업데이트합니다...")
                user.role = 'admin'
                db.session.commit()
                print("✅ Role이 'admin'으로 업데이트되었습니다.")
            else:
                print(f"\n✅ Role이 이미 'admin'으로 설정되어 있습니다.")
        else:
            print("❌ 'hmmaster' 사용자를 찾을 수 없습니다.")
            print("관리자 계정을 생성합니다...")
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
            print("✅ 관리자 계정이 생성되었습니다.")

if __name__ == '__main__':
    update_admin_role()

