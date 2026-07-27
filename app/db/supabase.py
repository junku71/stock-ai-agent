import base64
import json

from supabase import create_client, Client
from app.core.config import settings

# RLS(Row Level Security) ON 환경에서는 service_role 키만 쓰기가 통과된다.
# service_role 키가 설정돼 있으면 우선 사용하고, 없으면 기존 anon 키로 폴백.
url: str = settings.SUPABASE_URL
key: str = settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_KEY


def _key_role(k: str) -> str:
    """키에 담긴 role(anon/service_role)을 판별한다. JWT가 아니면 형태로 추정."""
    try:
        payload = k.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # base64 패딩 보정
        return json.loads(base64.urlsafe_b64decode(payload)).get("role", "알 수 없음")
    except Exception:
        if k.startswith("sb_secret_"):
            return "service_role (신형 secret 키)"
        if k.startswith("sb_publishable_"):
            return "anon (신형 publishable 키)"
        return "알 수 없음 (JWT 형식 아님 - 키를 다시 확인하세요)"


if not settings.SUPABASE_SERVICE_ROLE_KEY:
    print("⚠️  SUPABASE_SERVICE_ROLE_KEY 미설정 - anon 키 사용 중. RLS가 켜져 있으면 쓰기가 차단될 수 있습니다.")

# 어떤 키가 실제 적용됐는지 마스킹해서 출력 (에러 문의 시 스크린샷만으로 판별 가능)
print(f"Supabase 키 role: {_key_role(key)} (끝 4자리: ...{key[-4:] if key else '없음'})")

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