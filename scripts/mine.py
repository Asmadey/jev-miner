#!/usr/bin/env python3
"""jev-miner: find messages in a chat export with Jev, add context, report.

Subcommands
  prepare     read a Telegram JSON export, keep messages, write messages.jsonl
  classify    ask Jev one binary-style request per long message (dry run unless --send)
  candidates  join Jev answers with context; keep messages that pass the spec gate
  report      write report.md from candidates, extractions.jsonl and usage totals

Work directory layout (keep it outside any git repo):
  messages.jsonl    all messages from the export (local, raw text)
  jev_out.jsonl     one line per Jev call: id, spec_hash, answers, usage, error?
  candidates.jsonl  one line per candidate, with context (raw text, local only)
  extractions.jsonl written by the LLM extraction step (one line per candidate)
  report.md         final report
"""
import argparse
import hashlib
import json
import os
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from anonymize import anonymize  # noqa: E402

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"
KEY_ENV = "OPENROUTER_API_KEY"
CONTEXT_WINDOW = 2  # messages before and after each candidate


# ---------- helpers ----------

def flat_text(message: dict) -> str:
    text = message.get("text", "")
    if isinstance(text, list):
        text = "".join(part if isinstance(part, str) else part.get("text", "") for part in text)
    return " ".join(text.split())


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path: Path, rows: list) -> None:
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_meta(work: Path) -> dict:
    path = work / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def post_link(meta: dict, msg_id) -> "str | None":
    """Telegram link to a message. Only private chats have a stable t.me/c/ form.

    Public chats need their username, which the export does not contain, so no link.
    The link opens only for members of the chat.
    """
    chat_id = meta.get("chat_id")
    if chat_id is None or not str(meta.get("chat_type", "")).startswith("private"):
        return None
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{msg_id}"


def load_spec(path: str) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("name", "questions", "gate"):
        if key not in spec:
            raise SystemExit(f"spec is missing '{key}': {path}")
    if spec["gate"]["question"] not in spec["questions"]:
        raise SystemExit("gate.question must name one of the spec's questions")
    return spec


def spec_hash(spec: dict) -> str:
    # The gate threshold is not part of the hash: changing it must not re-bill Jev.
    canonical = json.dumps(spec["questions"], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def build_request(text: str, questions: dict) -> dict:
    return {
        "model": JEV_MODEL,
        "state": {"message": anonymize(text)},
        "questions": questions,
    }


ENV_FILES = [Path(__file__).resolve().parent.parent / name for name in (".env.local", ".env")]


def load_key_from_env_file() -> None:
    """Fill OPENROUTER_API_KEY from the skill's .env.local or .env when the shell does not set it.

    Only the variable is read; nothing is printed. Both files are git-ignored.
    """
    if os.environ.get(KEY_ENV):
        return
    for path in ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(KEY_ENV + "="):
                os.environ[KEY_ENV] = line.split("=", 1)[1].strip().strip('"').strip("'")
                return


def send_to_jev(request: dict) -> dict:
    """Single HTTP call. Kept separate so tests can replace it."""
    load_key_from_env_file()
    key = os.environ[KEY_ENV]  # never printed
    req = urllib.request.Request(
        JEV_URL,
        data=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120, context=_ssl_context()) as resp:
        return json.loads(resp.read())


def _ssl_context():
    """Python.org builds on macOS often ship without CA certs; use the system bundle."""
    system_ca = "/etc/ssl/cert.pem"
    if os.path.exists(system_ca):
        return ssl.create_default_context(cafile=system_ca)
    return ssl.create_default_context()


# ---------- subcommands ----------

def cmd_prepare(args) -> None:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(args.export).read_text(encoding="utf-8"))

    rows = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        text = flat_text(m)
        rows.append({
            "id": m["id"],
            "date": m.get("date", "")[:10],
            "reply_to": m.get("reply_to_message_id"),
            "text": text,
            "long": len(text) > args.min_chars,
        })

    meta = {"chat_id": data.get("id"), "chat_type": data.get("type", ""),
            "chat_name": data.get("name", ""), "export": str(args.export)}
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    (work / "messages.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    long_count = sum(r["long"] for r in rows)
    print(f"messages: {len(rows)} | longer than {args.min_chars} chars: {long_count}")
    print(f"written: {work / 'messages.jsonl'}")


MAX_BYTES_DEFAULT = 32_000  # docs: above ~32 KB of text Jev runs in its slower long-context mode


def split_by_size(rows, max_bytes):
    """Return (rows that fit, rows that are too big). Size is UTF-8 bytes of the message text."""
    fit, big = [], []
    for r in rows:
        (big if len(r["text"].encode("utf-8")) > max_bytes else fit).append(r)
    return fit, big


def cmd_classify(args) -> None:
    work = Path(args.work)
    spec = load_spec(args.spec)
    h = spec_hash(spec)
    out_path = work / "jev_out.jsonl"

    messages = [r for r in read_jsonl(work / "messages.jsonl") if r["long"]]
    done = {r["id"] for r in read_jsonl(out_path) if r.get("spec_hash") == h and not r.get("error")}
    todo = [r for r in messages if r["id"] not in done]
    if args.ids:
        wanted = {r["id"] for r in read_jsonl(Path(args.ids))}
        todo = [r for r in todo if r["id"] in wanted]
    todo, too_big = split_by_size(todo, args.max_bytes)
    if args.allow_long:
        todo = todo + too_big  # sent anyway, with a warning below
    if args.limit:
        todo = todo[: args.limit]

    print(f"spec: {spec['name']} (hash {h}) | long messages: {len(messages)} | "
          f"already answered: {len(done)} | to send: {len(todo)}")
    if too_big:
        ids = ", ".join(str(r["id"]) for r in too_big[:20])
        if args.allow_long:
            print(f"WARNING: {len(too_big)} messages are over {args.max_bytes} bytes and will be sent "
                  f"in Jev's slower long-context mode (--allow-long): {ids}")
        else:
            print(f"SKIPPED: {len(too_big)} messages are over {args.max_bytes} bytes "
                  f"(Jev long-context mode). Use --allow-long to send them anyway: {ids}")

    if not args.send:
        if todo:
            sample = build_request(todo[0]["text"], spec["questions"])
            print("DRY RUN, nothing sent. Sample request (anonymized):")
            print(json.dumps(sample, ensure_ascii=False, indent=2)[:1200])
        print("Add --send only after the user has approved sending anonymized text to OpenRouter.")
        return

    load_key_from_env_file()
    if not os.environ.get(KEY_ENV):
        raise SystemExit(f"{KEY_ENV} is not set: export it or put it in the skill's .env")

    def job(row):
        return row["id"], send_to_jev(build_request(row["text"], spec["questions"]))

    ok = failed = 0
    with out_path.open("a", encoding="utf-8") as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(job, r): r for r in todo}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                msg_id, resp = fut.result()
                record = {
                    "id": msg_id,
                    "spec_hash": h,
                    "answers": resp.get("answers"),
                    "usage": resp.get("usage", {}),
                    "model": resp.get("model"),
                }
                ok += 1
            except Exception as exc:  # record and continue; a re-run retries only errors
                record = {"id": row["id"], "spec_hash": h, "error": type(exc).__name__}
                failed += 1
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"done: {ok} answered, {failed} failed. Run `usage` for totals.")


def cmd_candidates(args) -> None:
    work = Path(args.work)
    spec = load_spec(args.spec)
    h = spec_hash(spec)
    threshold = args.threshold if args.threshold is not None else spec["gate"]["threshold"]
    gate_q = spec["gate"]["question"]

    messages = read_jsonl(work / "messages.jsonl")
    by_id = {m["id"]: m for m in messages}
    position = {m["id"]: i for i, m in enumerate(messages)}
    meta = load_meta(work)

    answers = {}
    for r in read_jsonl(work / "jev_out.jsonl"):
        if r.get("spec_hash") == h and not r.get("error") and r.get("answers"):
            answers[r["id"]] = r["answers"]

    rows = []
    for msg_id, ans in answers.items():
        prob = (ans.get(gate_q) or {}).get("noul")
        if prob is None or prob < threshold:
            continue
        m = by_id.get(msg_id)
        if m is None:
            continue
        i = position[msg_id]
        before = messages[max(0, i - CONTEXT_WINDOW): i]
        after = messages[i + 1: i + 1 + CONTEXT_WINDOW]
        parent = by_id.get(m.get("reply_to"))
        rows.append({
            "id": msg_id,
            "date": m["date"],
            "gate_probability": prob,
            "answers": ans,
            "text": m["text"],
            "post_link": post_link(meta, msg_id),
            "text_anon": anonymize(m["text"]),
            "parent": parent and {"id": parent["id"], "text": parent["text"]},
            "context_before": [{"id": c["id"], "text": c["text"]} for c in before],
            "context_after": [{"id": c["id"], "text": c["text"]} for c in after],
        })

    rows.sort(key=lambda r: r["id"])
    (work / "candidates.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    print(f"gate: {gate_q}.noul >= {threshold} | answered: {len(answers)} | candidates: {len(rows)}")
    print(f"written: {work / 'candidates.jsonl'}")


def cmd_eval(args) -> None:
    work = Path(args.work)
    spec = load_spec(args.spec)
    h = spec_hash(spec)
    gate_q = spec["gate"]["question"]
    labels = {r["id"]: r["label"] for r in read_jsonl(work / args.labels)
              if r.get("label") in ("yes", "no")}
    scores = {}
    for r in read_jsonl(work / "jev_out.jsonl"):
        if r.get("spec_hash") == h and not r.get("error") and r.get("answers"):
            scores[r["id"]] = (r["answers"].get(gate_q) or {}).get("noul")
    pairs = [(scores[i], labels[i] == "yes") for i in labels if scores.get(i) is not None]
    missing = len(labels) - len(pairs)
    pos = sum(1 for _, y in pairs if y)
    print(f"labelled yes/no: {len(labels)} | with Jev score: {len(pairs)} | missing Jev: {missing}")
    print(f"positives: {pos} of {len(pairs)}  (small samples: treat numbers as indicative only)")
    print(f"{'thr':>5} {'TP':>4} {'FP':>4} {'FN':>4} {'precision':>10} {'recall':>8}")
    for thr in [round(0.5 + 0.05 * k, 2) for k in range(10)]:
        tp = sum(1 for sc, y in pairs if sc >= thr and y)
        fp = sum(1 for sc, y in pairs if sc >= thr and not y)
        fn = sum(1 for sc, y in pairs if sc < thr and y)
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        print(f"{thr:>5} {tp:>4} {fp:>4} {fn:>4} {prec:>10.2f} {rec:>8.2f}")


def cmd_usage(args) -> None:
    work = Path(args.work)
    tot = {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
    for r in read_jsonl(work / "jev_out.jsonl"):
        if r.get("error"):
            tot["errors"] += 1
            continue
        u = r.get("usage") or {}
        tot["calls"] += 1
        tot["input_tokens"] += u.get("input_tokens", 0)
        tot["output_tokens"] += u.get("output_tokens", 0)
        tot["cost"] += u.get("cost", 0.0)
    print(json.dumps(tot, ensure_ascii=False, indent=2))
    return tot


def post_link_line(meta: dict, evidence_ids: list) -> str:
    links = [post_link(meta, i) for i in evidence_ids]
    links = [l for l in links if l]
    if not links:
        return "нет (публичный чат без username в экспорте)"
    extra = f" (ещё: {', '.join(links[1:])})" if len(links) > 1 else ""
    return links[0] + extra


def cmd_report(args) -> None:
    work = Path(args.work)
    spec = load_spec(args.spec)
    h = spec_hash(spec)
    tot = cmd_usage(argparse.Namespace(work=args.work))

    messages = read_jsonl(work / "messages.jsonl")
    long_count = sum(1 for m in messages if m["long"])
    candidates = read_jsonl(work / "candidates.jsonl")
    extractions = read_jsonl(work / "extractions.jsonl")
    answered = {r["id"] for r in read_jsonl(work / "jev_out.jsonl")
                if r.get("spec_hash") == h and not r.get("error")}

    meta = load_meta(work)
    cases = [e for e in extractions if e.get("is_case") and e.get("evidence_ids")]
    doubtful = [e for e in extractions if not (e.get("is_case") and e.get("evidence_ids"))]

    lines = [
        f"# Поиск: {spec.get('target', spec['name'])}",
        "",
        f"- Экспорт: `{args.export_name}`",
        f"- Спецификация: `{spec['name']}` (хеш {h})",
        f"- Порог Jev: {spec['gate']['threshold']}",
        f"- Длина сообщения: больше {args.min_chars} символов",
        "",
        "## Сводка",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Сообщений всего | {len(messages)} |",
        f"| Длиннее порога | {long_count} |",
        f"| Ответов Jev | {len(answered)} |",
        f"| Прошли порог Jev | {len(candidates)} |",
        f"| Извлечено кейсов | {len(cases)} |",
        f"| Сомнительных | {len(doubtful)} |",
        "",
        f"**Расходы на Jev:** вызовов {tot['calls']}, ошибок {tot['errors']}, "
        f"входных токенов {tot['input_tokens']}, выходных {tot['output_tokens']}, "
        f"стоимость ${tot['cost']:.6f}.",
        "",
        "## Найденные кейсы",
        "",
    ]
    for n, e in enumerate(cases, 1):
        lines += [
            f"### {n}. {e.get('product', 'без названия')}",
            f"- Категория: {e.get('category', '—')}",
            f"- Статус: {e.get('status', '—')}",
            f"- Автор: {e.get('author_role', '—')}",
            f"- Дата: {e.get('date', '—')}",
            f"- Что сделано: {e.get('what_was_built', '—')}",
            f"- Результат: {e.get('result', '—')}",
            f"- Ссылка на пост: {post_link_line(meta, e.get('evidence_ids', []))}",
            f"- Доказательства: {', '.join(str(i) for i in e.get('evidence_ids', []))}",
            f"- Вероятность Jev: {e.get('gate_probability', '—')}",
            "",
        ]
    lines += ["## Сомнительные случаи", ""]
    for e in doubtful:
        link = post_link(meta, e.get("id"))
        lines.append(f"- {e.get('id')}: {e.get('reason', 'нет доказательства или решения')}"
                     + (f" ({link})" if link else ""))
    if not doubtful:
        lines.append("- нет")

    out = Path(args.out) if args.out else work / "report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {out}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="mine.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("prepare")
    s.add_argument("--export", required=True)
    s.add_argument("--work", required=True)
    s.add_argument("--min-chars", type=int, default=120)
    s.set_defaults(fn=cmd_prepare)

    s = sub.add_parser("classify")
    s.add_argument("--work", required=True)
    s.add_argument("--spec", required=True)
    s.add_argument("--send", action="store_true", help="really call Jev (external send)")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--limit", type=int, default=0, help="send at most N requests (0 = all)")
    s.add_argument("--ids", default=None, help="jsonl file; only messages whose id it lists are sent")
    s.add_argument("--max-bytes", type=int, default=MAX_BYTES_DEFAULT,
                   help="skip messages whose text is larger than this many UTF-8 bytes (default 32000)")
    s.add_argument("--allow-long", action="store_true",
                   help="send messages above --max-bytes anyway (Jev long-context mode: slower, less accurate)")
    s.set_defaults(fn=cmd_classify)

    s = sub.add_parser("candidates")
    s.add_argument("--work", required=True)
    s.add_argument("--spec", required=True)
    s.add_argument("--threshold", type=float, default=None)
    s.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("eval")
    s.add_argument("--work", required=True)
    s.add_argument("--spec", required=True)
    s.add_argument("--labels", default="labels.jsonl")
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("usage")
    s.add_argument("--work", required=True)
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("report")
    s.add_argument("--work", required=True)
    s.add_argument("--spec", required=True)
    s.add_argument("--export-name", default="export.json")
    s.add_argument("--min-chars", type=int, default=120)
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
