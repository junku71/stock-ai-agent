from supabase import create_client, Client
from app.core.config import settings

# RLS(Row Level Security) ON 환경에서는 service_role 키만 쓰기가 통과된다.
# service_role 키가 설정돼 있으면 우선 사용하고, 없으면 기존 anon 키로 폴백.
url: str = settings.SUPABASE_URL
key: str = settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_KEY

if not settings.SUPABASE_SERVICE_ROLE_KEY:
    print("⚠️  SUPABASE_SERVICE_ROLE_KEY 미설정 - anon 키 사용 중. RLS가 켜져 있으면 쓰기가 차단될 수 있습니다.")

supabase: Client = create_client(url, key)

def get_data(table_name):
    """Supabase에서 데이터 가져오기"""
    try:
        response = supabase.table(table_name).select("*").execute()
        print(f"{table_name}에서 데이터를 성공적으로 가져왔습니다!")
        return response.data
    except Exception as e:
        print(f"데이터 가져오기 오류: {e}")
        return None