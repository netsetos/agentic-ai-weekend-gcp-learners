#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make tune: launch, or poll, the managed supervised-tuning job on the frozen dataset. Module 10 (10.1).

    make tune PROJECT=... [TUNE_BASE=gemini-3.1-flash-lite] [TUNE_EPOCHS=3] [TUNE_ADAPTER=4]
    python evals/tune.py --project P --dataset gs://P-datasets/sft/documind_sft_v1.vertex.jsonl
    python evals/tune.py --project P --poll projects/P/locations/us-central1/tuningJobs/123

An explicit act, like deploy-slm: it bills per training token and nothing else on the lane triggers it.
Tuning is REGIONAL (us-central1) and so is the endpoint it produces; the API serves an endpoint through
a regional client when GENERATOR_MODEL names one (services/rag-api/generator.py, _client_for). The
base-model check is 10.1's rule: a base managed SFT does not accept fails at submission, after the
dataset is written, uploaded and paid for; failing here costs nothing and says why. TUNABLE is dated -
re-verify it on the tuning page before the room.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

TUNABLE = {"gemini-3.5-flash", "gemini-3.1-flash-lite"}     # managed SFT, as of 2026-09-04 (10.1); 3.6-flash is not tunable
TUNABLE_DATE = "2026-09-04"
REGION = "us-central1"
# The LoRA rank is an ENUM in the SDK, not a number: adapter_size=4 fails pydantic's validation at submission
# ("Input should be 'ADAPTER_SIZE_ONE', ..." - F37, the first live make tune on 10 September). The rank stays a
# number on the command line and in 10.1, and is spelled the SDK's way here.
ADAPTER_SIZE = {1: "ADAPTER_SIZE_ONE", 2: "ADAPTER_SIZE_TWO", 4: "ADAPTER_SIZE_FOUR", 8: "ADAPTER_SIZE_EIGHT",
                16: "ADAPTER_SIZE_SIXTEEN", 32: "ADAPTER_SIZE_THIRTY_TWO"}


def config_for(base: str, epochs: int, adapter: int, display_name: str, validation: str = "") -> dict:
    """The tuning config as a dict, checked before anything is submitted: the base is tunable, the rank is one
    the SDK spells. Pure, so --selftest proves both refusals without the SDK or a project."""
    if base not in TUNABLE:
        raise SystemExit(f"{base} is not tunable: managed SFT accepts {sorted(TUNABLE)} as of {TUNABLE_DATE}. "
                         f"gemini-3.6-flash is the course default for inference and is NOT tunable - re-check the "
                         f"tuning page, then update TUNABLE here and in 10.1.")
    if adapter not in ADAPTER_SIZE:
        raise SystemExit(f"adapter {adapter}: the LoRA rank must be one of {sorted(ADAPTER_SIZE)}")
    cfg = {"epoch_count": epochs, "adapter_size": ADAPTER_SIZE[adapter], "tuned_model_display_name": display_name}
    if validation:
        cfg["validation_dataset"] = validation
    return cfg


def launch(project: str, dataset: str, base: str, epochs: int, adapter: int, display_name: str, validation: str = ""):
    cfg = config_for(base, epochs, adapter, display_name, validation)
    from google import genai
    from google.genai import types
    client = genai.Client(enterprise=True, project=project, location=REGION)      # tuning is regional
    if validation:
        cfg["validation_dataset"] = types.TuningValidationDataset(gcs_uri=validation)
    return client.tunings.tune(base_model=base, training_dataset=types.TuningDataset(gcs_uri=dataset),
                               config=types.CreateTuningJobConfig(**cfg))


def selftest() -> int:
    """Offline: the two refusals fire before any spend, and a good config spells the rank the SDK's way."""
    for base, adapter in (("gemini-3.6-flash", 4), ("gemini-3.1-flash-lite", 3)):
        try:
            config_for(base, 3, adapter, "x")
            raise AssertionError(f"{base} / adapter {adapter} was accepted")
        except SystemExit:
            pass
    cfg = config_for("gemini-3.1-flash-lite", 3, 4, "documind-sft-v1")
    assert cfg == {"epoch_count": 3, "adapter_size": "ADAPTER_SIZE_FOUR", "tuned_model_display_name": "documind-sft-v1"}, cfg
    try:
        from google.genai import types
        types.CreateTuningJobConfig(**cfg)                 # the SDK's own validation, where the SDK is installed
        sdk = "and the SDK accepts it"
    except ImportError:
        sdk = "(the SDK's own validation runs where it is installed)"
    print(f"selftest: an untunable base and a rank the SDK cannot spell are refused before submission; "
          f"adapter 4 is ADAPTER_SIZE_FOUR {sdk}")
    return 0


def poll(project: str, name: str, every_s: int = 60):
    from google import genai
    client = genai.Client(enterprise=True, project=project, location=REGION)
    done = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED"}
    job = client.tunings.get(name=name)
    while str(job.state).rsplit(".", 1)[-1] not in done:
        print(f"  {time.strftime('%H:%M:%S')} {job.state}")
        time.sleep(every_s)
        job = client.tunings.get(name=name)
    return job


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.environ.get("DOCUMIND_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    ap.add_argument("--dataset", default="", help="gs://PROJECT-datasets/sft/documind_sft_v1.vertex.jsonl")
    ap.add_argument("--validation", default="")
    ap.add_argument("--base", default="gemini-3.1-flash-lite")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--adapter", type=int, default=4, help="the LoRA rank; in the config, not a comment (10.1)")
    ap.add_argument("--display-name", default="documind-sft-v1")
    ap.add_argument("--poll", default="", help="a tuning job name to wait for")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.project:
        print("--project is required - this is a live, billed tool", file=sys.stderr)
        return 2
    if a.poll:
        job = poll(a.project, a.poll)
    else:
        if not a.dataset:
            print("--dataset gs://... is required (make trainset uploads it)", file=sys.stderr)
            return 2
        job = launch(a.project, a.dataset, a.base, a.epochs, a.adapter, a.display_name, a.validation)
        print(f"  submitted {job.name} on {a.base}: {a.epochs} epochs, adapter {a.adapter}, dataset {a.dataset}")
        if a.no_wait:
            print(f"  poll later: python evals/tune.py --project {a.project} --poll {job.name}")
            return 0
        job = poll(a.project, job.name)
    state = str(job.state).rsplit(".", 1)[-1]
    print(f"  {state}")
    if state == "JOB_STATE_SUCCEEDED" and job.tuned_model:
        ep = job.tuned_model.endpoint
        print(f"  tuned model : {job.tuned_model.model}\n  endpoint    : {ep}")
        print(f"\n  serve it as a candidate revision, no traffic, and judge it:\n"
              f"    make candidate PROJECT={a.project} GENERATOR_MODEL={ep} RAG_MODEL_BASE={a.base}\n"
              f"    make eval-live PROJECT={a.project} API=<the candidate url>\n"
              f"    make judge PROJECT={a.project} API_B=<the candidate url>")
        return 0
    print(f"  error: {getattr(job, 'error', None)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
