FROM python:3.13-slim

RUN pip install --no-cache-dir "numpy==2.3.3" "pandas==2.3.3" "tabulate==0.9.0" \
      "google-cloud-storage==3.4.0"

COPY backtest.py analyze.py /app/
WORKDIR /app
ENTRYPOINT ["python3", "backtest.py"]
