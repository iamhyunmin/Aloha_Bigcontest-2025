# ✅ 1. 파이썬 환경 선택
FROM python:3.10

# ✅ 2. 작업 디렉토리 설정
WORKDIR /app

# ✅ 3. app 폴더 안에 있는 모든 파일 복사
COPY ./app /app

# ✅ 4. 패키지 설치
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# ✅ 5. FastAPI 서버 실행 (uvicorn)
EXPOSE 7860
CMD ["uvicorn", "mcp_server:app", "--host", "0.0.0.0", "--port", "7860"]
