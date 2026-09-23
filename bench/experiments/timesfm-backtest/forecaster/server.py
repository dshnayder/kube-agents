"""TimesFM 2.5 behind one HTTP endpoint, CPU only.

POST /forecast {"inputs": [[...], ...], "horizon": 288}
  -> {"point": [[...]], "quantiles": [[[mean, q10..q90], ...]], "seconds": 1.2, "bucket": 2048}

The model pads every call to its compiled context length and batch size, so one compiled copy
per context bucket keeps a 1-day context from paying for a 28-day one. The copies share
nothing but the checkpoint file; each is about 1 GB resident.
"""

import os
import threading
import time

import numpy as np
import timesfm
import torch
from fastapi import FastAPI
from pydantic import BaseModel

CHECKPOINT = os.environ.get("TIMESFM_CHECKPOINT", "google/timesfm-2.5-200m-pytorch")
CONTEXT_BUCKETS = (512, 2048, 8192)
MAX_HORIZON = 512
BATCH_SIZE = 64

app = FastAPI()
models = {}
lock = threading.Lock()


class Request(BaseModel):
    inputs: list[list[float]]
    horizon: int


@app.on_event("startup")
def load():
    torch.set_num_threads(os.cpu_count())
    for bucket in CONTEXT_BUCKETS:
        m = timesfm.TimesFM_2p5_200M_torch.from_pretrained(CHECKPOINT)
        m.compile(timesfm.ForecastConfig(
            max_context=bucket, max_horizon=MAX_HORIZON, per_core_batch_size=BATCH_SIZE,
            normalize_inputs=True, use_continuous_quantile_head=True,
            force_flip_invariance=True, infer_is_positive=True, fix_quantile_crossing=True))
        models[bucket] = m


@app.get("/healthz")
def healthz():
    return {"ready": len(models) == len(CONTEXT_BUCKETS)}


@app.post("/forecast")
def forecast(req: Request):
    longest = max(len(x) for x in req.inputs)
    bucket = next(b for b in CONTEXT_BUCKETS if b >= longest) if longest <= CONTEXT_BUCKETS[-1] \
        else CONTEXT_BUCKETS[-1]
    inputs = [np.asarray(x, dtype=np.float32) for x in req.inputs]
    # One forecast at a time: torch already uses every core, and two interleaved calls would
    # make the reported seconds meaningless.
    with lock:
        t = time.time()
        point, quant = models[bucket].forecast(horizon=req.horizon, inputs=inputs)
        seconds = time.time() - t
    return {"point": point.tolist(), "quantiles": quant.tolist(), "seconds": seconds,
            "bucket": bucket}
