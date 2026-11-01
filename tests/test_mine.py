"""Offline tests for jev-miner. No network: Jev is replaced by a fake.

Run: python3 -m unittest discover -s ~/.claude/skills/jev-miner/tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mine  # noqa: E402

SPEC = {
    "name": "test-spec",
    "target": "test target",
    "questions": {
        "ai_business": {"type": "noul", "instructions": "x", "criteria": {"true": "a", "false": "b"}},
    },
    "gate": {"question": "ai_business", "threshold": 0.8},
}


def export_doc():
    long_a = "Я сделал бота для поддержки, он экономит 10 часов в неделю, пишите @someone или +7 999 123 45 67 " + "x" * 80
    long_b = "Вопрос: как настроить вебхук в портале? " + "y" * 120
    short = "ок"
    return {
        "name": "test chat",
        "id": 3761894596,
        "type": "private_supergroup",
        "messages": [
            {"id": 1, "type": "service", "date": "2026-03-26T10:00:00", "text": ""},
            {"id": 2, "type": "message", "date": "2026-03-27T10:00:00", "from": "Secret Name",
             "from_id": "user1", "text": short},
            {"id": 3, "type": "message", "date": "2026-03-27T11:00:00", "from": "Secret Name",
             "from_id": "user1", "text": long_a},
            {"id": 4, "type": "message", "date": "2026-03-27T12:00:00", "text": long_b,
             "reply_to_message_id": 3},
            {"id": 5, "type": "message", "date": "2026-03-28T10:00:00", "text": "см. https://example.com/page " + "z" * 130},
        ],
    }


def fake_response(prob, cost=0.00002):
    return {
        "model": "typesafe/jev-1.13-test",
        "answers": {"ai_business": {"type": "noul", "noul": prob}},
        "usage": {"input_tokens": 100, "output_tokens": 0, "cost": cost},
    }


class ChatMinerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.export = self.root / "export.json"
        self.export.write_text(json.dumps(export_doc(), ensure_ascii=False), encoding="utf-8")
        self.work = self.root / "work"
        self.spec = self.root / "spec.json"
        self.spec.write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
        self._orig_send = mine.send_to_jev
        self._orig_key = os.environ.get(mine.KEY_ENV)
        os.environ[mine.KEY_ENV] = "test-key-not-real"

    def tearDown(self):
        mine.send_to_jev = self._orig_send
        if self._orig_key is None:
            os.environ.pop(mine.KEY_ENV, None)
        else:
            os.environ[mine.KEY_ENV] = self._orig_key
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mine.main(list(argv))
        return buf.getvalue()

    def prepare(self):
        self.run_cli("prepare", "--export", str(self.export), "--work", str(self.work), "--min-chars", "120")

    def test_prepare_keeps_only_messages_and_drops_sender(self):
        self.prepare()
        rows = [json.loads(l) for l in (self.work / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["id"] for r in rows], [2, 3, 4, 5])  # service message dropped
        self.assertTrue(all("from" not in r and "from_id" not in r for r in rows))
        by_id = {r["id"]: r for r in rows}
        self.assertFalse(by_id[2]["long"])
        self.assertTrue(by_id[3]["long"])
        self.assertEqual(by_id[4]["reply_to"], 3)
        meta = json.loads((self.work / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["chat_id"], 3761894596)

    def test_dry_run_sends_nothing(self):
        self.prepare()

        def boom(_req):
            raise AssertionError("network call during dry run")

        mine.send_to_jev = boom
        out = self.run_cli("classify", "--work", str(self.work), "--spec", str(self.spec))
        self.assertIn("DRY RUN", out)
        self.assertFalse((self.work / "jev_out.jsonl").exists())

    def test_send_records_usage_and_second_run_is_cached(self):
        self.prepare()
        calls = []

        def fake(req):
            calls.append(req)
            return fake_response(0.9)

        mine.send_to_jev = fake
        self.run_cli("classify", "--work", str(self.work), "--spec", str(self.spec), "--send", "--workers", "1")
        self.assertEqual(len(calls), 3)  # only the three long messages
        for req in calls:
            self.assertNotIn("@someone", req["state"]["message"])
            self.assertNotIn("+7 999", req["state"]["message"])
            self.assertNotIn("example.com", req["state"]["message"])

        out = self.run_cli("classify", "--work", str(self.work), "--spec", str(self.spec), "--send", "--workers", "1")
        self.assertEqual(len(calls), 3, "second run must not bill again")
        self.assertIn("to send: 0", out)

    def test_usage_totals(self):
        self.prepare()
        mine.send_to_jev = lambda req: fake_response(0.9, cost=0.5)
        self.run_cli("classify", "--work", str(self.work), "--spec", str(self.spec), "--send", "--workers", "1")
        out = self.run_cli("usage", "--work", str(self.work))
        data = json.loads(out)
        self.assertEqual(data["calls"], 3)
        self.assertEqual(data["input_tokens"], 300)
        self.assertAlmostEqual(data["cost"], 1.5)

    def test_threshold_change_does_not_change_hash(self):
        h1 = mine.spec_hash(SPEC)
        changed = json.loads(json.dumps(SPEC))
        changed["gate"]["threshold"] = 0.5
        self.assertEqual(h1, mine.spec_hash(changed))
        changed["questions"]["ai_business"]["instructions"] = "different"
        self.assertNotEqual(h1, mine.spec_hash(changed))

    def test_candidates_gate_and_context(self):
        self.prepare()
        answers = {3: 0.95, 4: 0.6, 5: 0.9}
        lines = []
        for msg_id, p in answers.items():
            lines.append(json.dumps({"id": msg_id, "spec_hash": mine.spec_hash(SPEC),
                                     "answers": {"ai_business": {"type": "noul", "noul": p}},
                                     "usage": {}}, ensure_ascii=False))
        (self.work / "jev_out.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.run_cli("candidates", "--work", str(self.work), "--spec", str(self.spec))
        rows = [json.loads(l) for l in (self.work / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["id"] for r in rows], [3, 5])  # 4 is below 0.8
        row3 = rows[0]
        self.assertIn("@someone", row3["text"])  # local copy keeps raw text
        self.assertNotIn("@someone", row3["text_anon"])  # sendable copy is masked
        self.assertEqual(row3["post_link"], "https://t.me/c/3761894596/3")
        self.assertEqual([c["id"] for c in row3["context_after"]], [4, 5])

    def test_report_has_sections_and_cost(self):
        self.prepare()
        mine.send_to_jev = lambda req: fake_response(0.9, cost=0.25)
        self.run_cli("classify", "--work", str(self.work), "--spec", str(self.spec), "--send", "--workers", "1")
        (self.work / "jev_out.jsonl").write_text(
            (self.work / "jev_out.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        (self.work / "candidates.jsonl").write_text(
            json.dumps({"id": 3}) + "\n", encoding="utf-8")
        (self.work / "extractions.jsonl").write_text(
            json.dumps({"id": 3, "is_case": True, "product": "Бот поддержки", "category": "support",
                        "status": "работает", "author_role": "руководитель", "date": "2026-03-27",
                        "what_was_built": "бот", "result": "10 часов в неделю (со слов автора)",
                        "evidence_ids": [3], "gate_probability": 0.95}, ensure_ascii=False) + "\n"
            + json.dumps({"id": 4, "is_case": False, "reason": "вопрос"}, ensure_ascii=False) + "\n",
            encoding="utf-8")

        self.run_cli("report", "--work", str(self.work), "--spec", str(self.spec))
        md = (self.work / "report.md").read_text(encoding="utf-8")
        self.assertIn("# Поиск: test target", md)
        self.assertIn("## Найденные кейсы", md)
        self.assertIn("### 1. Бот поддержки", md)
        self.assertIn("Доказательства: 3", md)
        self.assertIn("- Ссылка на пост: https://t.me/c/3761894596/3", md)
        self.assertIn("## Сомнительные случаи", md)
        self.assertIn("- 4: вопрос", md)
        self.assertIn("стоимость $0.750000", md)


if __name__ == "__main__":
    unittest.main()


class SplitBySizeTest(unittest.TestCase):
    def test_splits_by_utf8_bytes_not_characters(self):
        # 'я' is 2 bytes in UTF-8: 10 chars = 20 bytes
        rows = [{"id": 1, "text": "я" * 10}, {"id": 2, "text": "x" * 25}]
        fit, big = mine.split_by_size(rows, 24)
        self.assertEqual([r["id"] for r in fit], [1])
        self.assertEqual([r["id"] for r in big], [2])

    def test_default_limit_is_the_documented_32kb(self):
        self.assertEqual(mine.MAX_BYTES_DEFAULT, 32_000)
        fit, big = mine.split_by_size([{"id": 1, "text": "a" * 32_000}, {"id": 2, "text": "a" * 32_001}], mine.MAX_BYTES_DEFAULT)
        self.assertEqual([r["id"] for r in fit], [1])
        self.assertEqual([r["id"] for r in big], [2])
