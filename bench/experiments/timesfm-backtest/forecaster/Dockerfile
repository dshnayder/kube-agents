FROM python:3.13-slim

RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
      "timesfm[torch]==3.0.2" "fastapi==0.115.0" "uvicorn==0.30.6" \
 && python -c "import timesfm; timesfm.TimesFM_2p5_200M_torch.from_pretrained('google/timesfm-2.5-200m-pytorch')"

COPY server.py /app/server.py
WORKDIR /app
ENV HF_HUB_OFFLINE=1
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
