import json
import os

def load_accident_data():
    """저장된 사고 데이터를 불러옴"""
    filepath = 'data/accident_data.json'
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

def save_accident_data(data):
    filepath = 'data/accident_data.json'
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"데이터가 {filepath}에 저장되었습니다.")

if __name__ == "__main__":
    print("사고 데이터에 요약 정보를 추가하는 중...")
    data = load_accident_data()
    if data:
        save_accident_data(data)
        print("요약 정보가 성공적으로 추가되었습니다.")
        if 'summary' in data:
            print(f"총 사고 건수: {data['summary']['total_count']}건")
            print(f"가해사고: {data['summary']['at_fault_count']}건")
            print(f"피해사고: {data['summary']['not_at_fault_count']}건")
    else:
        print("데이터를 로드할 수 없습니다.") 