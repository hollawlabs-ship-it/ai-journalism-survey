# 자문의견 요청서 수집 서버
# 빌드: docker build -t advisory-form .
# 실행: docker run -p 8768:8768 -v advisory_data:/data -e ADVISORY_ADMIN_PASSWORD=<비밀번호> advisory-form
# DATABASE_URL 을 주면 /data 대신 PostgreSQL 에 저장한다 (Render 무료 플랜).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY template.docx .

# 응답 DB 와 키 파일은 /data 볼륨에 둔다.
ENV ADVISORY_DATA=/data \
    PYTHONUTF8=1 \
    PORT=8768
RUN mkdir -p /data
EXPOSE 8768

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
