from pathlib import Path
import os
import faiss
import pandas as pd
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
import google.generativeai as genai
import numpy as np 

# -------------------------------
# 경로 설정
# -------------------------------
# ✅ 안전한 캐시 경로로 변경
CACHE_DIR = Path("/tmp/huggingface_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
os.environ["HF_HOME"] = str(CACHE_DIR)

ARTIFACTS_DIR = os.getcwd()
OUT_DIR = "."       # ✅ 현재 디렉토리 기준으로 변경
DATA_DIR = "."

EMB_MODEL = "BAAI/bge-m3"
TOP_K = 12

# 🔑 Gemini API 키
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("⚠️ WARNING: GEMINI_API_KEY not found in .env file. API calls may fail.")

llm = None

try:
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    llm = genai.GenerativeModel("gemini-2.5-flash")  # or "gemini-2.0-flash"
    print("✅ Gemini model loaded successfully.")
except Exception as e:
    print(f"⚠️ WARNING: Gemini initialization failed: {e}")

# -------------------------------
# 인덱스 및 메타 불러오기
# -------------------------------
index = faiss.read_index(os.path.join(OUT_DIR, "rag_faiss.index"))
meta = pd.read_csv(os.path.join(OUT_DIR, "meta.csv"))

# ✅ 모델 캐시 디렉터리 지정
model = SentenceTransformer(EMB_MODEL, device="cpu", cache_folder=str(CACHE_DIR))

# -------------------------------
# 추가 데이터 불러오기
# -------------------------------

# === 폐점 데이터 ===
if os.path.exists("versus_closed.csv"):
    summary_df = pd.read_csv("versus_closed.csv", encoding="utf-8-sig")
else:
    summary_df = pd.DataFrame()

def build_closure_hints(summary_df, max_lines=3):
    """폐점/영업중 평균 비교 데이터를 간결한 문자열 요약으로 변환 (LLM 참고용 힌트)"""
    hints = []
    for _, r in summary_df.iterrows():
        idx = r["Index"]
        c_mean, o_mean = r["Closed_mean"], r["Open_mean"]
        if pd.notna(c_mean) and pd.notna(o_mean):
            diff_ratio = abs(c_mean - o_mean) / max(o_mean, 1e-6)
            if diff_ratio > 0.05:
                direction = "폐점 ↑" if c_mean > o_mean else "영업중 ↑"
                hints.append(f"{idx}: {direction} ({c_mean:.1f}/{o_mean:.1f})")
        if len(hints) >= max_lines:
            break
    return "\n".join(hints)

# === 별점 데이터 ===
if os.path.exists("store_google_rating.csv"):
    ratings = pd.read_csv("store_google_rating.csv", encoding="utf-8-sig")
    ratings = ratings[["ENCODED_MCT", "g_rating", "g_user_ratings_total"]]
    ratings["ENCODED_MCT"] = ratings["ENCODED_MCT"].astype(str)
    rating_map = dict(zip(ratings["ENCODED_MCT"], zip(ratings["g_rating"], ratings["g_user_ratings_total"])))
else:
    rating_map = {}

def build_rating_summary(mct_list, max_lines=None):
    """구글맵 별점 데이터 요약"""
    seen, lines = set(), []
    for m in mct_list:
        key = str(m)
        if key in seen:
            continue
        seen.add(key)

        r_pair = rating_map.get(key)
        if r_pair is None or any(pd.isna(v) for v in r_pair):
            lines.append(f"{key}: 별점 정보 없음")
        else:
            try:
                rating, total = float(r_pair[0]), int(r_pair[1])
                lines.append(f"{key}: ⭐ {rating:.1f}/5 ({total}명 평가)")
            except:
                lines.append(f"{key}: 별점 정보 없음")

        if max_lines and len(lines) >= max_lines:
            break
    return "\n".join(lines) if lines else "별점 요약 데이터 없음"


# SentenceTransformer 모델 로드
try:
    print(f"Loading embedding model: {EMB_MODEL} to cache folder: {CACHE_DIR}")
    # cache_folder 인자를 사용하지만, 환경 변수(HF_HOME)가 우선합니다.
    print("Embedding model loaded successfully.")
    EMB_DIM = model.get_sentence_embedding_dimension()
except Exception as e:
    print(f"Error loading SentenceTransformer model: {e}")
    raise RuntimeError(f"Failed to load SentenceTransformer: {e}")

# 임베딩 차원 확인
EMB_DIM = model.get_sentence_embedding_dimension()

# -------------------------------
# 검색 함수
# -------------------------------
def retrieve_context(query, top_k=TOP_K):
    q_emb = model.encode([query], normalize_embeddings=True)
    D, I = index.search(np.array(q_emb, dtype="float32"), top_k)
    ctx = meta.iloc[I[0]].copy()
    ctx["score"] = D[0]
    return ctx

# -------------------------------
# LLM 프롬프트 템플릿
# -------------------------------
SYSTEM_PROMPT = """
당신은 데이터 기반 마케팅 내비게이터 ‘ReVue’입니다.
주어진 매장 데이터를 바탕으로, 현재 상태를 진단하고 가장 적절한 전략 경로를 제시하세요.
질문이 특정 지표(예: 재방문율, 신규고객 등)를 명시하면, 그 지표를 핵심 목표로 삼으세요.
질문에 반드시 답변해야 합니다.
⚠️ 모든 수치는 실제 데이터에서 추출된 값이며, LLM은 이를 수정·보정·추정하지 않습니다. 반드시 그대로 인용하세요.
💡 사고 과정 안내:
think step by step — 각 단계에서 **제공된 데이터만** 근거로 논리적으로 판단하고,  
추가 계산이나 추정은 하지 않습니다. 지표 간 비교 시에도 **주어진 값 그대로** 사용하세요.
===== 📊 주요 지표 해석 규칙 =====
지표별로 “좋은 방향”이 다릅니다. 아래 기준을 반드시 따라 해석하세요.
1. 해석 시 유의: 구간 환산 및 표현 규칙:
1) 순위형 지표 (매출금액, 거래건수, 평균 객단가, 순고객수, 매출 순위 비율)
- 값이 50% 이상인 경우, "100% - 값"으로 환산 후 ‘하위 ○%’로 표현합니다. (예: 80 → 하위 20%)
- 값이 50% 미만인 경우, 그대로 ‘상위 ○%’로 표현합니다. (예: 10 → 상위 10%)
- 낮은 수치일수록 우수한 성과(상위권), 높은 수치일수록 개선 필요(하위권)임을 명확히 인식하세요.
2) 역방향 지표 (결제취소율)
- 방향을 반대로 해석합니다.
  50% 이상 → “100% - 값” 후 ‘상위 ○%’
  50% 미만 → ‘하위 ○%’
- 낮은 수치일수록 좋은 상태(즉, 낮은 취소율은 우수함)를 의미합니다.
3) 중요한 점  
- “상위/하위 ○%”는 단순히 **순위 표현**일 뿐, 실제 수치의 크고 작음을 의미하지 않습니다.  
  예를 들어 ‘매출 하위 20%’는 ‘하위권(낮은 성과)’을, ‘결제취소율 하위 20%’는 ‘상위권(우수한 성과)’을 뜻합니다.  
※ 환산 규칙은 '순위형 지표'(매출금액, 거래건수, 평균 객단가, 순고객수, 매출 순위 비율)에만 적용합니다.  
고객 비율형 지표(예: 재방문율, 신규율, 고객 유형 비중)는 **절대 환산하지 않습니다.**
2. 신규/재방문 고객 해석 규칙:
- 신규 고객 비율이 낮을수록 신규 유입이 부족하다는 의미이며,
  재방문 고객 비율이 높을수록 충성 고객이 많은 상태입니다.
- 재방문율이 낮은 매장에 대한 질문이 주어졌을 때는,
  신규 고객 비율이 낮더라도 반드시 재방문율을 높이는 방안을 제시해야 합니다.
  (즉, 답변을 회피하거나 "데이터 부족"으로 끝내지 않습니다.)
- 신규율과 재방문율이 동시에 낮은 경우(각각 30% 미만)는  
  두 지표를 별도로 비교하지 말고, '고객 기반이 약한 상태'로만 요약하세요.  
  정량 비교나 수치 언급은 생략합니다.
3. 모든 수치는 집계 기준에 따라 합이 100%가 아닐 수 있습니다.
   모델은 보정하지 말고, 주어진 수치를 그대로 사용해 해석합니다. 
4. 각 매장마다 주어진 데이터가 다르므로,  
**데이터의 다양성을 만들어내지 말고**, 입력된 데이터 범위 내에서만  
해석과 전략을 생성하세요.
===== 🚗 답변 작성 가이드 =====
질의에 대한 답변은 반드시 다음 형식을 따라야 합니다:
답변은 명확·간결·전략적으로 표현하고, 실행안을 나열식으로 쓰지 마세요.
===== 📍 현재 위치 파악 =====
- 🚦 신호등 등급(🔴, 🟡, 🟢)만을 사용하여 전반적인 매장 상태를 직관적으로 표시하세요.
- 잘 가고 있는 구간에는 강점
- 느리게 가고 있는 구간에는 약점을 출력하세요.
- 상위 2개, 하위 2개만 설명하세요.
- "낮을수록 상위" 또는 이와 유사한 표현은 절대 출력하지 마세요.
- 모든 퍼센트 수치는 위 규칙에 따라 자연스럽게 상·하위로 기술하세요.
===== 🧭 경로 탐색 =====
아래 3가지 중 각 항목의 의미를 명확히 구분해 짧은 근거(1~2줄)와 함께 제시하세요:
1. 강화 경로 (Enhance Line): 현재 강점을 강화하는 경로
•  예: 20대 고객 비율 높음 -> 20대 대상 마케팅 강화
•  예: 재방문 고객 비중 높음 -> 단골 고객 관리 프로그램 지속 운영
2. 보수 경로 (Fix Line): 개선이 필요한 약점을 보완하는 경로
•  예: 재방문율 낮음 -> 재방문 고객 대상 프로모션 강화
•  예: 순 고객 수 낮음  -> 순 고객 수 증대 위한 이벤트 도입
3. 전환 경로 (Shift Line): 강점과 약점을 재해석해 새로운 매출 루트를 만드는 경로
•  예: 주거 고객 비율 높음(장점) + 신규 고객 비율 낮음(약점) -> 주거 고객 대상 구독제 도입 (신규 고객 유입 없이도 안정적 매출 확보)
•  예: 순 고객 수 낮음(약점) + 객단가 높음(장점) -> 객단가 상승 전략 제시 (순 고객 수가 낮아도 객단가 상승으로 매출 보완)
각 경로에는 반드시 짧고 구체적인 근거를 붙이세요.  
예: “고객 재방문율이 높아 충성 고객 관리 강화 필요” 등
===== 🏁 최종 추천 경로 =====
- 단, **여러 아이디어를 나열하지 말고**,  
  현재 상황에서 **가장 효과가 클 것으로 예상되는 하나의 핵심 전략에만 집중**하세요. 
- 3개 경로 중 **가장 핵심적인 하나**만 선택하세요.  
- 아래 항목을 반드시 포함합니다:
  • 전략명 (전략을 잘 설명하는 간결한 이름)
  • 핵심 아이디어 (2문장 이내)  
  • 채택 근거 (핵심 지표 기반, 구체적 수치 언급 가능)
===== 🧩 운행 안내 =====
- “실행 방법”에는 실제로 사장님이 바로 적용할 수 있는 행동을 구체적으로 제시하세요. 각 항목은 반드시 행동 중심으로 작성하고, 맥락에 맞는 이모지를 포함하세요.
- “기대효과”에는 실행으로 예상되는 변화(지표, 고객 반응, 매출 등)를 수치나 체감 효과 중심으로 작성하세요.
- 예시는 아래와 같은 형식으로 작성하세요.
<실행 방법 예시>
1. **‘시그니처 세트’ 출시:** 인기 메뉴에 🍰 디저트를 결합한 세트를 출시하고 매장 내 🖼️ 포스터를 비치합니다.  
2. **‘출근길 구독제’ 운영:** 👔 직장인 고객 대상으로 월 구독제를 도입해 🔁 반복 방문을 유도합니다.  
3. **‘리뷰 이벤트’ 진행:** 재방문 고객에게 리뷰 작성 시 🍹 음료 쿠폰을 제공합니다.  
<기대효과 예시>
- 평균 객단가가 약 10~15% 상승하고, 재방문율이 3%p 향상됩니다.  
- 배달 주문 건수가 증가하며 신규 고객 유입이 활성화됩니다.
===== 🏆 도착 알림 =====
- 첫번째 문장에는 위에서 생성한 '전략명'을 그대로 사용하여 노선으로 출력하라.
- 마지막 문장에는 오늘 사장님이 데이터 기반 성장 여정을 통해 어떤 캐릭터로 ‘레벨업’했는지를 표현하세요.
- 표현 형식은 반드시 다음과 같습니다:
  🎉오늘 사장님은  “단골 마스터"로 성장했습니다!
- "..." 안에는 배지나 칭호처럼 짧고 인상적인 이름을 넣으세요.
- 배지는 마케팅 성과 유형에 따라 RPG 캐릭터나 칭호처럼 창의적으로 표현할 수 있습니다.
  예시)
  • 높은 매출 성장 → "피크장인"
  • 고객 리텐션 강화 → "단골 마스터"
  • 신규 고객 확보/시장 전환 → "시프트 마스터"
  • 브랜드 가치·경험 중심 → "가치창조러", "브랜드 아티스트"
- 배지명은 3~6글자 이내로 짧고 강렬하게 만드세요.
- ‘~형’, ‘~전사’, ‘~러’, ‘~마스터’, ‘~크리에이터’ 등의 어미를 활용해 캐릭터형 표현을 지향합니다.
출력 템플릿:
---------------------------------------
ReVue — 데이터를 길로 바꾸는 마케팅 네비게이션
===== 📍 현재 위치 파악 =====
🚦신호등: 
🚗 잘 가고 있는 구간
- {강점 리스트}
⚠️ 느리게 가고 있는 구간
- {약점 리스트}
🎯한줄요약:
{핵심 진단 요약문}
===== 🧭 경로 탐색 =====
- 유지 경로 (Keep Line): ...
- 보수 경로 (Fix Line): ...
- 전환 경로 (Shift Line): ...
===== 🏁최종 경로 =====
추천 경로: ...
전략명: ...
핵심 아이디어: ...
채택 근거: ...
===== 🧩 운행 안내 =====
<실행 방법>
...
<기대효과>
...
===== 🏆 도착 알림 =====
🚌 ‘{전략명}’ 노선에 진입하셨네요.  
🎉오늘 사장님은 “...”(으)로 성장했습니다!
---------------------------------------
"""

# ----------------------------
# 🧠 질의 수행 함수
# ----------------------------
def generate_revue_answer(user_query, mct_list=None):
    """
    RAG + 폐점 힌트 + 별점 데이터를 함께 반영한 질의 응답
    - 주소 자동 감지 및 필터링 강화 (공백, 띄어쓰기 불일치 포함)
    - 잘못된 fallback 제거 (불필요한 구 단위 재검색 X)
    """

    # 1️⃣ RAG 검색
    ctx_df = retrieve_context(user_query, top_k=TOP_K)
    ctx_df_all = ctx_df.copy()  # 원본 백업 (필터 실패 시 전체 유지용)

    # (validation) RAG 검색 결과 확인
    print("=== 🔍 RAG 검색 결과 미리보기 ===")
    print(ctx_df[["TA_YM", "rag_text"]].head())
    print("================================\n")

    # ✅ 기본 문맥 구성
    context_text = "\n\n".join(ctx_df["rag_text"].head(10))

    # 1.5️⃣ 주소 자동 감지 및 필터링
    # 숫자 없어도 감지 가능 (ex. '왕십리로', '왕십리길')
    addr_pattern = r"((서울(?:특별시)?\s*)?(성동구\s*)?[가-힣A-Za-z0-9]+(\s*\d+|\s*(로|길|대로|대|가|나|다|라|마|바|사|아|자|차|카|타|파|하))\s*\d*)"
    addr_match = re.search(addr_pattern, user_query)  # ✅ 질의문에서 주소 감지

    if addr_match:
        # 감지된 주소 정규화: 모든 공백 제거
        addr_filter = re.sub(r"\s+", "", addr_match.group(0).strip())

        # rag_text 내부에서 [ADDR= ...] 부분 추출 및 정규화
        ctx_df["ADDR_EXTRACT"] = (
            ctx_df["rag_text"]
            .str.extract(r"\[ADDR=([^\]]+)\]")[0]
            .astype(str)
            .str.strip()
            .str.replace(r"\s+", "", regex=True)
        )

        # 공백 제거 후 정확 매칭 (regex=False로 안전하게)
        ctx_df = ctx_df[
            ctx_df["ADDR_EXTRACT"].str.contains(addr_filter, regex=False, na=False)
        ]

        if len(ctx_df) > 0:
            print(f"📍 주소 기반 필터링 적용: '{addr_filter}' 포함 매장만 사용 ({len(ctx_df)}건)")
        else:
            print(f"⚠️ '{addr_filter}' 감지되었지만 일치하는 매장 데이터가 없습니다. 전체 RAG 결과 유지.")
            ctx_df = ctx_df_all.copy()
    else:
        print("⚠️ 주소 패턴이 질의에서 감지되지 않음 — 전체 RAG 결과 사용")

    # 🔧 필터링 이후 문맥 재구성
    context_text = "\n\n".join(ctx_df["rag_text"].head(10))

    # 3️⃣ 폐점 힌트
    closure_hints = (
        build_closure_hints(summary_df, max_lines=3)
        if not summary_df.empty
        else "폐점 데이터 없음"
    )

    # 4️⃣ 별점 요약
    rating_summary = (
        build_rating_summary(mct_list or [], max_lines=5)
        if rating_map
        else "별점 요약 데이터 없음"
    )

    # 5️⃣ 통합 프롬프트 구성
    full_prompt = f"""
{SYSTEM_PROMPT}
[시스템 참고 힌트 - 폐점 데이터]
{closure_hints}
[근거 데이터 - 구글맵 별점]
{rating_summary}
[참고 데이터 문맥]
{context_text}
[사용자 질의]
{user_query}
"""

    # 6️⃣ LLM 호출 + 디버그 출력
    response = llm.generate_content(full_prompt)
    #print("=== CONTEXT TEXT 미리보기 ===")
    #print(context_text[:3000]) 
    #print(rating_summary[:500])  # 500자까지만 미리보기
    #print("==================================")
    print(response.text)

    return response.text

# -------------------------------
# 실행 예시
# -------------------------------
if __name__ == "__main__":
    print("안녕하세요! ReVue — 데이터를 길로 바꾸는 마케팅 네비게이터입니다. 🚘")
    while True:
        q = input("\n🔎 질문을 입력하세요 (종료하려면 exit 입력): ").strip()
        if q.lower() in ["exit", "quit", "종료"]:
            print("👋 이용해주셔서 감사합니다! ReVue 종료합니다.")
            break
        ans = generate_revue_answer(q)
        print("\n" + "="*80 + "\n")
