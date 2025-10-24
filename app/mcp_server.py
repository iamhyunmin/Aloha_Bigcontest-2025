from fastapi import FastAPI
from pydantic import BaseModel
from rag_gemini import generate_revue_answer
import uvicorn
import os

app = FastAPI(title="ReVue MCP Server")

class QueryRequest(BaseModel):
    query: str

@app.post("/search")
def search(request: QueryRequest):
    try:
        answer = generate_revue_answer(request.query)
        return {"answer": answer}
    except Exception as e:
        # Hugging Face 로그에서 확인하기 쉽게 에러 로그 출력
        print(f"❌ Error: {e}")
        return {"error": str(e)}

# ✅ Hugging Face에서는 PORT 환경변수를 읽어서 실행해야 함
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
