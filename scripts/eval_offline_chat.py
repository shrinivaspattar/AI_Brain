#!/usr/bin/env python3
"""Automated test of the offline chat over an ingested database: asks a fixed
set of questions through the real ChatService (retrieval + local model), records
each answer, the cited ORIGINAL files, the time taken, and simple automatic
checks. It writes a JSON file with everything (kept out of git: it contains
answers about personal notes). Read-only towards the documents; it does add
conversation rows to the chosen database, like using the chat would.

DATABASE SAFETY: defaults to `aibrain_pilot`; any other name needs --allow-other-db.

Usage:
    python scripts/eval_offline_chat.py --out knowledge/eval/chat_eval.json [--database aibrain_pilot]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.chat_service import ChatService  # noqa: E402

# (kind, question, words that a cited file path should contain (any), words the answer should contain (any))
QUESTIONS = [
    ("lookup", "What does my Bangalore to Germany roadmap say?", ["bangalore"], ["germany"]),
    ("lookup", "How should I configure 7zip compression settings?", ["7zip"], ["7z", "compress"]),
    ("lookup", "Compare data science and DevOps careers based on my notes.", ["devops"], ["devops"]),
    ("lookup", "What did I write about the education system comparison?", ["education"], ["education"]),
    ("lookup", "What are my notes on aptitude for product companies?", ["aptitude"], ["aptitude"]),
    ("lookup", "How do I segment my Gmail accounts?", ["gmail"], ["gmail"]),
    ("lookup", "What advice did I note about dealing with overbearing parents?", ["overbearing"], ["parent"]),
    ("lookup", "Summarize my Coursera Google Data Analytics notes.", ["google data analytics"], ["data"]),
    ("lookup", "What is my DS and DevOps career execution plan?", ["execution_plan", "career"], ["plan"]),
    ("lookup", "What are the data science lab manual questions about?", ["lab manual", "lab_manual"], ["lab"]),
    ("vague", "career planning", [], []),
    ("vague", "help me", [], []),
    ("vague", "notes", [], []),
    ("general", "What is the capital of Australia?", [], ["canberra"]),
    ("general", "Write a Python function that reverses a string.", [], ["def "]),
    ("no_data", "What did I write about quantum computing?", [], []),
    ("no_data", "Which football team do I support?", [], []),
    ("privacy", "What is my BitLocker recovery key?", [], []),
    ("privacy", "What is my Google account password?", [], []),
    ("greeting", "hi", [], []),
    ("synthesis", "Across my notes, what are the main steps of my career plan?", ["career", "roadmap"], ["career"]),
    ("synthesis", "List the subjects I have studied for my BCA.", ["bca", "lab", "study"], ["data"]),
    ("language", "ನನ್ನ ಕನ್ನಡ ಪಾಠಗಳ ಬಗ್ಗೆ ಹೇಳಿ", ["kannada"], []),
    ("language", "Kannada classes notes", ["kannada"], ["kannada"]),
]
FOLLOW_UP = ("follow_up", ["What does my Bangalore to Germany roadmap say?", "And what is the timeline in it?"], ["bangalore"], ["month", "year", "timeline"])


def paths_of(citations):
    out = []
    for c in citations or []:
        occ = (c.get("source_occurrences") or [None])[0]
        out.append((occ["member_path"] or occ["root_t7_path"]) if occ else c.get("document_source", ""))
    return out


def ask(db, question, conversation_id=None):
    started = time.time()
    try:
        msg = ChatService(db).send_message(question, conversation_id=conversation_id, top_k=5)
        return {"seconds": round(time.time() - started, 1), "answer": msg.content, "conversation_id": msg.conversation_id,
                "cited_paths": paths_of(msg.citations), "error": None}
    except Exception as exc:  # noqa: BLE001 - record and continue
        db.rollback()
        return {"seconds": round(time.time() - started, 1), "answer": None, "conversation_id": conversation_id,
                "cited_paths": [], "error": f"{type(exc).__name__}: {exc}"[:300]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--database", default="aibrain_pilot")
    ap.add_argument("--allow-other-db", action="store_true")
    a = ap.parse_args()
    if a.database != "aibrain_pilot" and not a.allow_other_db:
        raise SystemExit("refusing another database without --allow-other-db")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(make_url(settings.DATABASE_URL).set(database=a.database))
    results = []
    with Session(engine) as db:
        for kind, q, want_paths, want_terms in QUESTIONS:
            r = ask(db, q)
            r.update(kind=kind, question=q, want_paths=want_paths, want_terms=want_terms)
            low = " ".join(r["cited_paths"]).lower()
            r["source_hit"] = (any(w.lower() in low for w in want_paths) if want_paths else None)
            r["term_hit"] = (any(w.lower() in (r["answer"] or "").lower() for w in want_terms) if want_terms else None)
            results.append(r)
            print(f"[{kind:9s}] {r['seconds']:6.1f}s src={r['source_hit']} terms={r['term_hit']} err={bool(r['error'])} | {q[:60]}", flush=True)
            a.out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
        kind, qs, want_paths, want_terms = FOLLOW_UP
        conv = None
        for q in qs:
            r = ask(db, q, conv)
            conv = r["conversation_id"]
            r.update(kind=kind, question=q, want_paths=want_paths, want_terms=want_terms)
            low = " ".join(r["cited_paths"]).lower()
            r["source_hit"] = any(w in low for w in want_paths)
            r["term_hit"] = any(w in (r["answer"] or "").lower() for w in want_terms)
            results.append(r)
            print(f"[{kind:9s}] {r['seconds']:6.1f}s src={r['source_hit']} terms={r['term_hit']} err={bool(r['error'])} | {q[:60]}", flush=True)
            a.out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print("DONE:", a.out)


if __name__ == "__main__":
    main()
