"""Synthetic demo tenant so the dashboard works without connecting a pipeline.

A document-intelligence pipeline serving a handful of customers (the
segments), with some everyday gaps (one stage's calls aren't priced, a few
placeholder stages report success without doing anything) and four staged
incidents so alerting has something to catch:

- 6 to 4 days ago: field-extraction failures spike, then recover (alert resolves)
- last 3 days: classification calls get slow (alert stays open)
- last 4 days: a new customer starts sending a large share of traffic (drift)
- last 5 days: Globex Logistics documents stop reaching the downstream system
- last 4 days: Globex field extraction escalates to a pricier fallback model,
  so its cost per document jumps
- last 3 days: a bad release of the validation step corrupts correct totals
- last 2 days: a config change lets some documents skip redaction, and a
  cleanup job runs delete_source on a few documents mid-pipeline; both break
  path contracts. Field extraction retries after a failure, which the
  contracts allow (up to 3 runs), so that change doesn't alert.

Every step records what it produced, and a share of wrong outputs are reported
(as a reviewer or customer would), so error analysis can trace each one to the
step it started at: OCR losing totals on long documents, a misclassification
that breaks extraction, misread dates, and errors that happen after the
pipeline.

Cost comes from per-page model prices, review and rework minutes for about a
fifth of documents (more for long contracts and claims), and an example rate
card for people time and platform overhead.

All of it is generated. Nothing here is real pipeline data.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from typing import List

from sqlalchemy import delete, select

from sqlalchemy.engine import Engine

from assay import store
from assay.models import Window
from assay.runner import run_measures
from assay.sources.events import EventsSource

TENANT = "demo"
SOURCE = f"events:{TENANT}"
SEGMENTS = ["Northwind Bank", "Contoso Insurance", "Globex Logistics", "Initech Health", None]
TYPES = ["invoice", "bank_statement", "contract", "id_document", "insurance_claim", None]
STAGES = ["document_splitting", "text_extraction", "classification", "field_extraction"]
MODELS = {"document_splitting": "gemini-3-flash-preview", "text_extraction": "gemini-3-flash-preview",
          "classification": "claude-haiku-4-5", "field_extraction": "claude-sonnet-5"}
LATENCY_MS = {"document_splitting": 4000, "text_extraction": 6000, "classification": 1500,
              "field_extraction": 9000}
PIPELINE = ["file_prep", "pre_processing", "text_extraction", "classification", "field_extraction",
            "validation", "highlighting", "redaction"]
STUBS = {"highlighting", "redaction"}  # placeholder stages: report success, do nothing
FALLBACK = {"document_splitting": "gemini-3.5-flash-lite", "field_extraction": "claude-opus-5-5"}
PRICE_PER_PAGE = {"gemini-3-flash-preview": 0.0006, "gemini-3.5-flash-lite": 0.0003,
                  "claude-haiku-4-5": 0.0009, "claude-sonnet-5": 0.004, "claude-opus-5-5": 0.02}
PAGES = {"invoice": (1, 3), "bank_statement": (3, 12), "contract": (8, 40), "id_document": (1, 2),
         "insurance_claim": (4, 20), None: (1, 10)}
TOUCH = {"invoice": 0.10, "bank_statement": 0.20, "contract": 0.45, "id_document": 0.08,
         "insurance_claim": 0.35, None: 0.25}
VENDORS = ["Acme Supply Co", "Blue Harbor Freight", "Crestline Medical", "Delta Office Partners", "Evergreen Legal LLP"]
RELEASE_BUG_DAYS = 3  # validation v2.4 shipped 3 days ago and mangles amounts
PATH_BUG_DAYS = 2  # redaction skipped / delete_source run by a misconfigured job

# Rules about the steps a document may take (assay/contracts.py).
EXAMPLE_CONTRACTS = [
    dict(kind="must_include", step="redaction", severity="critical", note="Nothing leaves unredacted"),
    dict(kind="never", step="delete_source", severity="critical", note="Source files are only deleted by retention"),
    dict(kind="before", step="classification", other="field_extraction", severity="warning"),
    dict(kind="only_after", step="human_review", other="validation", severity="warning",
         note="Reviewers see validated values"),
    dict(kind="max_runs", step="field_extraction", max_runs=3, severity="warning", note="Retries are fine, loops aren't"),
]

# Prompt history per AI step: (version, released N days ago, template, what changed).
PROMPTS = {
    "document_splitting": ("split_documents", [("v2", None, "Split the file into separate documents. Return page ranges.", None)]),
    "text_extraction": ("ocr_transcribe", [("v4", None, "Transcribe every page exactly, preserving layout.", None)]),
    "classification": ("classify_document", [
        ("v7", None, "Classify the document as one of: invoice, bank_statement, contract, id_document, "
                     "insurance_claim.\nAnswer with the label only.", None),
        ("v8", 14, "Classify the document as one of: invoice, bank_statement, contract, id_document, "
                   "insurance_claim.\nLook at the title and the first table before deciding.\n"
                   "Answer with the label only.", "Look at the title and first table; fewer contract/claim mix-ups")]),
    "field_extraction": ("extract_fields", [
        ("v12", None, "Extract reference, vendor, total and date.\nDates: return ISO 8601 (YYYY-MM-DD).", None),
        ("v13", 6, "Extract reference, vendor, total and date.\nDates: return ISO 8601 (YYYY-MM-DD).\n"
                   "Accept European day-first dates (DD/MM/YYYY).", "Accept European day-first dates")]),
}


def prompt_for(stage, age):
    """(prompt_id, version) a stage ran for a document received `age` days ago."""
    if stage not in PROMPTS:
        return None, None
    pid, versions = PROMPTS[stage]
    live = [v for v in versions if v[1] is None or age < v[1]]
    return pid, live[-1][0]


def _truth(rng, received, itype):
    total = round(rng.uniform(40, 25000), 2)
    return {"reference": f"{(itype or 'doc')[:3].upper()}-{rng.randint(10000, 99999)}",
            "vendor": rng.choice(VENDORS), "total": f"{total:,.2f}",
            "date": (received - timedelta(days=rng.randint(0, 20))).strftime("%Y-%m-%d")}


def _text(truth, itype, pages, garble_total=False):
    d = datetime.strptime(truth["date"], "%Y-%m-%d")
    total = "$?,??1.?0" if garble_total else f"${truth['total']}"
    return (f"{(itype or 'document').replace('_', ' ').upper()} {truth['reference']}   {truth['vendor']}   "
            f"Date: {d:%d %b %Y}   Bill to: Accounts Payable   Line items ...   Total due {total}   "
            f"Page 1 of {pages}   Remit to {truth['vendor']}, 100 Main Street")


def _step_outputs(rng, truth, itype, pages, age, completed):
    """What each step produced, with realistic faults injected. Returns
    (outputs by stage, [(field, expected, observed, kind)] wrong in the final output)."""
    long_doc = pages >= 10
    garble = long_doc and rng.random() < 0.04                      # OCR loses the total (upstream)
    # classify_document v8 (14 days ago) halves misclassification; extract_fields v13
    # (6 days ago) starts reading US dates day-first.
    misclass = itype is not None and rng.random() < (0.008 if age < 14 else 0.022)
    bad_date = rng.random() < (0.16 if age < 6 else 0.004)
    release_bug = age < RELEASE_BUG_DAYS and rng.random() < 0.10    # validation v2.4 regression
    after = rng.random() < 0.004                                    # right in the pipeline, wrong in delivery
    predicted_type = rng.choice([t for t in TYPES if t and t != itype]) if misclass else itype
    fields = dict(truth)
    if garble:
        fields["total"] = f"{float(truth['total'].replace(',', '')) * 0.887:,.2f}"  # grabbed the subtotal
    if misclass:
        fields["vendor"] = "Accounts Payable"  # wrong template: takes the bill-to line
    if bad_date:
        d = datetime.strptime(truth["date"], "%Y-%m-%d")
        if d.day <= 12 and d.day != d.month:
            fields["date"] = f"{d.year}-{d.day:02d}-{d.month:02d}"
        else:
            bad_date = False
    validated = {"total": fields["total"].replace(",", ""), "date": fields["date"]}
    if release_bug and not garble:
        validated["total"] = f"{float(fields['total'].replace(',', '')) / 1000:.2f}"
    outputs = {"text_extraction": {"_text": _text(truth, itype, pages, garble)},
               "classification": {"document_type": predicted_type},
               "field_extraction": fields,
               "validation": validated}
    wrong = []
    if completed:
        if misclass:
            wrong += [("document_type", itype, predicted_type, "wrong"), ("vendor", truth["vendor"], fields["vendor"], "wrong")]
        if garble:
            wrong.append(("total", truth["total"], validated["total"], "wrong"))
        elif release_bug:
            wrong.append(("total", truth["total"], validated["total"], "wrong"))
        if bad_date:
            wrong.append(("date", truth["date"], fields["date"], "wrong"))
        if after and not wrong:
            wrong.append(("reference", truth["reference"], truth["reference"][:-1] + "0", "wrong"))
    return outputs, wrong


EXAMPLE_RATES = {"review_per_hour": 36.0, "rework_per_hour": 36.0,
                 "platform_per_document": 0.004, "platform_per_page": 0.0008}

EXAMPLE_SLOS = [
    ("fallback_attribution", None, None, 0.95, "Example target: every call says which tier answered"),
    ("cost_coverage", None, None, 0.99, "Example target: spend is a total, not a floor"),
    ("handoff_loss", "segment", None, 0.05, "Example target: no segment loses more than 5%"),
    ("stage_failure_rate", None, None, 0.02, "Example target"),
    ("call_error_rate", None, None, 0.02, "Example target"),
    ("call_latency_p95", "stage", "field_extraction", 20000, "Example target"),
    ("time_to_complete_p90", "processing_mode", "realtime", 4 * 3600, "Example target: realtime p90 under 4 h"),
]


def seed(engine: Engine, days: int = 56, docs_per_day: int = 120, seed_value: int = 11,
         window_days: int = 3) -> dict:
    rng = random.Random(seed_value)
    now = datetime.utcnow().replace(microsecond=0)
    ago = lambda ts: (now - ts).total_seconds() / 86400  # age in days
    rollout = 14  # pretend tier attribution for splitting shipped two weeks ago

    with engine.begin() as conn:
        for t in (store.event_calls, store.event_documents, store.event_stage_runs, store.event_indexed,
                  store.event_reviews, store.event_errors):
            conn.execute(delete(t).where(t.c.tenant == TENANT))
        run_ids = [r[0] for r in conn.execute(select(store.measure_runs.c.id)
                                              .where(store.measure_runs.c.source == SOURCE))]
        if run_ids:
            conn.execute(delete(store.measure_results).where(store.measure_results.c.run_id.in_(run_ids)))
            conn.execute(delete(store.measure_runs).where(store.measure_runs.c.id.in_(run_ids)))
        conn.execute(delete(store.alerts).where(store.alerts.c.source == SOURCE))
        conn.execute(delete(store.slos).where(store.slos.c.source == SOURCE))
        conn.execute(delete(store.path_contracts).where(store.path_contracts.c.source == SOURCE))
        for c in EXAMPLE_CONTRACTS:  # one at a time: each kind has its own fields
            conn.execute(store.path_contracts.insert().values(source=SOURCE, updated_at=now, **c))
        conn.execute(delete(store.cost_rates).where(store.cost_rates.c.source == SOURCE))
        conn.execute(delete(store.prompt_versions).where(store.prompt_versions.c.tenant == TENANT))
        conn.execute(store.cost_rates.insert(), [dict(source=SOURCE, key=k, value=v, updated_at=now)
                                                 for k, v in EXAMPLE_RATES.items()])
        conn.execute(store.slos.insert(), [dict(source=SOURCE, measure_id=m, dimension=d, slice_value=v,
                                                target=t, note=n, updated_at=now)
                                           for m, d, v, t, n in EXAMPLE_SLOS])

    calls, docs, runs, indexed, reviews, errors = [], [], [], [], [], []
    for day in range(days):
        for k in range(docs_per_day):
            received = now - timedelta(days=days - day) + timedelta(minutes=rng.randint(0, 1439))
            age = ago(received)
            did = f"demo-{day:03d}-{k:03d}"
            segment = "Umbrella Legal" if age < 4 and rng.random() < 0.3 else rng.choice(SEGMENTS)
            itype = rng.choice(TYPES)
            mode = "batch" if rng.random() < 0.35 else "realtime"
            if mode == "realtime":
                minutes = rng.lognormvariate(3.75, 1.2)  # median ~43 min, long tail
                completed = received + timedelta(minutes=minutes) if rng.random() < 0.97 else None
            else:
                completed = received + timedelta(hours=rng.uniform(2, 30)) if rng.random() < 0.32 else None
            if completed and completed > now:
                completed = None
            lost_rate = 0.45 if (segment == "Globex Logistics" and age < 5) else 0.02
            fh = f"sha256:{rng.getrandbits(64):016x}"
            pages = rng.randint(*PAGES[itype])
            docs.append(dict(tenant=TENANT, document_id=did, received_at=received, completed_at=completed, page_count=pages,
                             status="completed" if completed else "processing", processing_mode=mode,
                             file_hash=fh, segment=segment, document_type=itype,
                             delivered_downstream=(rng.random() >= lost_rate) if completed else None))
            truth = _truth(rng, received, itype)
            step_out, wrong = _step_outputs(rng, truth, itype, pages, age, completed)
            for field_, expected, observed, kind in wrong:
                reported = (completed or received) + timedelta(hours=rng.uniform(2, 30))
                if rng.random() < 0.75 and reported < now:  # most wrong outputs get noticed
                    errors.append(dict(tenant=TENANT, error_id=f"{did}-{field_}", document_id=did, field=field_,
                                       expected=expected, observed=observed, kind=kind, reported_at=reported,
                                       reporter=f"reviewer-{rng.randint(1, 6)}",
                                       source=rng.choice(["review", "review", "qa", "customer"])))
            # Documents that need a person take a branch through human_review.
            touched = rng.random() < TOUCH[itype]
            path = PIPELINE[:6] + (["human_review"] if touched else []) + PIPELINE[6:]
            if age < PATH_BUG_DAYS and rng.random() < 0.04:
                path.remove("redaction")
            if age < PATH_BUG_DAYS and rng.random() < 0.015:
                path.insert(2, "delete_source")
            s = 0
            for stage in path:
                fail_p = 0.12 if (stage == "field_extraction" and 4 <= age < 6) else 0.004
                failed = stage not in STUBS and stage != "delete_source" and rng.random() < fail_p
                # A failed field extraction is retried once; other failed steps aren't.
                for attempt in ([True, False] if failed and stage == "field_extraction" else [failed]):
                    start = received + timedelta(seconds=30 * s)
                    runs.append(dict(tenant=TENANT, run_id=f"{did}-{s:02d}-{stage}", document_id=did, stage=stage,
                                     status="failed" if attempt else "success", started_at=start,
                                     finished_at=start + timedelta(seconds=0.1 if stage in STUBS else 20),
                                     did_work=stage not in STUBS, sequence=s,
                                     outputs=None if attempt and stage == "field_extraction" else step_out.get(stage),
                                     prompt_id=prompt_for(stage, age)[0], prompt_version=prompt_for(stage, age)[1]))
                    s += 1
            for stage in STAGES:
                ts = received + timedelta(seconds=rng.randint(10, 600))
                attributed = rng.random() < (0.97 if (stage == "document_splitting" and ago(ts) < rollout) else 0.6)
                served = MODELS[stage]
                escalate = 0.6 if (stage == "field_extraction" and segment == "Globex Logistics"
                                   and ago(ts) < 4) else 0.05 if stage == "document_splitting" else 0.03 \
                    if stage == "field_extraction" else 0.0
                if rng.random() < escalate:
                    served = FALLBACK[stage]
                    attributed = True if stage == "field_extraction" else attributed
                billed_pages = min(pages, 2) if stage == "classification" else pages
                price = round(PRICE_PER_PAGE[served] * billed_pages * rng.lognormvariate(0, 0.2), 5)
                slow = 3.5 if (stage == "classification" and ago(ts) < 3) else 1.0
                calls.append(dict(
                    tenant=TENANT, call_id=f"{did}-{stage}", stage=stage, ts=ts, document_id=did,
                    model_declared=MODELS[stage], model_served=served,
                    prompt_id=prompt_for(stage, age)[0], prompt_version=prompt_for(stage, age)[1],
                    resolving_layer=("primary" if served == MODELS[stage] else "fallback_1") if attributed else None,
                    gate_reason=("ok" if served == MODELS[stage] else "low_confidence") if attributed else None,
                    # text extraction isn't priced at all, flash-lite fallbacks never are, and 5% of
                    # other calls lose their price: the ledger estimates what it can and says so.
                    cost_usd=None if (stage == "text_extraction" or served == "gemini-3.5-flash-lite"
                                      or rng.random() < 0.05) else price,
                    code_revision=None if rng.random() < 0.03 else "a1b2c3d",
                    segment=segment, document_type=itype,
                    latency_ms=round(LATENCY_MS[stage] * slow * rng.lognormvariate(0, 0.35)),
                    status="error" if rng.random() < 0.008 else "success"))
            if touched:
                rts = received + timedelta(minutes=rng.randint(15, 240))
                reviews.append(dict(tenant=TENANT, review_id=f"{did}-r", document_id=did, ts=rts, kind="review",
                                    minutes=round(rng.lognormvariate(1.1, 0.5) * (1 + pages / 10), 1),
                                    reviewer=f"reviewer-{rng.randint(1, 6)}", stage="field_extraction"))
                if rng.random() < 0.3:
                    reviews.append(dict(tenant=TENANT, review_id=f"{did}-w", document_id=did,
                                        ts=rts + timedelta(minutes=30), kind="rework",
                                        minutes=round(rng.lognormvariate(1.8, 0.5), 1),
                                        reviewer=f"reviewer-{rng.randint(1, 6)}", stage="field_extraction"))
            indexed.append(dict(tenant=TENANT, extraction_id=f"{did}-x", document_id=did, has_positions=rng.random() < 0.85,
                                segment=segment, document_type=itype))

    with engine.begin() as conn:
        conn.execute(store.event_documents.insert(), docs)
        conn.execute(delete(store.trace_inputs).where(store.trace_inputs.c.tenant == TENANT))
        conn.execute(store.trace_inputs.insert(), [dict(tenant=TENANT, trace_id=d["document_id"], input=None,
                                                        input_ref=f"s3://demo-inbox/{d['file_hash'][7:]}.pdf",
                                                        captured_at=d["received_at"]) for d in docs])
        conn.execute(store.event_stage_runs.insert(), runs)
        conn.execute(store.event_calls.insert(), calls)
        conn.execute(store.event_indexed.insert(), indexed)
        conn.execute(store.event_reviews.insert(), reviews)
        if errors:
            conn.execute(store.event_errors.insert(), errors)

    from assay.ingest import PromptEvent, register_prompts
    register_prompts(engine, [PromptEvent(prompt_id=pid, version=ver, template=text, note=note,
                                          author="ml-team" if note else None)
                              for pid, versions in PROMPTS.values() for ver, _, text, note in versions], TENANT)

    scored = seed_document_scores(engine, docs, now)

    # Backfill one run per day over a rolling window, oldest first, so alerts
    # open and resolve in the order they would have live.
    source = EventsSource(engine, TENANT)
    run_ids = []
    for d in range(days - 2 * window_days, -1, -1):
        end = now - timedelta(days=d)
        run_ids.append(run_measures(engine, source, Window(end - timedelta(days=window_days), end), as_of=end,
                                    prompt_regressions=d < 7))
    evals = seed_evals(engine, now)
    agent = seed_agents(engine, now)
    with engine.connect() as conn:
        a = store.alerts
        open_n = len(conn.execute(select(a.c.id).where((a.c.source == SOURCE) & (a.c.state == "open"))).all())
        resolved_n = len(conn.execute(select(a.c.id).where((a.c.source == SOURCE) & (a.c.state == "resolved"))).all())
    return {"documents": len(docs), "calls": len(calls), "stage_runs": len(runs), "reviews": len(reviews),
            "errors": len(errors),
            "runs": len(run_ids), "eval_results": evals, "document_checks": scored, "agent_trajectories": agent["trajectories"],
            "alerts_open": open_n, "alerts_resolved": resolved_n}


# ---------- evaluation runs ----------

EVAL_TENANT = "demo-eval"  # its own source, so certification traffic doesn't skew production measures
EVAL_CASES = 400


def _eval_text(truth, itype, pages, us_date, garble_total):
    d = datetime.strptime(truth["date"], "%Y-%m-%d")
    total = "$?,??1.?0" if garble_total else f"${truth['total']}"
    return (f"{itype.replace('_', ' ').upper()} {truth['reference']}   {truth['vendor']}   "
            f"Date: {d:%m/%d/%Y}   " if us_date else
            f"{itype.replace('_', ' ').upper()} {truth['reference']}   {truth['vendor']}   Date: {d:%d %b %Y}   ") + \
        f"Bill to: Accounts Payable   Line items ...   Total due {total}   Page 1 of {pages}"


def seed_evals(engine: Engine, now: datetime, cases: int = EVAL_CASES, attempts: int = 3) -> int:
    """Two runs of a 400-case certification set, before and after a release, each
    case attempted three times, with one cause of each kind built in:

    - both runs: OCR loses totals on long documents (AI, long-standing); the test
      set writes 30% of totals as "$1,240.00" where the pipeline returns "1240.00"
      (evaluator too strict); 3% of expected references are typos (evaluator:
      the expected value isn't in the document); the LLM judge fails 6% of
      vendors on one attempt and passes the same output on the others
      (evaluator inconsistent); field extraction sometimes drops the last
      character of a reference on 6% of cases (flaky model, not a regression).
    - second run only: extract_fields v13 reads US dates day-first (AI regression);
      build b2e4f60 upper-cases vendor names on purpose (intended change); the
      field-extraction service times out for half an hour (infrastructure); the
      LLM judge endpoint times out on some attempts (infrastructure, in the
      harness); b2e4f60 emits totals as strings (schema check); and 3% of cases
      start taking the bill-to line as the vendor on some attempts (plausibly
      worse, too few attempts to tell: rerun).
    """
    from assay.ingest import PromptEvent, register_prompts
    t = EVAL_TENANT
    with engine.begin() as conn:
        for tbl in (store.event_documents, store.event_stage_runs, store.event_calls, store.eval_results):
            conn.execute(delete(tbl).where(tbl.c.tenant == t))
        conn.execute(delete(store.prompt_versions).where(store.prompt_versions.c.tenant == t))
        conn.execute(delete(store.failure_decisions).where(store.failure_decisions.c.source == f"events:{t}"))
    register_prompts(engine, [PromptEvent(prompt_id="extract_fields", version="v12", template=PROMPTS["field_extraction"][1][0][2]),
                              PromptEvent(prompt_id="extract_fields", version="v13", template=PROMPTS["field_extraction"][1][1][2],
                                          note="Accept European day-first dates", author="ml-team"),
                              PromptEvent(prompt_id="ocr_transcribe", version="v4", template=PROMPTS["text_extraction"][1][0][2])], t)
    runs = [(f"cert-{(now - timedelta(days=14)):%Y-%m-%d}", now - timedelta(days=14, hours=3), "v12", "a1b2c3d", False),
            (f"cert-{(now - timedelta(days=1)):%Y-%m-%d}", now - timedelta(days=1, hours=3), "v13", "b2e4f60", True)]
    docs, stage_runs, calls, results = [], [], [], []
    for run_id, start, version, build, after in runs:
        lineage = {"prompt": f"extract_fields@{version}", "model": "claude-sonnet-5", "build": build}
        outage = range(int(cases * 0.37), int(cases * 0.45))  # half an hour of field-extraction timeouts
        for i in range(cases):
            crng = random.Random(f"case-{i}")  # the same case in both runs
            itype = crng.choice(["invoice", "bank_statement", "contract", "insurance_claim"])
            pages = crng.randint(*PAGES[itype])
            truth = _truth(crng, start, itype)
            us = crng.random() < 0.4
            garble = pages >= 10 and crng.random() < 0.35
            dollar_total = crng.random() < 0.30
            typo_ref = crng.random() < 0.03
            judge_flips = crng.random() < 0.06
            flaky_ref = crng.random() < 0.06
            newly_flaky = crng.random() < 0.03
            case_id = f"case-{i:03d}"
            expected = {"reference": truth["reference"][:-2] + truth["reference"][-1] + truth["reference"][-2]
                        if typo_ref else truth["reference"], "vendor": truth["vendor"],
                        "total": f"${truth['total']}" if dollar_total else truth["total"].replace(",", ""),
                        "date": truth["date"]}
            for k in range(attempts):
                arng = random.Random(f"{run_id}-{i}-{k}")  # what varies between attempts
                did, ts = f"{run_id}/{case_id}#{k}", start + timedelta(seconds=7 * (i * attempts + k))
                timeout = after and i in outage
                fields = {"reference": truth["reference"], "vendor": truth["vendor"], "total": truth["total"],
                          "date": truth["date"]}
                if garble:
                    fields["total"] = f"{float(truth['total'].replace(',', '')) * 0.887:,.2f}"
                d = datetime.strptime(truth["date"], "%Y-%m-%d")
                if after and us and d.day <= 12 and d.day != d.month:
                    fields["date"] = f"{d.year}-{d.day:02d}-{d.month:02d}"
                if flaky_ref and arng.random() < 0.35:
                    fields["reference"] = truth["reference"][:-1]
                if after and newly_flaky and k == 1:
                    fields["vendor"] = "Accounts Payable"
                validated = dict(fields, total=fields["total"].replace(",", ""))
                if after:
                    validated["vendor"] = validated["vendor"].upper()
                steps = [("file_prep", {"pages": pages}, None),
                         ("text_extraction", {"_text": _eval_text(truth, itype, pages, us, garble)}, ("ocr_transcribe", "v4")),
                         ("classification", {"document_type": itype}, None),
                         ("field_extraction", None if timeout else fields, ("extract_fields", version)),
                         ("validation", None if timeout else validated, None)]
                docs.append(dict(tenant=t, document_id=did, received_at=ts, completed_at=ts + timedelta(minutes=2),
                                 status="completed", processing_mode="batch", segment="Certification set",
                                 document_type=itype, page_count=pages))
                for j, (stage, out, prompt) in enumerate(steps):
                    status = "timeout" if timeout and stage == "field_extraction" else "success"
                    stage_runs.append(dict(tenant=t, run_id=f"{did}-{stage}", document_id=did, stage=stage,
                                           status=status, started_at=ts + timedelta(seconds=j),
                                           finished_at=ts + timedelta(seconds=j + 1), did_work=True, sequence=j,
                                           outputs=out, prompt_id=prompt[0] if prompt else None,
                                           prompt_version=prompt[1] if prompt else None))
                    if prompt:
                        model = "claude-sonnet-5" if stage == "field_extraction" else "gemini-3-flash-preview"
                        calls.append(dict(tenant=t, call_id=f"{did}-{stage}", stage=stage, ts=ts + timedelta(seconds=j),
                                          document_id=did, model_declared=model, model_served=model,
                                          resolving_layer="primary", prompt_id=prompt[0], prompt_version=prompt[1],
                                          segment="Certification set", document_type=itype,
                                          status="timeout" if status == "timeout" else "success",
                                          latency_ms=30000 if status == "timeout" else 4000, code_revision=build))
                final = {} if timeout else validated
                rt = ts + timedelta(seconds=5)
                for field_, exp in expected.items():
                    act = final.get(field_)
                    results.append(dict(tenant=t, result_id=f"{run_id}-{case_id}-{field_}-em-{k}", run_id=run_id,
                                        case_id=case_id, document_id=did, field=field_, expected=exp, actual=act,
                                        evaluator="exact_match@2", status="pass" if act == exp else "fail", ts=rt,
                                        attempt=k, lineage=lineage, reason=None if act == exp else "values differ"))
                act = final.get("vendor")
                ok = act is not None and act.lower() == truth["vendor"].lower() and not (judge_flips and k == 0)
                harness = after and arng.random() < 0.02
                results.append(dict(tenant=t, result_id=f"{run_id}-{case_id}-vendor-judge-{k}", run_id=run_id,
                                    case_id=case_id, document_id=did, field="vendor", expected=truth["vendor"],
                                    actual=act, evaluator="llm_judge@1", attempt=k, lineage=lineage,
                                    ts=rt + timedelta(seconds=1), status="error" if harness else "pass" if ok else "fail",
                                    reason="LLM judge request timed out after 30 s" if harness else None if ok else
                                    "The vendor name does not match the one on the document."))
                schema_bad = after and not timeout and random.Random(f"schema-{i}").random() < 0.05
                results.append(dict(tenant=t, result_id=f"{run_id}-{case_id}-schema-{k}", run_id=run_id,
                                    case_id=case_id, document_id=did, field=None, expected=None, actual=None,
                                    evaluator="schema_check@1", attempt=k, lineage=lineage,
                                    ts=rt + timedelta(seconds=2), status="fail" if schema_bad else "pass",
                                    reason=f"schema violation: 'total' is a string ('{validated['total']}'), "
                                           "expected number" if schema_bad else None))
    with engine.begin() as conn:
        conn.execute(store.event_documents.insert(), docs)
        conn.execute(store.event_stage_runs.insert(), stage_runs)
        conn.execute(store.event_calls.insert(), calls)
        conn.execute(store.eval_results.insert(), results)
    return len(results)


# ---------- an agent ----------

AGENT_TENANT = "demo-agent"
AGENT_CASES = 150
AGENT_CONTRACTS = [
    dict(kind="never", step="delete_order", where={"confirmed": {"not": True}}, severity="critical",
         note="Orders are cancelled, not deleted, unless the customer confirms"),
    dict(kind="only_after", step="issue_refund", other="get_order", same=["order_id"], severity="critical",
         note="Refund only an order the agent has looked at"),
    dict(kind="max_runs", step="lookup_customer", max_runs=2, identical=True, severity="warning"),
]


def _quirks(crng) -> dict:
    return {k: crng.random() for k in ("ignore_kb", "skips", "search", "wrong_order", "by_increase", "delete", "loop")}


def _case_facts(i: int, crng) -> dict:
    return {"email": f"customer{i}@example.com", "cust": f"C-{2000 + i}", "oid": f"O-{10000 + i}",
            "other": f"O-{20000 + i}", "price": round(crng.uniform(15, 240), 2), "qty": crng.randint(1, 4),
            "status": crng.choice(["processing", "shipped", "delivered"]), "add": crng.randint(1, 3)}


def _support_run(i: int, task: str, arng, quirks: dict, ts: datetime, after: bool, down: bool = False):
    """One run of the demo support agent: (steps, answer, finished_at). `after` is prompt v5,
    with its bugs; `down` is a kb_search outage."""
    f = _case_facts(i, random.Random(f"agent-facts-{i}"))
    email, cust, oid, other = f["email"], f["cust"], f["oid"], f["other"]
    price, qty, status, add = f["price"], f["qty"], f["status"], f["add"]
    s, clock = [], [ts]

    def step(kind, **kw):
        clock[0] += timedelta(seconds=arng.uniform(0.4, 2.5))
        s.append(dict(kind=kind, started_at=clock[0], finished_at=clock[0] + timedelta(seconds=0.3), **kw))

    def think():
        tok = int(arng.lognormvariate(6.6, 0.3) * (1.25 if after else 1))
        step("reason", model="claude-sonnet-5", tokens=tok, cost_usd=round(tok * 3e-6, 6), text="Planning the next step.")

    def tool(name, args, result=None, error=None):
        think()
        step("tool", name=name, args=args, result=result, error=error)

    order = {"order_id": oid, "customer_id": cust, "qty": qty, "price": price, "status": status}
    tool("lookup_customer", {"email": email}, {"customer_id": cust, "name": f"Customer {i}"})
    if after and task == "refund_request" and quirks["loop"] < 0.25:
        for _ in range(2):
            tool("lookup_customer", {"email": email}, {"customer_id": cust, "name": f"Customer {i}"})
    looked = order
    if task in ("refund_request", "order_status") and after and quirks["search"] < 0.35:
        results = [dict(order), {**order, "order_id": other, "price": round(price * 0.6, 2)}]
        if quirks["wrong_order"] < 0.5:
            results.reverse()
        tool("search_orders", {"customer_id": cust}, results)
        looked = results[0]
    elif task == "order_status" and quirks["skips"] < 0.2 and arng.random() < 0.4:
        looked = None  # answers from memory
    elif task != "policy_question":
        tool("get_order", {"order_id": oid}, order)
    if task == "refund_request":
        tool("issue_refund", {"order_id": looked["order_id"], "amount": looked["price"]},
             {"refund_id": f"R-{i}", "status": "issued"})
        step("state", name=f"refund:{looked['order_id']}", args={"op": "create"},
             result={"order_id": looked["order_id"], "amount": looked["price"]})
        answer = f"I've refunded ${looked['price']:.2f} to your card for order {looked['order_id']}."
    elif task == "change_quantity":
        new = add if (after and quirks["by_increase"] < 0.5) else qty + add
        tool("update_order", {"order_id": oid, "qty": new}, {**order, "qty": new})
        step("state", name=f"order:{oid}", args={"op": "update"}, result={**order, "qty": new})
        answer = f"Done: order {oid} now has {new} items."
    elif task == "order_status":
        answer = f"Your order {oid} is {looked['status'] if looked else 'processing'}."
    elif task == "cancel_order":
        if after and quirks["delete"] < 0.3:
            tool("delete_order", {"order_id": oid}, {"deleted": True})
            step("state", name=f"order:{oid}", args={"op": "delete"}, result=None)
        else:
            tool("cancel_order", {"order_id": oid}, {**order, "status": "cancelled"})
            step("state", name=f"order:{oid}", args={"op": "update"}, result={**order, "status": "cancelled"})
        answer = f"Order {oid} has been cancelled."
    else:
        for _ in range(2 if down else 1):
            tool("kb_search", {"query": "return window for unopened items"},
                 None if down else {"passage": "Unopened items can be returned within 30 days of delivery."},
                 "503 Service Unavailable" if down else None)
        days = "14 days" if (down or quirks["ignore_kb"] < 0.25) else "30 days"
        answer = f"You can return unopened items within {days}."
    think()
    step("answer", text=answer)
    return s, answer, clock[0]


def _request(i: int, task: str) -> str:
    """What the customer wrote: the input a failing run is replayed with."""
    f = _case_facts(i, random.Random(f"agent-facts-{i}"))
    return {"refund_request": f"Hi, this is {f['email']}. Please refund order {f['oid']}, it arrived damaged.",
            "change_quantity": f"From {f['email']}: can you add {f['add']} more to order {f['oid']}?",
            "order_status": f"Where is my order {f['oid']}? My email is {f['email']}.",
            "cancel_order": f"Please cancel order {f['oid']} ({f['email']}), I ordered it by mistake.",
            "policy_question": f"How long do I have to return unopened items? ({f['email']})"}[task]


def seed_agents(engine: Engine, now: datetime, cases: int = AGENT_CASES, attempts: int = 3) -> dict:
    """A customer-support agent with nine tools, run on 150 test cases before and after
    a prompt release (support_agent v4 → v5), three attempts each. Built in:

    - both runs: policy answers say "14 days" although kb_search returned 30 (ignored
      a tool result, long-standing); some order-status attempts answer without looking
      the order up (flaky: stops early on some attempts).
    - v5 only: looks orders up by customer (search_orders) instead of by id, and on
      refunds sometimes refunds the wrong order (wrong tool, and an unsafe refund);
      sets the quantity to the increase instead of adding it (wrong arguments); deletes
      orders instead of cancelling them (unsafe action); repeats the customer lookup
      (a loop); and kb_search is down for a stretch (infrastructure).
    """
    from assay import agents
    from assay.ingest import PromptEvent, register_prompts
    from assay.sources.events import EventsSource
    t, source = AGENT_TENANT, f"events:{AGENT_TENANT}"
    with engine.begin() as conn:
        for tbl in (store.event_documents, store.agent_steps, store.agent_trajectories, store.agent_references,
                    store.eval_results, store.trace_inputs, store.trace_feedback):
            conn.execute(delete(tbl).where(tbl.c.tenant == t))
        conn.execute(delete(store.prompt_versions).where(store.prompt_versions.c.tenant == t))
        conn.execute(delete(store.path_contracts).where(store.path_contracts.c.source == source))
        conn.execute(delete(store.failure_decisions).where(store.failure_decisions.c.source == source))
        for tbl in (store.regression_candidates, store.suite_cases, store.pattern_log):
            conn.execute(delete(tbl).where(tbl.c.source == source))
        for c in AGENT_CONTRACTS:
            conn.execute(store.path_contracts.insert().values(source=source, updated_at=now, **c))
    register_prompts(engine, [PromptEvent(prompt_id="support_agent", version="v4", template="You are a support agent…"),
                              PromptEvent(prompt_id="support_agent", version="v5", template="You are a support agent… "
                                          "If the order id is unclear, search the customer's orders.",
                                          note="Search a customer's orders when the order id is unclear",
                                          author="agents-team")], t)
    tasks = ["refund_request"] * 35 + ["change_quantity"] * 20 + ["order_status"] * 20 + ["cancel_order"] * 10 + \
        ["policy_question"] * 15
    heads, steps, docs, refs = [], [], [], []
    runs = [(f"agent-{(now - timedelta(days=14)):%Y-%m-%d}", now - timedelta(days=14, hours=2), "v4", "c7d1e02", False),
            (f"agent-{(now - timedelta(days=1)):%Y-%m-%d}", now - timedelta(days=1, hours=2), "v5", "d93a4b8", True)]
    for run_id, start, version, build, after in runs:
        lineage = {"prompt": f"support_agent@{version}", "model": "claude-sonnet-5", "build": build}
        outage = range(int(cases * 0.35), int(cases * 0.65))  # kb_search down for a stretch of the run
        for i in range(cases):
            crng = random.Random(f"agent-case-{i}")
            task = crng.choice(tasks)
            fx = _case_facts(i, random.Random(f"agent-facts-{i}"))
            oid, other, price, qty, status, add = fx["oid"], fx["other"], fx["price"], fx["qty"], fx["status"], fx["add"]
            quirks = _quirks(crng)
            case_id = f"case-{i:03d}"
            ref = {"case_id": case_id, "allow_extra": ["lookup_customer", "kb_search"], "state": [],
                   "answer_match": "contains"}
            if task == "refund_request":
                ref |= {"calls": [{"tool": "get_order", "args": {"order_id": oid}},
                                  {"tool": "issue_refund", "args": {"order_id": oid, "amount": price}}],
                        "answer": f"{price:.2f}", "max_steps": 12,
                        "state": [{"object": f"refund:{oid}", "exists": True},
                                  {"object": f"refund:{oid}", "field": "amount", "equals": price},
                                  {"object": f"refund:{other}", "exists": False}]}
            elif task == "change_quantity":
                ref |= {"calls": [{"tool": "get_order", "args": {"order_id": oid}},
                                  {"tool": "update_order", "args": {"order_id": oid, "qty": qty + add}}],
                        "answer": str(qty + add), "max_steps": 10,
                        "state": [{"object": f"order:{oid}", "field": "qty", "equals": qty + add}]}
            elif task == "order_status":
                ref |= {"calls": [{"tool": "get_order", "args": {"order_id": oid}}], "answer": status, "max_steps": 8}
            elif task == "cancel_order":
                ref |= {"calls": [{"tool": "get_order", "args": {"order_id": oid}},
                                  {"tool": "cancel_order", "args": {"order_id": oid}}], "answer": "cancelled",
                        "max_steps": 10, "state": [{"object": f"order:{oid}", "exists": True},
                                                   {"object": f"order:{oid}", "field": "status", "equals": "cancelled"}]}
            else:
                ref |= {"calls": [{"tool": "kb_search", "args": {"query": "*"}}], "answer": "30 days", "max_steps": 8,
                        "allow_extra": ["lookup_customer"]}
            if after:
                refs.append(ref)
            for k in range(attempts):
                arng = random.Random(f"{run_id}-{i}-{k}")
                tid = f"{run_id}.{case_id}.a{k}"
                ts = start + timedelta(seconds=20 * (i * attempts + k))
                s, answer, end_at = _support_run(i, task, arng, quirks, ts, after, down=after and i in outage)
                heads.append(dict(tenant=t, trajectory_id=tid, run_id=run_id, case_id=case_id, attempt=k, task=task,
                                  started_at=ts, finished_at=end_at, answer=answer, status="completed", lineage=lineage))
                docs.append(dict(tenant=t, document_id=tid, received_at=ts, completed_at=end_at, status="completed",
                                 document_type=task, segment="Support agent eval"))
                steps += [dict(tenant=t, trajectory_id=tid, seq=n, name=None, args=None, result=None, error=None,
                               text=None, model=None, tokens=None, cost_usd=None) | x for n, x in enumerate(s)]
    now_ = datetime.utcnow()
    with engine.begin() as conn:
        conn.execute(store.agent_trajectories.insert(), heads)
        conn.execute(store.agent_steps.insert(), steps)
        conn.execute(store.event_documents.insert(), docs)
        conn.execute(store.agent_references.insert(), [dict(tenant=t, updated_at=now_, calls=r.get("calls"),
                                                           allow_extra=r["allow_extra"], answer=r.get("answer"),
                                                           answer_match=r["answer_match"], state=r["state"] or None,
                                                           max_steps=r.get("max_steps"), case_id=r["case_id"])
                                                      for r in refs])
    live = _agent_production(engine, now, tasks)
    src = EventsSource(engine, t)
    results = sum(agents.evaluate_run(engine, src, t, r[0])["results"] for r in runs)
    with engine.begin() as conn:
        ids = [r[0] for r in conn.execute(select(store.measure_runs.c.id).where(store.measure_runs.c.source == source))]
        if ids:
            conn.execute(delete(store.measure_results).where(store.measure_results.c.run_id.in_(ids)))
            conn.execute(delete(store.measure_runs).where(store.measure_runs.c.id.in_(ids)))
        conn.execute(delete(store.alerts).where(store.alerts.c.source == source))
    # One run over both evaluation runs, so the overview, measures and workflow graph cover the agent.
    run_measures(engine, src, Window(now - timedelta(days=15), now), as_of=now, prompt_regressions=False)
    # A developer already turned two production patterns into regression cases (the rest wait for review).
    from assay import learn
    from assay.runner import CachedSource
    live_src, week = CachedSource(src), Window(now - timedelta(days=7), now)
    pats = learn.patterns(live_src, week, engine)["patterns"]
    for kind, step in (("loop", "lookup_customer"), ("contract", "delete_order")):
        p = next((x for x in pats if x["type"] == kind and x["stage"] == step), None)
        if p:
            made = learn.propose(live_src, week, engine, p["key"])
            if made:
                learn.approve(engine, source, made[0]["id"], "support-agent-regressions", by="demo@assay")
    return {"trajectories": len(heads), "steps": len(steps), "eval_results": results, "production": live}


def _agent_production(engine: Engine, now: datetime, tasks: List[str], per_day: int = 80, days: int = 7) -> int:
    """A week of live traffic for the demo agent: v4 until two days ago, then v5 with its
    bugs; the customer's message captured as the input; thumbs down and retries on some
    bad answers; kb_search down for eight hours yesterday."""
    t = AGENT_TENANT
    heads, steps, docs, inputs, feedback = [], [], [], [], []
    outage = (now - timedelta(hours=26), now - timedelta(hours=18))  # kb_search down for eight hours
    n = 0
    for d in range(days, 0, -1):
        for _ in range(per_day):
            rng = random.Random(f"live-{n}")
            ts = now - timedelta(days=d) + timedelta(seconds=rng.randint(0, 86399))
            i = 1000 + rng.randint(0, 3999)  # a customer
            task = rng.choice(tasks)
            after = ts > now - timedelta(days=2)
            quirks = _quirks(rng)
            s, answer, end_at = _support_run(i, task, random.Random(f"live-run-{n}"), quirks, ts, after,
                                             down=outage[0] <= ts <= outage[1])
            ok_steps, ok_answer, _ = _support_run(i, task, random.Random(f"live-run-{n}"), {k: 1.0 for k in quirks}, ts,
                                                  False)
            bad = answer != ok_answer or [x.get("name") for x in s if x["kind"] == "tool"] != \
                [x.get("name") for x in ok_steps if x["kind"] == "tool"]
            tid = f"live-{ts:%m%d}-{n:04d}"
            heads.append(dict(tenant=t, trajectory_id=tid, run_id=None, case_id=None, attempt=None, task=task,
                              started_at=ts, finished_at=end_at, answer=answer, status="completed",
                              lineage={"prompt": f"support_agent@{'v5' if after else 'v4'}", "model": "claude-sonnet-5",
                                       "build": "d93a4b8" if after else "c7d1e02"}))
            docs.append(dict(tenant=t, document_id=tid, received_at=ts, completed_at=end_at, status="completed",
                             document_type=task, segment="Support agent (live)"))
            steps += [dict(tenant=t, trajectory_id=tid, seq=k, name=None, args=None, result=None, error=None, text=None,
                           model=None, tokens=None, cost_usd=None) | x for k, x in enumerate(s)]
            inputs.append(dict(tenant=t, trace_id=tid, input=_request(i, task), input_ref=None, captured_at=ts))
            r = rng.random()
            if bad and r < 0.35:
                feedback.append(dict(tenant=t, feedback_id=f"{tid}-fb", trace_id=tid, kind="thumbs_down",
                                     ts=end_at + timedelta(minutes=2), note=None))
            elif bad and r < 0.5:
                feedback.append(dict(tenant=t, feedback_id=f"{tid}-fb", trace_id=tid, kind="retry",
                                     ts=end_at + timedelta(minutes=1), note=None))
            elif not bad and r < 0.08:
                feedback.append(dict(tenant=t, feedback_id=f"{tid}-fb", trace_id=tid, kind="thumbs_up",
                                     ts=end_at + timedelta(minutes=2), note=None))
            n += 1
    with engine.begin() as conn:
        conn.execute(store.agent_trajectories.insert(), heads)
        conn.execute(store.agent_steps.insert(), steps)
        conn.execute(store.event_documents.insert(), docs)
        conn.execute(store.trace_inputs.insert(), inputs)
        if feedback:
            conn.execute(store.trace_feedback.insert(), feedback)
    return len(heads)


# ---------- document scoring: what assay_sdk.documents records, on a sample of the documents ----------

SCORED_SHARE = 0.12  # of the day's documents, the share a person labelled for scoring
SCORING_EVALUATORS = ("assay.documents@1", "assay.spotcheck@1", "assay.superseded@1")
DATE_FIX_DAYS = 6  # extract_fields v13: "Accept European day-first dates"
TYPE_FIX_DAYS = 14  # classify_document v8: fewer contract/claim mix-ups
EUROPEAN = {"Contoso Insurance", "Umbrella Legal"}


class _Collect:
    """Stands in for a test case's run: keeps the checks the scorers record."""

    def __init__(self):
        self.checks = []

    def check(self, field, status, **kw):
        self.checks.append({"field": field, "status": status, **kw})


def _row(doc: dict, c: dict, run_id: str, case: str, ts: datetime, n: int) -> dict:
    from assay import ingest
    text = lambda v: None if v is None else v if isinstance(v, str) else json.dumps(v, default=str)
    return {"tenant": TENANT, "result_id": ingest._derive(run_id, case, c["field"], str(n)), "run_id": run_id,
            "case_id": case, "document_id": doc["document_id"], "field": c["field"], "status": c["status"],
            "expected": text(c.get("expected")), "actual": text(c.get("actual")), "evaluator": c.get("evaluator"),
            "score": c.get("score"), "reason": (c.get("reason") or None) and c["reason"][:2000], "ts": ts,
            "attempt": 0, "raw_output": c.get("raw_output"), "category": c.get("category"),
            "error_kind": c.get("error_kind")}


def seed_document_scores(engine: Engine, docs: List[dict], now: datetime, seed_value: int = 29) -> int:
    """A labelled sample of the demo's documents scored the way assay_sdk.documents does it: fields,
    line items, types, splits, OCR, locations, tables, confidence, spot checks of published output
    and superseded values, with the story the rest of the demo tells: day-first dates fixed by
    extract_fields v13, totals mangled by the validation release, contract/claim mix-ups cut by
    classify_document v8, two-column statements read out of order, and corrections for Globex
    Logistics that never reached output."""
    import assay_sdk
    from assay_sdk import documents as dx
    rng = random.Random(seed_value)
    schema = {"reference": dx.Text(weight=3), "vendor": dx.Text(), "total": dx.Money(weight=3),
              "date": dx.Date(day_first=True),
              "line_items": dx.LineItems({"description": dx.Text(), "amount": dx.Money()}, key="description")}
    rows: List[dict] = []
    sent: List[tuple] = []  # checks the production-side calls (spot_check, superseded_values) send

    def capture(test_run, case, status, **kw):
        sent.append((case, {"status": status, **kw}))
    real, assay_sdk.check = assay_sdk.check, capture
    try:
        for doc in docs:
            globex_now = doc["segment"] == "Globex Logistics" and (now - doc["received_at"]).days < 5
            if rng.random() >= (0.5 if globex_now else SCORED_SHARE) or doc["document_type"] is None:
                continue  # half of Globex's latest are checked: their corrections went missing
            age = (now - doc["received_at"]).total_seconds() / 86400
            itype, seg, did = doc["document_type"], doc["segment"], doc["document_id"]
            truth = _truth(rng, doc["received_at"], itype)
            items = _items(rng, truth["total"])
            truth["line_items"] = items
            got = {**truth, "line_items": [dict(x) for x in items]}
            conf = {k: round(rng.uniform(0.9, 0.995), 3) for k in ("reference", "vendor", "total", "date")}
            d = datetime.strptime(truth["date"], "%Y-%m-%d")
            if seg in EUROPEAN and age >= DATE_FIX_DAYS and d.day <= 12 and d.day != d.month and rng.random() < 0.6:
                got["date"] = d.replace(month=d.day, day=d.month).strftime("%Y-%m-%d")  # read month first
                conf["date"] = round(rng.uniform(0.9, 0.99), 3)  # and sure of it
            if age < RELEASE_BUG_DAYS and rng.random() < 0.35:  # validation v2.4 moves the decimal point
                got["total"] = f"{float(truth['total'].replace(',', '')) * 100:,.2f}"
                conf["total"] = round(rng.uniform(0.93, 0.99), 3)
            if rng.random() < 0.03:
                got["vendor"] = rng.choice(VENDORS)
                conf["vendor"] = round(rng.uniform(0.5, 0.8), 3)
            if rng.random() < 0.02:
                got["reference"] = ""
            if rng.random() < 0.06 and got["line_items"]:
                got["line_items"][-1]["amount"] = f"{float(got['line_items'][-1]['amount']) + 1:.2f}"
            run = _Collect()
            dx.score_document(run, truth, got, schema, rules=[dx.total_of("line_items.amount", equals="total")],
                              confidence=conf)
            mixups = {"contract": "insurance_claim", "insurance_claim": "contract"}
            wrong_type = itype in mixups and rng.random() < (0.12 if age >= TYPE_FIX_DAYS else 0.03)
            dx.classify_document(run, itype, mixups[itype] if wrong_type else itype,
                                 confidence=round(rng.uniform(0.85, 0.99), 3))
            pages = doc["page_count"] or 1
            text = _text(truth, itype, pages)
            lines = [text[i:i + 40] for i in range(0, len(text), 40)]
            read = list(lines)
            if itype == "bank_statement" and rng.random() < 0.5:  # two columns, read across
                half = len(read) // 2
                read = [x for pair in zip(read[:half], read[half:]) for x in pair] + read[2 * half:]
            if rng.random() < 0.3:
                k = rng.randrange(len(read))
                read[k] = read[k].replace("0", "O", 1).replace("l", "1", 1)
            dx.score_ocr(run, "\n".join(lines), "\n".join(read), page=1, max_cer=0.05)
            box = [100, 700, 220, 716]
            moved = rng.random() < 0.08
            dx.score_locations(run, {"total": {"page": 1, "bbox": box}},
                               {"total": {"page": 2 if moved and pages > 1 else 1,
                                          "bbox": [120, 640, 240, 656] if moved else [102, 699, 221, 717]}})
            table = [["Description", "Amount"]] + [[x["description"], x["amount"]] for x in items]
            read_table = [list(r) for r in table]
            if rng.random() < 0.07:
                read_table = [["Description Amount"]] + [[f"{a} {b}"] for a, b in table[1:]]  # columns merged
            dx.score_table(run, table, read_table, name="line items", cells={"Amount": dx.Money()})
            if itype == "bank_statement" and pages >= 3:  # a statement file holding two statements
                cut = rng.randint(2, pages)
                bad = rng.random() < (0.15 if age < 7 else 0.05)
                dx.score_split(run, [1, cut], [1, cut + 1 if cut < pages else cut - 1] if bad else [1, cut],
                               page_count=pages)
            ts = doc["received_at"] + timedelta(minutes=5)
            day = doc["document_id"].split("-")[1]
            rows += [_row(doc, c, f"demo-scoring-{day}", did, ts, n) for n, c in enumerate(run.checks)]
            # Production side: a person re-checks some published values; corrections arrive for some.
            if rng.random() < 0.4:
                auto = rng.random() < 0.6
                escaped = got["total"] != truth["total"] and (auto or rng.random() < 0.2)
                dx.spot_check(did, "total", got["total"] if escaped else truth["total"], truth["total"], dx.Money(),
                              reviewed=not auto, auto_approved=auto, checked_by=f"auditor-{rng.randint(1, 3)}")
                if rng.random() < 0.05:
                    dx.spot_check(did, "vendor", rng.choice(VENDORS) if auto and rng.random() < 0.3 else truth["vendor"],
                                  truth["vendor"],
                                  reviewed=not auto, auto_approved=auto)
            if rng.random() < (0.8 if globex_now else 0.2):  # a corrected version of the document arrived later
                new = {**truth, "total": f"{float(truth['total'].replace(',', '')) * 0.9:,.2f}"}
                stuck = seg == "Globex Logistics" and age < 5
                output = truth if stuck or rng.random() < 0.05 else new
                dx.superseded_values(did, f"{did}-corrected", truth, new, output,
                                     flagged=["total"] if not stuck and rng.random() < 0.3 else (),
                                     schema={"total": dx.Money()}, link=rng.choice(["replaces", "amends"]))
            for n, (case, c) in enumerate(sent):
                run_id = "spot-checks" if c.get("evaluator") == "assay.spotcheck@1" else "superseded"
                rows.append(_row(doc, c, run_id, case, ts + timedelta(days=1), 100 + n))
            sent.clear()
    finally:
        assay_sdk.check = real
    with engine.begin() as conn:
        t = store.eval_results
        conn.execute(delete(t).where((t.c.tenant == TENANT) & t.c.evaluator.in_(SCORING_EVALUATORS)))
        for i in range(0, len(rows), 2000):
            conn.execute(t.insert(), rows[i:i + 2000])
    return len(rows)


def _items(rng, total: str) -> List[dict]:
    """Line items that add up to the total."""
    amount = float(total.replace(",", ""))
    n = rng.randint(1, 4)
    cuts = sorted(round(rng.uniform(0.1, 0.9) * amount, 2) for _ in range(n - 1))
    parts = [b - a for a, b in zip([0.0] + cuts, cuts + [amount])]
    names = ["Freight", "Handling", "Consulting", "Licence", "Storage", "Parts"]
    rng.shuffle(names)
    return [{"description": names[i], "amount": f"{p:.2f}"} for i, p in enumerate(parts)]
