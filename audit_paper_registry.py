"""Read-only inventory of actual PAPER records; never infer missing runs.

No Google write, market reconstruction, evaluation or archive deletion. Older
prose without a stable RUN_ID remains unparsed and cannot be rotation-eligible.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path

RID = r"RADAR-\d{8}-\d{4}-Bahia"
SNAP = re.compile(r"^(?:(SNAPSHOT(?:_PAPER)?|RUN)\s*\|\s*|RUN_ID\s*:?\s*)?("+RID+r")(?:\s*\||\s*$)")
EVAL = re.compile(r"\b(EVAL(?:_PAPER)?)\s*\|\s*("+RID+r")\s*\|\s*(H\+[14])\s*\|")
ALLOWED = {"NO_TRIGGER", "TRIGGERED_HELD", "TRIGGERED_INVALIDATED", "MISSED_MOVE",
           "LATE_CHASE_AVOIDED", "DATA_FAILURE", "INSUFFICIENT_HISTORY"}

def run_time(rid):
    return datetime.strptime(rid, "RADAR-%Y%m%d-%H%M-Bahia").replace(tzinfo=timezone(timedelta(hours=-3)))

def audit(text: str, now: datetime) -> dict:
    if now.tzinfo is None:
        raise ValueError("--now requires an explicit offset")
    snapshots, evaluations, malformed, glued = [], [], [], []
    for lineno, line in enumerate(text.splitlines(), 1):
        sm, ems = SNAP.match(line), list(EVAL.finditer(line))
        if sm:
            try: run_time(sm[2])
            except ValueError:
                malformed.append({"line":lineno,"kind":"invalid_snapshot_date"}); continue
            snapshots.append({"id":sm[2],"format":sm[1] or "LEGACY_ID","line":lineno})
        for em in ems:
            try: run_time(em[2])
            except ValueError:
                malformed.append({"line":lineno,"kind":"invalid_evaluation_date"}); continue
            evaluations.append({"id":em[2],"horizon":em[3],"format":em[1],"line":lineno})
            if em.start() != 0:
                glued.append({"line":lineno,"id":em[2],"horizon":em[3],"offset":em.start()})
        if not sm and not ems and re.match(r"^(?:SNAPSHOT(?:_PAPER)?|EVAL(?:_PAPER)?|RUN_ID)\s*(?:\||:)", line):
            malformed.append({"line":lineno,"kind":"unrecognized_record"})
    sc, ec = Counter(s["id"] for s in snapshots), Counter((e["id"],e["horizon"]) for e in evaluations)
    missing = []
    for rid in sc:
        for h in ("H+1","H+4"):
            if (rid,h) not in ec:
                due = run_time(rid)+timedelta(hours=int(h[-1]))
                missing.append({"id":rid,"horizon":h,"due":due.isoformat(),
                                "mature":due<=now, "exclusion_review_required":True})
    return {
        "as_of":now.isoformat(), "source_sha256":hashlib.sha256(text.encode()).hexdigest(),
        "source_chars":len(text), "snapshot_records":len(snapshots), "snapshot_unique":len(sc),
        "snapshot_formats":dict(Counter(s["format"] for s in snapshots)),
        "evaluation_records":len(evaluations), "evaluation_unique_pairs":len(ec),
        "evaluation_formats":dict(Counter(e["format"] for e in evaluations)),
        "evaluation_horizons":dict(Counter(e["horizon"] for e in evaluations)),
        "duplicate_snapshots":[k for k,v in sc.items() if v>1],
        "duplicate_pairs":[list(k) for k,v in ec.items() if v>1],
        "orphan_evaluations":[e for e in evaluations if e["id"] not in sc],
        "malformed_records":malformed, "missing_pairs":missing,
        "glued_evaluations":glued,
        "rotation_candidates_ids_only":[rid for rid in sc if sc[rid]==1 and
            all(ec[rid,h]==1 for h in ("H+1","H+4"))],
        "rotation_authorized":False,
        "limits":["INPUT_COMPLETENESS_MUST_BE_VERIFIED_SEPARATELY", "NO_MARKET_PATH_OR_OUTCOME_VALIDATION",
                  "LEGACY_PROSE_WITHOUT_STABLE_ID_NOT_COUNTED", "EXCLUSIONS_NOT_INFERRED",
                  "CANDIDATES_ARE_NOT_DELETION_INSTRUCTIONS"],
    }

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path);p.add_argument("--now",required=True)
    p.add_argument("--output",type=Path)
    args=p.parse_args();result=audit(args.input.read_text(encoding="utf-8-sig"),datetime.fromisoformat(args.now))
    data=json.dumps(result,ensure_ascii=False,indent=2)+"\n"
    if args.output:args.output.write_text(data)
    else: print(data,end="")
    return 1 if any(result[k] for k in ("duplicate_snapshots","duplicate_pairs","orphan_evaluations","malformed_records")) else 0

if __name__=="__main__": raise SystemExit(main())
