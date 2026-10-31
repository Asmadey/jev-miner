#!/usr/bin/env python3
"""Build a labelling page for chat-miner evaluation, served through lavish-axi.

  make  sample long messages (stratified by a local keyword pre-filter) and write labelling.html
  read  turn the answers submitted from the page into labels.jsonl

The sample stays on this machine: the page is served by lavish-axi on loopback and nothing
is sent to a model. Each message is answered Yes / No / Not sure. Only Yes and No are used
for evaluation; "Not sure" is kept and reported.
"""
import argparse
import html
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mine import read_jsonl, load_spec  # noqa: E402

# Local pre-filter for stratification only. It never decides labels.
TOPIC = re.compile(
    r"ИИ|нейро|GPT|ГПТ|Claude|Клод|Gemini|Jev|бот[ауеы]?\b|агент|автоматиз|скрипт|интеграц|"
    r"дашборд|CRM|сделк|робот|отчет|отчёт|BitrixGPT|API|MCP",
    re.IGNORECASE,
)

QUESTION = ("Описывает ли сообщение ИИ или автоматизацию, которую автор сам создал "
            "или внедрил в бизнес-процесс?")


def cmd_make(args) -> None:
    work = Path(args.work)
    spec = load_spec(args.spec)
    messages = [m for m in read_jsonl(work / "messages.jsonl") if m["long"]]
    rng = random.Random(args.seed)

    pilot = [r["id"] for r in read_jsonl(work / "jev_out.jsonl") if not r.get("error")]
    by_id = {m["id"]: m for m in messages}
    chosen = {}
    for mid in pilot:
        if mid in by_id:
            chosen[mid] = "pilot"

    hits = [m for m in messages if TOPIC.search(m["text"]) and m["id"] not in chosen]
    rest = [m for m in messages if not TOPIC.search(m["text"]) and m["id"] not in chosen]
    n_hit = min(args.hits, len(hits))
    n_rest = min(args.rest, len(rest))
    for m in rng.sample(hits, n_hit):
        chosen[m["id"]] = "keyword"
    for m in rng.sample(rest, n_rest):
        chosen[m["id"]] = "other"

    sample = [by_id[mid] | {"stratum": s} for mid, s in chosen.items()]
    sample.sort(key=lambda m: m["id"])
    (work / "label_sample.jsonl").write_text(
        "".join(json.dumps({"id": m["id"], "date": m["date"], "stratum": m["stratum"],
                            "text": m["text"]}, ensure_ascii=False) + "\n" for m in sample),
        encoding="utf-8")

    html_path = work / "labelling.html"
    html_path.write_text(render(sample, spec), encoding="utf-8")
    counts = {}
    for m in sample:
        counts[m["stratum"]] = counts.get(m["stratum"], 0) + 1
    print(f"sample: {len(sample)} {counts} | population: keyword {len(hits)}, other {len(rest)}")
    print(f"written: {html_path}")


def cmd_band(args) -> None:
    """Label a random sample of messages whose gate score falls inside a band.

    Excludes messages already labelled, so each labelling round adds new examples.
    """
    work = Path(args.work)
    spec = load_spec(args.spec)
    h = spec_hash_of(spec)
    gate_q = spec["gate"]["question"]
    by_id = {m["id"]: m for m in read_jsonl(work / "messages.jsonl") if m["long"]}
    scores = {}
    for r in read_jsonl(work / "jev_out.jsonl"):
        if r.get("spec_hash") == h and not r.get("error") and r.get("answers"):
            scores[r["id"]] = (r["answers"].get(gate_q) or {}).get("noul")
    labelled = {r["id"] for r in read_jsonl(work / "labels.jsonl")}
    pool = [mid for mid, sc in scores.items()
            if sc is not None and args.band_min <= sc < args.band_max
            and mid in by_id and mid not in labelled]
    rng = random.Random(args.seed)
    chosen = rng.sample(sorted(pool), min(args.n, len(pool)))
    sample = [by_id[mid] | {"stratum": "band"} for mid in sorted(chosen)]
    for m in sample:
        m["score"] = round(scores[m["id"]], 3)
    (work / args.out_name.replace(".html", ".sample.jsonl")).write_text(
        "".join(json.dumps({"id": m["id"], "date": m["date"], "stratum": m["stratum"],
                            "score": m["score"], "text": m["text"]}, ensure_ascii=False) + "\n"
                for m in sample), encoding="utf-8")
    (work / args.out_name).write_text(render(sample, spec), encoding="utf-8")
    print(f"band [{args.band_min}, {args.band_max}): pool {len(pool)} | sample {len(sample)}")
    print(f"written: {work / args.out_name}")


def spec_hash_of(spec):
    from mine import spec_hash
    return spec_hash(spec)


def render(sample, spec) -> str:
    cards = []
    for m in sample:
        mid = m["id"]
        text = html.escape(m["text"])
        cards.append(f"""
<article class="card" data-id="{mid}">
  <div class="meta">#{mid} · {html.escape(m['date'])}</div>
  <p class="text">{text}</p>
  <div class="choices" role="radiogroup" aria-label="Ответ для сообщения {mid}">
    <label><input type="radio" name="m{mid}" value="yes"> Да</label>
    <label><input type="radio" name="m{mid}" value="no"> Нет</label>
    <label><input type="radio" name="m{mid}" value="unsure"> Не уверен</label>
  </div>
</article>""")
    total = len(sample)
    return f"""<!doctype html>
<html lang="ru" data-lavish-annotate="off">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Разметка: {html.escape(spec['name'])}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#111; --muted:#666; --bg:#fafafa; --card:#fff; --line:#e5e5e5; --accent:#2563eb; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg:#eee; --muted:#9a9a9a; --bg:#111; --card:#1b1b1b; --line:#2a2a2a; --accent:#60a5fa; }} }}
  body {{ margin:0; font:15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }}
  header {{ position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:12px 16px; z-index:1; }}
  h1 {{ font-size:16px; margin:0 0 4px; }}
  .question {{ margin:0 0 8px; color:var(--muted); }}
  .bar {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
  .progress {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
  button {{ font:inherit; background:var(--accent); color:#fff; border:0; border-radius:8px; padding:8px 14px; cursor:pointer; }}
  button:disabled {{ opacity:.5; cursor:default; }}
  main {{ max-width:820px; margin:16px auto; padding:0 16px 48px; display:grid; gap:12px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; }}
  .meta {{ font-size:12px; color:var(--muted); margin-bottom:6px; }}
  .text {{ margin:0 0 10px; white-space:pre-wrap; word-break:break-word; }}
  .choices {{ display:flex; gap:14px; flex-wrap:wrap; }}
  .choices label {{ display:inline-flex; gap:6px; align-items:center; cursor:pointer; }}
  .done {{ border-color:var(--accent); }}
</style>
</head>
<body>
<form id="labels" data-lavish-question="gate-labels">
<header>
  <h1>Разметка {total} сообщений</h1>
  <p class="question">{html.escape(QUESTION)}</p>
  <p class="question">Общие восклицания без конкретики о том, что именно сделано, — «Нет».</p>
  <div class="bar">
    <span class="progress" id="progress">Отмечено: 0 из {total}</span>
    <button type="submit" id="send" disabled>Отправить разметку</button>
  </div>
</header>
<main>
{''.join(cards)}
</main>
</form>
<script>
(() => {{
  const form = document.getElementById('labels');
  const total = {total};
  const progress = document.getElementById('progress');
  const send = document.getElementById('send');
  const refresh = () => {{
    let answered = 0;
    document.querySelectorAll('.card').forEach(card => {{
      const picked = card.querySelector('input[type=radio]:checked');
      card.classList.toggle('done', !!picked);
      if (picked) answered++;
    }});
    progress.textContent = 'Отмечено: ' + answered + ' из ' + total;
    send.disabled = answered === 0;
  }};
  form.addEventListener('change', refresh);
  form.addEventListener('submit', (event) => {{
    event.preventDefault();
    const items = [...document.querySelectorAll('.card')].map(card => {{
      const id = Number(card.dataset.id);
      const picked = card.querySelector('input[type=radio]:checked');
      return {{ id, answer: picked ? picked.value : null }};
    }});
    const labelled = items.filter(i => i.answer !== null);
    if (!labelled.length) return;
    if (window.lavish && window.lavish.queuePrompt) {{
      window.lavish.queuePrompt('Labels for the chat-miner evaluation are attached in data.items.', {{
        tag: 'labels', text: 'Отправлена разметка: ' + labelled.length + ' сообщений',
        element: form, data: {{ question: {json.dumps(QUESTION, ensure_ascii=False)}, items: labelled }}
      }});
      send.disabled = true;
      send.textContent = 'Отправлено';
    }}
  }});
  refresh();
}})();
</script>
</body>
</html>
"""


def cmd_read(args) -> None:
    print("read: not implemented until a real submission has been inspected", file=sys.stderr)
    raise SystemExit(2)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="label.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--work", required=True)
    m.add_argument("--spec", required=True)
    m.add_argument("--hits", type=int, default=40, help="keyword-stratum sample size")
    m.add_argument("--rest", type=int, default=40, help="other-stratum sample size")
    m.add_argument("--seed", type=int, default=7)
    m.set_defaults(fn=cmd_make)
    b = sub.add_parser("band")
    b.add_argument("--work", required=True)
    b.add_argument("--spec", required=True)
    b.add_argument("--band-min", type=float, default=0.3)
    b.add_argument("--band-max", type=float, default=0.9)
    b.add_argument("--n", type=int, default=150)
    b.add_argument("--seed", type=int, default=11)
    b.add_argument("--out-name", default="labelling-band.html")
    b.set_defaults(fn=cmd_band)
    r = sub.add_parser("read")
    r.add_argument("--work", required=True)
    r.set_defaults(fn=cmd_read)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
