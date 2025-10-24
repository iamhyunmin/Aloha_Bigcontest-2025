# mcp_server.py
from fastapi import FastAPI
from pydantic import BaseModel
from rag_gemini import generate_revue_answer
import uvicorn

app = FastAPI(title="ReVue MCP Server")

class QueryRequest(BaseModel):
    query: str

@app.post("/search")
def search(request: QueryRequest):
    try:
        answer = generate_revue_answer(request.query)
        return {"answer": answer}
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    # 로컬 테스트 시 실행
    uvicorn.run(app, host="0.0.0.0", port=8000)