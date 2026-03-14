FROM python:3.12-slim

# 关闭 Python 输出缓冲，docker logs 才能看到 print 等输出
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ai.py index.html ./

EXPOSE 8765

CMD ["python", "app.py"]
