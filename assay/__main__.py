"""Command line: python -m assay <command>"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import select

from assay import runner, store
from assay.config import Settings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="assay", description="Evaluation and observability for document-intelligence pipelines.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the API and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8400)
    s.add_argument("--every", type=int, metavar="MINUTES",
                   help="Also run measures every N minutes (overrides ASSAY_SCHEDULE_MINUTES)")
    s.add_argument("--source", action="append", dest="sources", metavar="SOURCE",
                   help="Source to schedule, repeatable (overrides ASSAY_SCHEDULE_SOURCES)")
    s.add_argument("--window-days", type=float, help="Window each scheduled run covers (default 1)")

    r = sub.add_parser("run", help="Compute all measures once and store the results")
    r.add_argument("--source", default="sql", help="sql (your pipeline database) or events:<tenant>")
    r.add_argument("--days", type=int, default=7)

    sub.add_parser("check-source", help="Test every mapped field against your pipeline database")
    cn = sub.add_parser("connect", help="Attach Assay to your pipeline: what's here and the least-work way in; "
                                        "db (read the schema, write the mapping), code (the change, as a diff), "
                                        "verify (the pipeline Assay found)")
    cn.add_argument("what", nargs="?", choices=["db", "code", "evals", "verify"],
                    help="Leave out for what's here; evals: a proposed test for every model call")
    cn.add_argument("target", nargs="?", help="db: the database URL; code: the folder or file (default: here)")
    cn.add_argument("--apply", action="store_true", help="code, evals: write the change (it's shown first either way)")
    cn.add_argument("--out", help="db: where to write the mapping (default: mappings/<database>.json)")
    cn.add_argument("--force", action="store_true", help="db: overwrite the mapping file")
    cn.add_argument("--password-command", metavar="CMD",
                    help="db: a command that prints a short-lived password (your cloud CLI's login), "
                         "run when connecting; nothing is stored")
    cn.add_argument("--preset", choices=["aws-rds", "azure", "gcloud", "okta", "snowflake-sso"],
                    help="db: build the password command for your cloud's login (suggested when one fits)")
    cn.add_argument("--profile", help="db: the AWS profile, for aws-rds and okta")

    k = sub.add_parser("keys", help="Create, list and revoke API keys")
    ks = k.add_subparsers(dest="keys_cmd", required=True)
    kc = ks.add_parser("create", help="Create a key; the secret is printed once")
    kc.add_argument("--tenant", required=True, help="Tenant the key belongs to, or '*' for a platform key")
    kc.add_argument("--scopes", required=True, help="Comma-separated: ingest, read, manage, admin")
    kc.add_argument("--name", required=True, help="What uses it, e.g. 'invoice pipeline (prod)'")
    kc.add_argument("--expires-in-days", type=int)
    kl = ks.add_parser("list", help="List keys (never shows secrets)")
    kl.add_argument("--tenant")
    kr = ks.add_parser("revoke", help="Revoke a key immediately")
    kr.add_argument("id", type=int)

    b = sub.add_parser("backfill", help="Replay past days so baselines and alerts work from day one")
    b.add_argument("--source", default="sql", help="sql or events:<tenant>")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--window-days", type=float, default=1.0)

    c = sub.add_parser("coverage", help="Which measures your data can answer, and what would unlock the rest")
    c.add_argument("--source", default="sql", help="sql or events:<tenant>")
    c.add_argument("--days", type=float, default=7)
    sub.add_parser("demo", help="Load a synthetic demo tenant and backfill 7 weeks of daily runs")
    ld = sub.add_parser("load", help="Load events the SDK recorded locally (no server set) into the store")
    ld.add_argument("file", nargs="?", default=".assay/events.jsonl")
    ld.add_argument("--tenant", default="local", help="Tenant to load them into (default: local)")
    sub.add_parser("init", help="Set up local testing here: assay.toml and a runnable example")
    t = sub.add_parser("test", help="Run your tests with the SDK recording, check every run, compare with the "
                                    "last run that passed")
    t.add_argument("--repeat", type=int, metavar="N", help="Attempts per case (overrides assay.toml)")
    t.add_argument("--baseline", metavar="RUN", help="Compare with this run instead of the last that passed; "
                                                     "'none' for no baseline")
    t.add_argument("--upload", action="store_true", help="Also send the run to a server (ASSAY_URL, ASSAY_KEY)")
    t.add_argument("--junit", metavar="PATH", help="Also write JUnit XML, for CI to show each case")
    t.add_argument("--timeout", type=float, metavar="SECONDS", help="Stop an attempt that runs longer (overrides "
                                                                     "assay.toml)")
    t.add_argument("--failed", action="store_true", help="Run only the cases that didn't pass last time (pytest)")
    t.add_argument("--judge", action="store_true", help="Also have an LLM judge plan quality and consistency "
                                                        "(needs `pip install anthropic`; a model call per run)")
    t.add_argument("command", nargs=argparse.REMAINDER, help="-- <command> (overrides assay.toml)")
    u = sub.add_parser("upload", help="Send a test run (the latest, by default) to an Assay server")
    u.add_argument("run", nargs="?", help="A run id instead of the latest")
    for q in (t, u):
        q.add_argument("--url", help="Server address (default: ASSAY_URL)")
        q.add_argument("--key", help="API key with the ingest scope (default: ASSAY_KEY)")
        q.add_argument("--tenant", dest="send_tenant", metavar="TENANT",
                       help="Tenant to send to (a tenant key's own is used otherwise)")
    pc = sub.add_parser("pr-comment", help="Post the latest run's summary on the pull request (GitHub Actions), "
                                           "updating Assay's earlier comment")
    pc.add_argument("--summary", default=".assay/summary.md")
    pc.add_argument("--pr", type=int, help="The PR number (default: from the GitHub Actions event)")
    pc.add_argument("--repo", help="owner/name (default: GITHUB_REPOSITORY)")
    d = sub.add_parser("diff", help="What behavior changed between two runs: regressions with the flow before "
                                    "and after, improvements, flaky cases, severity")
    d.add_argument("baseline", nargs="?", help="A run id or a version (default: each case's last passing run)")
    d.add_argument("current", nargs="?", help="A run id or a version (default: the latest run)")
    d.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    a = sub.add_parser("accept", help="Make the latest test run the baseline, known failures and all; its "
                                      "failures are acknowledged for a while (assay acks)")
    a.add_argument("run", nargs="?", help="A run id instead of the latest")
    a.add_argument("--reason", default="accepted with `assay accept`", help="Why its failures are known")
    a.add_argument("--for", dest="for_", default="14d", metavar="DURATION",
                   help="How long its failures stay acknowledged: 36h, 14d, 2w (at most 90 days)")
    ak = sub.add_parser("ack", help="Acknowledge a failing check: quiet until it gets worse than it is now, "
                                    "and only for a while")
    ak.add_argument("case", help="The test case (a pytest id, or a unique part of one)")
    ak.add_argument("checks", nargs="*", help="Its checks (answer, consistency, behavior.cost_usd, ...); "
                                              "default: every check it fails")
    ak.add_argument("--reason", required=True, help="Why it's known: a ticket, a decision")
    ak.add_argument("--for", dest="for_", default="14d", metavar="DURATION",
                    help="How long: 36h, 14d, 2w (default 14d, at most 90 days)")
    ak.add_argument("--by", help="Who (default: git config user.name)")
    ak.add_argument("--run", help="The run to take its state from (default: the latest)")
    g = sub.add_parser("golden", help="The golden set a judge is calibrated against: outputs a person scored")
    gs = g.add_subparsers(dest="golden_cmd", required=True)
    ga = gs.add_parser("add", help="Label a recorded output (or give --output); a second person adds a label")
    ga.add_argument("case", help="The test case, or the item's id")
    ga.add_argument("--score", type=float, required=True, help="Your score, on the labels' scale")
    ga.add_argument("--by", help="Who labeled it (default: git config user.name)")
    ga.add_argument("--tags", default="", help="Comma-separated, e.g. behavioral,product_sense")
    ga.add_argument("--run", help="The run to take the output from (default: the latest)")
    ga.add_argument("--input", dest="input_", help="The input, if it wasn't recorded")
    ga.add_argument("--output", help="The output, if it wasn't recorded")
    ga.add_argument("--note")
    ga.add_argument("--critique", help="Why this score: what a judge should learn from it")
    gcl = gs.add_parser("claims", help="Add the claims experts called supported or wrong (a server) as labels")
    gcl.add_argument("--url")
    gcl.add_argument("--source", default="events:default")
    gcl.add_argument("--days", type=float)
    gp = gs.add_parser("split", help="Assign train, dev and test, stratified by label, so a judge isn't measured "
                                     "on what it learned from")
    gp.add_argument("--train", type=float, default=0.2)
    gp.add_argument("--dev", type=float, default=0.4)
    gp.add_argument("--seed", type=int, default=0)
    gp.add_argument("--by", choices=["tags", "input"], help="Keep whole groups together: items with the same tags, or "
                                                          "answers to the same input, all go to one split")
    gs.add_parser("stats", help="Labels per score, labelers, and how much people agree")
    gg = gs.add_parser("suggest", help="Recorded outputs to label next, spread over the judge's scores")
    gg.add_argument("-n", type=int, default=10)
    gg.add_argument("--field", help="The judge's check (default: [calibrate] field)")
    gg.add_argument("--vs", help="Another judge's check: pick the runs the two scored furthest apart")
    gg.add_argument("--disagree", action="store_true",
                    help="Pick runs the judge passed but a deterministic check failed")
    mx = sub.add_parser("matrix", help="Transition failure matrix of a test run: the last step that went right "
                                       "against the first that failed")
    mx.add_argument("--run", help="A test run (default: the latest)")
    mx.add_argument("--baseline", help="Another run, to see which transition got worse")
    mx.add_argument("--task")
    mx.add_argument("--format", choices=["text", "json"], default="text")
    tg = sub.add_parser("triage", help="For each failure category: fix the prompt, a code check, or (if it persists) "
                                       "a judge; drafts the code checks")
    tg.add_argument("--url")
    tg.add_argument("--source", default="events:default")
    tg.add_argument("--category", type=int)
    tg.add_argument("--run", action="store_true", help="Triage now (a model call per category)")
    tg.add_argument("--apply", action="store_true", help="Write the drafted code checks")
    tg.add_argument("--folder", default="tests/ai")
    ev = sub.add_parser("evals", help="The evaluators themselves")
    evs = ev.add_subparsers(dest="evals_cmd", required=True)
    ea = evs.add_parser("audit", help="What each evaluator costs to keep: code checks and judges apart")
    ea.add_argument("--days", type=float, default=30)
    ea.add_argument("--format", choices=["text", "json"], default="text")
    eg = evs.add_parser("guardrails", help="Which evaluators could run in the request path: latency, cost, and false "
                                            "positives and negatives against people's labels")
    eg.add_argument("--days", type=float, default=30)
    eg.add_argument("--format", choices=["text", "json"], default="text")
    eg.add_argument("--export", help="Write the candidates to this file, for your guardrail layer")

    rd = sub.add_parser("redact", help="Check redaction: personal data that got through, and whether edited traces "
                                       "still behave like the real ones")
    rs = rd.add_subparsers(dest="redact_cmd", required=True)
    rc = rs.add_parser("check", help="Scan what was recorded (.assay/events.jsonl, or a server) for personal data")
    rc.add_argument("--file")
    rc.add_argument("--url")
    rc.add_argument("--source", default="events:default")
    rc.add_argument("--days", type=float, default=7)
    rp = rs.add_parser("replay", help="Run recorded inputs through the app with personal data replaced, and compare")
    rp.add_argument("--app", help='The entry point, as "app/bot.py:answer" (default: [synthetic] app)')
    rp.add_argument("--file")
    rp.add_argument("--mode", choices=["pseudonym", "placeholder"], default="pseudonym",
                    help="Realistic stand-ins (default), or placeholders like <email>")
    rp.add_argument("--no-control", action="store_true", help="Don't rerun the original to tell the app's own variation apart")
    rp.add_argument("--limit", type=int)

    sy = sub.add_parser("synth", help="Synthetic data for error analysis: dimensions, tuples, queries, runs through "
                                      "the app, and a comparison with real traffic")
    ss = sy.add_subparsers(dest="synth_cmd", required=True)
    st = ss.add_parser("tuple", help='A tuple written by hand: one value per dimension, "Dimension=value" each')
    st.add_argument("pairs", nargs="+")
    st.add_argument("--note", help="What it's there to test")
    ss.add_parser("check", help="Coverage per value, and values the system prompt never mentions")
    sg = ss.add_parser("tuples", help="More tuples: every combination filtered by a model (default), or --direct")
    sg.add_argument("--direct", action="store_true", help="Ask a model for realistic combinations instead")
    sg.add_argument("-n", type=int, default=50, help="With --direct: how many")
    sg.add_argument("--no-filter", action="store_true", help="Keep every combination, unfiltered")
    sg.add_argument("--force", action="store_true", help="Generate before 20 are written by hand")
    sq = ss.add_parser("queries", help="Each tuple as the message a user would send, in a prompt of its own")
    sq.add_argument("--per", type=int, default=1, help="Queries per tuple")
    ss.add_parser("personas", help="Each tuple as a persona, for multi-turn runs (assay_sdk.simulate)")
    sr = ss.add_parser("run", help="Send the queries (or --personas) through the app, recorded as synthetic runs")
    sr.add_argument("--app", help='The entry point, as "app/bot.py:answer" (default: [synthetic] app)')
    sr.add_argument("--personas", action="store_true", help="Simulated conversations instead of single queries")
    sr.add_argument("--limit", type=int)
    sc = ss.add_parser("compare", help="Synthetic runs against production on the same dimensions (a server)")
    sc.add_argument("--source", default="events:default")
    sc.add_argument("--days", type=float, default=30)
    sc.add_argument("--sample", type=int, default=100, help="Production conversations to place (a model call each)")
    sc.add_argument("--url")

    cb = sub.add_parser("calibrate", help="Run the judge over the golden set: ranking, agreement, bias, consistency; "
                                          "compared with the last calibration that passed")
    cb.add_argument("--baseline", help="A calibration id, or 'none'")
    cb.add_argument("--judge", help="module:function or path.py:function (default: [calibrate] judge)")
    cb.add_argument("--repeat", type=int, help="Judgements per item (default: [calibrate] repeat)")
    cb.add_argument("--second-judge", help="Another judge over the same items, to see where they disagree "
                                           "(default: [calibrate] second_judge)")
    cb.add_argument("--format", choices=["text", "json"], default="text")
    cb.add_argument("--final", action="store_true", help="Report on the held-out test split (default: dev)")
    rp = sub.add_parser("report", help="What the evaluation found this week: issues caught before users saw them, "
                                       "failure modes, fixes, the log")
    rp.add_argument("--days", type=float, default=7)
    rp.add_argument("--format", choices=["markdown", "json"], default="markdown")
    rp.add_argument("--out", help="Write it to a file too")
    lg = sub.add_parser("log", help="The running log of findings: add what you learned")
    lgs = lg.add_subparsers(dest="log_cmd", required=True)
    la = lgs.add_parser("add", help="Add a note: an error found, what was learned, the fix, the impact avoided")
    la.add_argument("text")
    la.add_argument("--by", help="Who (default: git config user.name)")
    al = sub.add_parser("acks", help="What's acknowledged, what expires soon, and what woke up")
    al.add_argument("--prune", action="store_true", help="Remove the ones that ended (expired, or passing since)")
    sub.add_parser("schema", help="Print the v1 event schema as JSON Schema")

    args = p.parse_args(argv)
    if args.cmd == "pr-comment":
        from assay import github, local
        repo, pr, token = args.repo or os.environ.get("GITHUB_REPOSITORY"), args.pr or github.pr_number(), \
            os.environ.get("GITHUB_TOKEN")
        if not pr:
            print("Not a pull request: nothing to comment on.")
            return 0
        if not (repo and token):
            print("Set GITHUB_TOKEN and GITHUB_REPOSITORY (GitHub Actions sets the second; pass the first "
                  "from secrets.GITHUB_TOKEN).", file=sys.stderr)
            return 2
        try:
            body = open(args.summary, encoding="utf-8").read()
        except OSError:
            print(f"No summary at {args.summary}: run `pytest --assay` or `assay test` first.", file=sys.stderr)
            return 2
        try:
            print(f"{github.comment(body, repo, pr, token, local.MARKER).capitalize()} the comment on PR #{pr}.")
        except (RuntimeError, OSError) as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0
    if args.cmd == "diff":
        from pathlib import Path
        from assay import diff
        return diff.main(Path.cwd(), args.baseline, args.current, args.format)
    if args.cmd == "matrix":
        from pathlib import Path
        from assay import transitions
        return transitions.cli(Path.cwd(), args.run, args.baseline, args.task, args.format)
    if args.cmd == "triage":
        from pathlib import Path
        from assay import triage
        return triage.cli(Path.cwd(), args)
    if args.cmd == "evals":
        from pathlib import Path
        if args.evals_cmd == "guardrails":
            from assay import guardrails
            return guardrails.cli(Path.cwd(), args.days, args.format, args.export)
        from assay import upkeep
        return upkeep.cli(Path.cwd(), args.days, args.format)
    if args.cmd == "redact":
        from pathlib import Path
        from assay import redaction
        return redaction.cli(Path.cwd(), args)
    if args.cmd == "synth":
        from pathlib import Path
        from assay import synth
        return synth.cli(Path.cwd(), args)
    if args.cmd in ("init", "test", "accept", "upload", "ack", "acks", "golden", "calibrate", "report", "log"):
        from pathlib import Path
        from assay import local
        root = Path.cwd()
        if args.cmd == "upload":
            return local.upload(root, args.run, args.url, args.key, args.send_tenant)
        if args.cmd == "accept":
            return local.accept(root, args.run, args.reason, args.for_)
        if args.cmd == "ack":
            return local.ack(root, args.case, args.checks, args.reason, args.for_, args.by, args.run)
        if args.cmd == "acks":
            return local.list_acks(root, args.prune)
        if args.cmd == "report":
            return local.report_cmd(root, args.days, args.format, args.out)
        if args.cmd == "log":
            return local.log_add(root, args.text, args.by)
        if args.cmd == "calibrate":
            return local.calibrate_cmd(root, args.baseline, args.format, args.judge, args.repeat,
                                       second_judge=args.second_judge, final=args.final)
        if args.cmd == "golden":
            try:
                if args.golden_cmd == "add":
                    return local.golden_add(root, args.case, args.score, args.by,
                                            [t.strip() for t in args.tags.split(",") if t.strip()], args.run,
                                            args.input_, args.output, args.note, args.critique)
                if args.golden_cmd == "claims":
                    import os
                    from assay import claims
                    url = args.url or os.environ.get("ASSAY_URL")
                    if not url:
                        print("From which server? Set ASSAY_URL (and ASSAY_KEY), or pass --url.", file=sys.stderr)
                        return 2
                    return claims.pull(root, url, args.source, args.days, local._calib_cfg(root)["golden"])
                if args.golden_cmd == "split":
                    return local.golden_split(root, args.train, args.dev, args.seed, args.by)
                if args.golden_cmd == "stats":
                    return local.golden_stats(root)
                return local.golden_suggest(root, args.n, args.field, args.vs, args.disagree)
            except (local.SetupError, ValueError) as exc:  # a bad assay.toml, or a golden set that doesn't read
                print(exc, file=sys.stderr)
                return 2
        if args.cmd == "test":
            send = {"url": args.url, "key": args.key, "tenant": args.send_tenant} if args.upload else None
            return local.test(root, local.split_command(args.command), args.repeat, args.baseline, send,
                              args.junit, args.timeout, args.failed, args.judge)
        made = local.init(root)
        print(f"Created {', '.join(made)}." if made else f"{local.CONFIG} is already here; nothing changed.")
        try:
            import pytest  # noqa: F401
            print("Next: `pytest --assay tests/ai`. Then add your own tests next to the example.")
        except ImportError:
            print("Next: `pip install pytest`, then `pytest --assay tests/ai`.")
        return 0
    if args.cmd == "schema":
        from assay.schema import json_schema
        print(json.dumps(json_schema(), indent=1))
        return 0
    settings = Settings.from_env()

    if args.cmd == "serve":
        if args.every is not None:
            settings.schedule_minutes = args.every
        if args.sources:
            settings.schedule_sources = args.sources
        if args.window_days is not None:
            settings.schedule_window_days = args.window_days
        import uvicorn
        from assay.api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    engine = store.make_engine(settings.store_url)
    from assay import integrations

    if args.cmd == "run":
        try:
            source = runner.resolve_source(args.source, engine, settings)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        run_id = runner.run_measures(engine, source, runner.window_for_days(args.days),
                                     notify=integrations.notifier(engine, source.name, settings.public_url,
                                                                  settings.notifier()),
                                     alert_min_n=settings.alert_min_n,
                                     alert_after_runs=settings.alert_after_runs)
        out = runner.latest_run(engine, source.name)
        for mid, m in out["measures"].items():
            val = m["overall"]["value"] if m["overall"] else None
            shown = "—" if val is None else f"{val:.4g}"
            print(f"{mid:24} {m['status']:10} {shown:>10}  {m['reason'] or ''}")
        with engine.connect() as conn:
            a = store.alerts
            live = conn.execute(select(a).where((a.c.source == source.name) & (a.c.state == "open"))).all()
        print(f"run {run_id} stored · {len(live)} open alerts")
        for r in live:
            print(f"  [{r.kind}] {r.message}")
        return 0

    if args.cmd == "keys":
        from assay import auth
        if args.keys_cmd == "create":
            try:
                row, secret = auth.create_key(engine, args.tenant, args.name,
                                              [s.strip() for s in args.scopes.split(",") if s.strip()],
                                              args.expires_in_days)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
            print(f"Created key {row['id']} for tenant {row['tenant']} with scopes {', '.join(row['scopes'])}.")
            print(f"\n  {secret}\n\nStore it now: it isn't shown again. Send it as 'Authorization: Bearer <key>'.")
            print("Authentication is now required on this server." if not settings.admin_key else "")
            return 0
        if args.keys_cmd == "list":
            for r in auth.list_keys(engine, args.tenant):
                state = "revoked" if r["revoked_at"] else "active"
                print(f"{r['id']:>4}  {r['prefix']}…  {r['tenant']:12} {','.join(r['scopes']):22} {state:8} "
                      f"last used {r['last_used_at'] or 'never'}  {r['name']}")
            return 0
        if args.keys_cmd == "revoke":
            ok = auth.revoke_key(engine, args.id)
            print("Revoked." if ok else f"No active key {args.id}.")
            return 0 if ok else 1

    if args.cmd in ("backfill", "coverage"):
        try:
            source = runner.resolve_source(args.source, engine, settings)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        if args.cmd == "backfill":
            out = runner.backfill(engine, source, args.days, args.window_days,
                                  settings.alert_min_n, settings.alert_after_runs)
            print(f"{out['runs_created']} runs created, {out['skipped']} days already had one.")
            return 0
        from assay.coverage import compute
        rep = compute(source, runner.window_for_days(args.days), runner.load_rates(engine, source.name))
        for name, p in rep["records"].items():
            print(f"{name:11} {'%d rows' % p['rows'] if p['available'] else 'not provided'}")
        print()
        for m in rep["measures"]:
            print(f"{m['status']:8} {m['name']}")
            for f in m["missing"]:
                print(f"         needs {f}")
            for i in m["improve"]:
                print(f"         better with {i['field']}: {i['why']}")
        c = rep["counts"]
        print(f"\n{c['live']} live, {c['partial']} partial, {c['blocked']} blocked")
        return 0

    if args.cmd == "connect":
        from pathlib import Path
        from assay import attach
        root = Path.cwd()
        if args.what == "db":
            return attach.db(root, args.target, args.out, args.force, args.password_command, args.preset, args.profile)
        if args.what == "code":
            return attach.code(Path(args.target) if args.target else root, args.apply)
        if args.what == "verify":
            return attach.verify(root)
        if args.what == "evals":
            return attach.evals(Path(args.target) if args.target else root, args.apply)
        return attach.overview(Path(args.target) if args.target else root)

    if args.cmd == "check-source":
        if not settings.source_url:
            print("Set ASSAY_SOURCE_URL first.", file=sys.stderr)
            return 2
        from assay.sources.sql import SQLSource
        from assay.credentials import CredentialError
        try:
            report = SQLSource(settings.source_url, password_command=settings.source_password_command).check()
        except CredentialError as exc:
            print(exc, file=sys.stderr)
            return 2
        bad = 0
        for table, fields in report.items():
            for field, err in fields.items():
                print(f"{'ok ' if err is None else 'ERR'} {table}.{field}{'' if err is None else '  ' + err}")
                bad += err is not None
        if bad:
            print(f"\n{bad} field(s) failed. Fix them in your mapping file (ASSAY_SOURCE_MAPPING), "
                  "or set them to \"NULL\" if your schema doesn't record them.", file=sys.stderr)
            return 1
        print("\nAll mapped fields work.")
        return 0

    if args.cmd == "load":
        from assay.local import load_file
        try:
            by_type, bad = load_file(engine, args.file, args.tenant)
        except OSError as exc:
            print(f"Can't read {args.file}: {exc.strerror}", file=sys.stderr)
            return 2
        if bad:
            print(f"{len(bad)} bad line(s) in {args.file}; nothing loaded:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        print(f"Loaded {sum(by_type.values())} events into tenant '{args.tenant}': "
              + (", ".join(f"{v} {k}" for k, v in by_type.items()) or "none"))
        print(f"Loading the same file again changes nothing. See them with: python -m assay serve "
              f"(source events:{args.tenant})")
        return 0

    if args.cmd == "demo":
        from assay.demo import seed
        print(json.dumps(seed(engine), indent=2))
        print("Demo loaded. Start the dashboard with: python -m assay serve")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
