---
name: jev-miner
description: Find messages in a chat export (Telegram JSON) that match a described target, using Jev as a yes/no classifier, then extract cases from the matches with an LLM and write a Markdown report. Use when the user has a chat export and wants to find posts about a topic, results, cases or products, or asks to run Jev over a chat. Triggers: "найди в чате", "извлеки кейсы из экспорта", "прогони через Jev", "classify chat messages".
---

# jev-miner

Pipeline for mining a chat export. Cheap binary filter (Jev) first, then context and extraction on the matches only.

```
export.json ──prepare──▶ messages.jsonl ──classify (Jev)──▶ jev_out.jsonl
                                                  │
                         candidates (gate + context) ◀┘
                                  │
                 extraction (LLM reads candidates.jsonl) ──▶ extractions.jsonl
                                  │
                                report.md  (usage totals from jev_out.jsonl)
```

Script: `scripts/mine.py` (stdlib only, Python 3.9+). Helper: `scripts/anonymize.py`. Reference: `references/jev.md`.

## Rules that apply every run

1. **External sends need approval in chat, every run.** `classify --send` sends message text to OpenRouter. Run `classify` without `--send` first (dry run), show the user the request count, the sample request and the cost estimate, and wait for a clear yes. Approval for one run does not carry over to the next.
2. **Privacy.** Message text is anonymized before sending: links, `@`-handles, emails and phone numbers are masked. Names written in plain text are not masked. Say so to the user and offer to strip known participant names if they ask. Never send the `from` field or user IDs.
3. **Never print the key.** The script takes `OPENROUTER_API_KEY` from the environment, or from the skill's own `.env` (git-ignored, mode 600) when the variable is unset. It never prints the value. Check presence by name only.
   Do not copy the skill folder into a repository or share it: the `.env` inside holds a live key.

4. **Work directory lives outside git repositories.** It holds raw message text. Default suggestion: `~/jev-miner-runs/<run-name>/`.
5. **Report cost.** After a run, show the `usage` totals (calls, input and output tokens, USD). Tell the user to cross-check with the OpenRouter activity page, because the per-response `usage.cost` is the number the script can see.

## Workflow

### 1. Prepare

```bash
python3 ~/.claude/skills/jev-miner/scripts/mine.py prepare \
  --export "<path/to/result.json>" --work "<work-dir>" --min-chars 120
```

Writes `messages.jsonl` with every message, the reply link and a `long` flag. Local only, no network.

### 2. Build the spec from the target

Turn the user's target into a spec: a small set of yes/no questions (`noul`) and optionally a `choice`, plus one gate question with a threshold. Rules for questions:

- one property per question, stated as a yes/no fact about the message;
- criteria for `true` and `false` concrete enough that two people would agree;
- instructions in English (the examples in `references/jev.md` work that way), criteria may be in Russian if the chat is in Russian;
- pick the gate question whose yes is required for a match.

Show the spec to the user, get approval, save it to `specs/<name>.json`. Start from `specs/ai-automation-results.json`.

### 3. Dry run, then approval, then classify

```bash
python3 ~/.claude/skills/jev-miner/scripts/mine.py classify \
  --work "<work-dir>" --spec "specs/<name>.json"
```

Shows the number of requests and an anonymized sample. Ask the user for approval. Only after a yes:

```bash
python3 ~/.claude/skills/jev-miner/scripts/mine.py classify \
  --work "<work-dir>" --spec "specs/<name>.json" --send --workers 8
```

Answers are appended to `jev_out.jsonl` with the spec hash. Re-running skips answered messages and retries errors. Use `--limit N` to test on a few messages first.

Check cost: `mine.py usage --work <work-dir>`.

### 4. Candidates

```bash
python3 ~/.claude/skills/jev-miner/scripts/mine.py candidates \
  --work "<work-dir>" --spec "specs/<name>.json" [--threshold 0.85]
```

Keeps messages where the gate question's `noul` probability reaches the threshold. Each candidate gets context: reply parent and two messages before and after. Output `candidates.jsonl` has raw `text` (local reading only) and `text_anon` (safe to send).

Threshold: the spec default is a starting point. Do not trust it until someone has checked a sample by hand.

### 5. Extraction

Read `candidates.jsonl` in batches of 20–40 and write one line per candidate to `extractions.jsonl`:

```json
{"id": 4328, "is_case": true, "product": "...", "category": "documents", "status": "работает",
 "author_role": "менеджер школы", "date": "2026-05-18", "what_was_built": "...",
 "result": "... (со слов автора)", "evidence_ids": [4328], "gate_probability": 0.97}
```

Rules:
- `is_case` true only when the message itself shows something the author built or put into use, plus a business context.
- `evidence_ids` must contain the message ids that support the claim. A case without evidence is treated as doubtful in the report.
- Copy results only as the author stated them. Mark claims as "со слов автора". Do not invent numbers.
- Doubtful rows: set `is_case` false and add `reason`.

If the user approves an external model (for example DeepSeek via OpenRouter) for this step, send only `text_anon` and context texts, and get a separate approval for that.

### 6. Report

```bash
python3 ~/.claude/skills/jev-miner/scripts/mine.py report \
  --work "<work-dir>" --spec "specs/<name>.json" --export-name "result.json"
```

Writes `report.md` with the sections below. Post links are built from the export's chat id (`meta.json`, written by `prepare`). They open only for members of a private chat. Public chats have no link because the export lacks the username.

## Report format (report.md)

```markdown
# Поиск: <target from spec>

- Экспорт: `<name>`
- Спецификация: `<name>` (хеш <12 символов>)
- Порог Jev: <threshold>
- Длина сообщения: больше <N> символов

## Сводка
| Показатель | Значение |   (сообщений всего, длиннее порога, ответов Jev, прошли порог, кейсов, сомнительных)

**Расходы на Jev:** вызовов, ошибок, входных и выходных токенов, стоимость в USD.

## Найденные кейсы
### 1. <product>
- Категория, Статус, Автор (роль), Дата
- Что сделано, Результат (со слов автора)
- Ссылка на пост: https://t.me/c/<chat_id>/<message_id>
- Доказательства: id сообщений
- Вероятность Jev

## Сомнительные случаи
- <id>: <reason>
```

## Files

| File | Written by | Contains |
|---|---|---|
| `messages.jsonl` | prepare | all messages, raw text (local) |
| `jev_out.jsonl` | classify | one line per Jev call, usage, error |
| `candidates.jsonl` | candidates | matches with context, raw and anonymized |
| `extractions.jsonl` | extraction step | one line per candidate |
| `report.md` | report | final report |

## Troubleshooting

- `OPENROUTER_API_KEY is not set`: export it in the shell that runs the script. Never paste it into chat.
- Many `error` lines in `jev_out.jsonl`: check the error type name, fix the cause, re-run `classify --send`. Only errors are retried.
- Changing the questions changes the spec hash, so every message is re-asked and billed again. Changing only the threshold does not.
