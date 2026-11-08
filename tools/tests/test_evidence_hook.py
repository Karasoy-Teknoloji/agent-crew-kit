"""Tests for tools/kanit_kancasi.py (Stop hook that blocks hedge words in the assistant's last message).

Run: python -m unittest discover -s tools/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "kanit_kancasi.py"
LOGDIR = tempfile.TemporaryDirectory()
LOG = Path(LOGDIR.name) / "kanit-kancasi.log"


# The hook under a forced cp1252 code page: stdio wrappers and the locale encoding are cp1252 (like Windows cp1252/cp1254),
# run the way the installer does (`python -I -c ... runpy`). Claude Code still sends UTF-8.
CP1252_RUNNER = (
    "import io, locale, runpy, sys\n"
    "locale.getpreferredencoding = lambda *a, **k: 'cp1252'\n"
    "sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='cp1252', errors='replace')\n"
    "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='cp1252', errors='replace')\n"
    "sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='cp1252', errors='replace')\n"
    "sys.argv = sys.argv[1:]\n"
    "try:\n"
    "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
    "finally:\n"
    "    sys.stdout.flush(); sys.stderr.flush()\n")


def run(payload, raw=None, cp1252=False):
    stdin = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    env = {**os.environ, "KANIT_KANCASI_LOG": str(LOG)}
    cmd = [sys.executable, "-I", "-c", CP1252_RUNNER, str(SCRIPT)] if cp1252 else [sys.executable, "-I", str(SCRIPT)]
    p = subprocess.run(cmd, input=stdin.encode("utf-8"), capture_output=True, timeout=10, env=env)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def blocked(msg, **extra):
    code, out, err = run({"last_assistant_message": msg, **extra})
    assert code == 0 and err == "", (code, err)
    return json.loads(out) if out.strip() else None


class Hook(unittest.TestCase):
    def test_hedge_in_plain_text_blocks(self):
        for text in ["Bu bilgi muhtemelen yanlış.", "Büyük ihtimalle VM id.", "BÜYÜK OLASILIKLA öyle", "SANIRIM doğru",
                     "It is probably the host.", "Presumably fine.", "Galiba bu.", "Herhalde evet.", "olsa  gerek"]:
            r = blocked(text)
            self.assertEqual(r["decision"], "block", text)
            self.assertIn("doğrulanmadı", r["reason"])

    def test_reason_names_the_word(self):
        self.assertIn("'muhtemelen'", blocked("Bu muhtemelen yanlış")["reason"])

    def test_hedge_reason_orders_restate_instruction_last(self):
        reason = blocked("Bu muhtemelen yanlış")["reason"]
        self.assertIn("Then write your COMPLETE final report again in your next message", reason)
        self.assertIn("do not refer to 'the report above'", reason)
        self.assertTrue(reason.endswith("do not refer to 'the report above'."), reason)
        self.assertIn("'büyük ihtimalle'", blocked("Bu Büyük  İhtimalle yanlış")["reason"])

    def test_turkish_capital_i(self):
        self.assertEqual(blocked("HERHALDE ÖYLE")["decision"], "block")
        self.assertEqual(blocked("SANIRIM")["decision"], "block")

    def test_clean_text_passes(self):
        self.assertIsNone(blocked("Doğrulandı: sunucu 10.0.0.5, çıktı `uname`. unlikely, likelyhood, probable."))

    def test_code_and_quotes_pass(self):
        for text in ["`probably` is a word", "``muhtemelen``", "```\nprobably\n```\ndone", "~~~\nmuhtemelen\n~~~",
                     "> muhtemelen yanlış\nSon.", "Rapor dedi: \"muhtemelen yanlış\"",
                     "Raporda “büyük ihtimalle” yazıyor.", "```py\nx = 'likely'\n"]:
            self.assertIsNone(blocked(text), text)

    def test_hedge_after_code_block_still_blocks(self):
        self.assertEqual(blocked("```\nx\n```\nBu muhtemelen yanlış")["decision"], "block")
        self.assertEqual(blocked("`a` ve \"b\" ama galiba öyle")["decision"], "block")

    def test_stop_hook_active_never_blocks(self):
        self.assertIsNone(blocked("muhtemelen", stop_hook_active=True))

    def test_transcript_fallback_uses_last_text_message(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.jsonl"
            rows = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "eski muhtemelen"}]}},
                    {"type": "user", "message": {"content": "x"}},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Doğrulandı."},
                                                                  {"type": "tool_use", "name": "Bash", "input": {"command": "echo probably"}}]}}]
            t.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            code, out, _ = run({"transcript_path": str(t)})
            self.assertEqual((code, out.strip()), (0, ""))
            rows.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "Galiba öyle"}]}})
            t.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            code, out, _ = run({"transcript_path": str(t)})
            self.assertEqual(json.loads(out)["decision"], "block")

    def test_broken_input_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.jsonl"
            bad.write_text("{not json\n\x00\xff\n")
            for payload, raw in [({"transcript_path": str(bad)}, None), ({"transcript_path": str(Path(d) / "yok")}, None),
                                 ({}, None), (None, "not json"), (None, ""), ([1], None), ({"transcript_path": 5}, None)]:
                self.assertEqual(run(payload, raw), (0, "", ""), (payload, raw))
        self.assertIn("kanit_kancasi allowed after error", LOG.read_text(encoding="utf-8"))  # errors leave a log line


def bash(cmd):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}


PROMPT = {"type": "user", "message": {"content": "lisans listesini al"}}
RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "403"}]}}


class AccessClaims(unittest.TestCase):
    """The access-claim check: a missing-access claim needs a `kasa.py bul` run in the same turn (or a cite in the message)."""

    def hook(self, msg, rows, event="Stop", **extra):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.jsonl"
            t.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            payload = {"hook_event_name": event, "last_assistant_message": msg, **extra}
            payload.setdefault("agent_transcript_path" if event == "SubagentStop" else "transcript_path", str(t))
            code, out, err = run(payload)
            self.assertEqual((code, err), (0, ""))
            return json.loads(out) if out.strip() else None

    def test_claims_without_bul_block(self):
        rows = [PROMPT, bash("curl -s https://x/api/v1/bilgisayarlar"), RESULT]
        for msg in ["GET /api/v1/bilgisayarlar 403 verdi; Vault'taki tek PAT'in okuma yetkisi yok.",
                    "Token yok, bir token verir misin?", "Bu işlem için izin gerekiyor.", "Erişimim yok.",
                    "Request failed: permission denied", "The token lacks read scope.",
                    "We have no access to the server.", "Please provide a token with read scope.", "Yetkisi yok",
                    "I need a token for the license API.", "Lisans API 403 döndü, yetki ver.",
                    "Erişim aç, giriş yap.",
                    "Token yok, ekipten istemek gerekiyor.", "Yetki gerekiyor, verir misiniz?", "permission denied, need a token"]:
            r = self.hook(msg, rows)
            self.assertEqual(r["decision"], "block", msg)
            self.assertIn("kasa.py bul", r["reason"])

    def test_reason_names_the_claim(self):
        r = self.hook("Bu arada yetkimiz yok.", [PROMPT])
        self.assertIn("'yetkimiz yok'", r["reason"])

    def test_bul_run_in_this_turn_allows_the_claim(self):
        for cmd in ["python3 tools/kasa.py bul panel okuma", "kasa bul wazuh", "~/.claude/bin/kasa.py bul lisans"]:
            rows = [PROMPT, bash(cmd), RESULT, bash("curl ..."), RESULT]
            self.assertIsNone(self.hook("Okuma token'ıyla da 403 aldım, yetkimiz yok.", rows), cmd)

    def test_bul_from_an_earlier_turn_does_not_count(self):
        rows = [PROMPT, bash("kasa.py bul lisans"), RESULT, PROMPT, bash("curl ..."), RESULT]  # no timestamps: age unknown
        self.assertEqual(self.hook("403 aldım, yetkimiz yok.", rows)["decision"], "block")

    def test_tool_results_do_not_start_a_new_turn(self):
        rows = [PROMPT, bash("kasa.py bul lisans"), RESULT, bash("curl"), RESULT, bash("curl2"), RESULT]
        self.assertIsNone(self.hook("403 aldım, yetkimiz yok.", rows))

    def test_citing_the_search_in_the_message_allows_it(self):
        msg = "Aramalar: `kasa.py bul panel okuma` -> claude-okuma, claude-otomasyon-publish; ikisiyle de 403 aldım."
        self.assertIsNone(self.hook(msg, [PROMPT]))

    def test_external_status_report_is_not_a_claim(self):
        rows = [PROMPT, bash("curl https://sandbox.example/api"), RESULT]  # no bul anywhere
        for msg in ["8 istek 401 döndü (İYS sandbox), kalan 12 istek 200.", "The API returned 403 Forbidden.",
                    "HTTP 401 Unauthorized döndü; kimlik doğrulanamadı, yanıt gövdesi boş.", "Lisans API -> 403",
                    "GET /x 403 verdi, sunucu Forbidden yanıtı gönderdi."]:
            self.assertIsNone(self.hook(msg, rows), msg)

    def test_status_with_access_wording_still_blocks(self):
        self.assertEqual(self.hook("8 istek 401 döndü, erişimim yok.", [PROMPT])["decision"], "block")

    def test_session_memory(self):
        claim = "erişimim yok, token verir misin?"

        def at(ago_h, row):
            ts = (datetime.now(timezone.utc) - timedelta(hours=ago_h)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            return {**row, "timestamp": ts}

        for ago, expect_block in [(1, False), (5, True)]:
            rows = [at(ago + 0.1, PROMPT), at(ago, bash("python3 tools/kasa.py bul iys")), at(ago, RESULT),
                    at(0.2, PROMPT), at(0.1, bash("curl x")), at(0.1, RESULT)]
            r = self.hook(claim, rows)
            self.assertEqual(r is not None, expect_block, ago)
        cite = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Aradım: `kasa.py bul ahs` -> 1 kayıt."}]}}
        rows = [at(1, PROMPT), at(1, cite), at(0.2, PROMPT), at(0.1, bash("curl x")), at(0.1, RESULT)]
        self.assertIsNone(self.hook(claim, rows))  # citation in an earlier assistant message
        rows[1] = at(5, cite)
        self.assertEqual(self.hook(claim, rows)["decision"], "block")
        rows = [PROMPT, bash("curl x"), RESULT]
        self.assertEqual(self.hook(claim, rows)["decision"], "block")  # no bul anywhere

    def test_quoted_evidence_and_normal_numbers_pass(self):
        rows = [PROMPT]
        for msg in ["Log satırı: `HTTP 403 Forbidden` ama yeniden denemede 200 döndü.", "```\nHTTP/1.1 403\n```\nSonra düzeldi.",
                    "> 403 Forbidden\nGeçti.", "Rapor \"permission denied\" diyor, sorun kapandı.",
                    "403 satır işlendi, token okundu.", "Port 4030 açık, yetki ayarları tamam.", "Erişim sağlandı; token var (43 karakter).",
                    "Access granted; the token has read scope.",
                    "Hat kullanılıyorsa da değişken bir adresi izin listesinde tutmak risk olmaya devam eder. Bu hattan erişim gerekiyorsa WireGuard VPN üzerinden yapılmalı.",
                    "Güvenlik oturumu bu şifreleri koddan çıkarıp kasaya (Vault) koydu. Kodda artık şifre yok, inceleme de geçti.",
                    "Repoda token bulunmuyor, gitleaks sızıntı bulmadı."]:
            self.assertIsNone(self.hook(msg, rows), msg)

    def test_subagent_uses_its_own_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            parent, agent = Path(d) / "p.jsonl", Path(d) / "a.jsonl"
            parent.write_text(json.dumps(PROMPT) + "\n" + json.dumps(bash("kasa.py bul x")) + "\n")
            agent.write_text(json.dumps(PROMPT) + "\n" + json.dumps(bash("curl")) + "\n")
            payload = {"hook_event_name": "SubagentStop", "transcript_path": str(parent), "agent_transcript_path": str(agent),
                       "last_assistant_message": "403, yetki yok", "agent_id": "a1", "agent_type": "quick-task-hm"}
            self.assertEqual(json.loads(run(payload)[1])["decision"], "block")  # the parent's bul does not help the subagent
            agent.write_text(json.dumps(PROMPT) + "\n" + json.dumps(bash("kasa.py bul lisans")) + "\n")
            self.assertEqual(run(payload)[1].strip(), "")

    def test_claim_in_subagent_handback_report_blocks(self):
        handback = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "Sonuç: tek PAT'te okuma yetkisi yok (403)."}}]}}
        r = self.hook("Bitti.", [PROMPT, bash("curl"), RESULT, handback], event="SubagentStop")
        self.assertEqual(r["decision"], "block")

    def test_unreadable_transcript_and_active_hook_fail_open(self):
        self.assertEqual(run({"last_assistant_message": "403, yetki yok", "transcript_path": "/yok/t.jsonl"}), (0, "", ""))
        self.assertEqual(run({"last_assistant_message": "403, yetki yok"}), (0, "", ""))
        self.assertIsNone(self.hook("403 aldım", [PROMPT], stop_hook_active=True))

    def test_hedge_and_access_reasons_are_combined(self):
        r = self.hook("Muhtemelen token yok.", [PROMPT])
        self.assertIn("'muhtemelen'", r["reason"])
        self.assertIn("kasa.py bul", r["reason"])
        self.assertEqual(r["reason"].count("COMPLETE final report"), 1)

    def test_access_block_reason_asks_for_full_restate(self):
        r = self.hook("403 aldım, yetkimiz yok.", [PROMPT])
        self.assertIn("kasa.py bul", r["reason"])
        self.assertIn("Then write your COMPLETE final report again in your next message", r["reason"])
        self.assertIn("do not refer to 'the report above'", r["reason"])


def human(text):
    return {"type": "user", "origin": {"kind": "human"}, "message": {"content": text}}


def say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool(name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


OK_RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
PROPOSAL = say("Önerim: eşiği 5'e indirelim, karantina bildirimini kapatalım. Uygulayayım mı?")


class Decisions(unittest.TestCase):
    """Decision check (Stop only): a decision in the user's prompt needs a GitHub record in the turn or a `Karar kaydı:` line."""

    def hook(self, msg, rows, event="Stop", cp1252=False, **extra):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.jsonl"
            t.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            payload = {"hook_event_name": event, "last_assistant_message": msg, **extra}
            if msg is None:  # no last_assistant_message: the hook reads the final text from the transcript
                del payload["last_assistant_message"]
            payload.setdefault("agent_transcript_path" if event == "SubagentStop" else "transcript_path", str(t))
            code, out, err = run(payload, cp1252=cp1252)
            self.assertEqual((code, err), (0, ""))
            return json.loads(out) if out.strip() else None

    def test_decision_without_record_blocks(self):
        for prompt in ["Bunu cuma gününe erteleyelim.", "Tamam, şöyle yapalım: önce dev'e alalım.", "Bence ödeme tarafını öncelik yap.",
                       "Eski endpoint kalsın.", "İptal, gerek yok.", "Başla.", "Go ahead with option B."]:
            r = self.hook("Anlaşıldı, öyle yapıyorum.", [say("Durum."), human(prompt), say("Anlaşıldı, öyle yapıyorum.")])
            self.assertEqual(r["decision"], "block", prompt)
            self.assertIn("Karar kaydı: yok", r["reason"])
            self.assertIn("team decisions issue", r["reason"])

    def test_decision_recorded_with_gh_comment_allows(self):
        for cmd in ["gh api repos/example-org/ops/issues/14/comments -f body='Karar: ...'",
                    "gh api -X POST repos/example-org/product-d/issues --input body.json",
                    "gh issue comment 14 -R example-org/ops --body x", "gh issue create -R o/r -t T -b B"]:
            rows = [human("Bunu cumaya erteleyelim."), tool("Bash", command=cmd), OK_RESULT, say("Yazdım.")]
            self.assertIsNone(self.hook("Yazdım.", rows), cmd)
        rows = [human("Erteleyelim."), tool("PowerShell", command="gh api repos/o/r/issues/3/comments -f body=x"), OK_RESULT]
        self.assertIsNone(self.hook("Yazdım.", rows))

    def test_gh_read_does_not_count(self):
        for cmd in ["gh api repos/o/r/issues/14/comments", "gh api -X GET repos/o/r/issues -f state=open", "gh issue view 14"]:
            rows = [human("Erteleyelim."), tool("Bash", command=cmd), OK_RESULT]
            self.assertEqual(self.hook("Tamam.", rows)["decision"], "block", cmd)

    def test_decision_delegated_to_agent_allows(self):
        for name, inp in [("Agent", {"prompt": "Record the decision as a comment on https://github.com/example-org/ops/issues/14"}),
                          ("Agent", {"prompt": "Kararı ilgili issue'ya yorum olarak yaz: eşik 5."}),
                          ("SendMessage", {"to": "a1", "message": "Also open an issue in product-d for this decision."})]:
            rows = [human("Bunu yapalım."), tool(name, **inp), OK_RESULT]
            self.assertIsNone(self.hook("Ajana verdim.", rows), inp)
        rows = [human("Bunu yapalım."), tool("Agent", prompt="Fix the build on dev."), OK_RESULT]
        self.assertEqual(self.hook("Ajana verdim.", rows)["decision"], "block")

    def test_decision_record_line_allows(self):
        rows = [human("Bunu yapalım.")]
        for msg in ["Yaptım.\nKarar kaydı: https://github.com/example-org/ops/issues/14#issuecomment-123",
                    "Karar kaydı: ajan yazıyor", "Tamam.\nKARAR KAYDI: yok (sohbet içi tercih)", "Karar kaydi: yok (karar değil)"]:
            self.assertIsNone(self.hook(msg, rows), msg)
        for msg in ["Karar kaydı: yok", "Karar kaydı: yok ()", "Karar kaydı: https://example.com/x"]:
            self.assertEqual(self.hook(msg, rows)["decision"], "block", msg)

    def test_question_is_not_a_decision(self):
        for prompt in ["Bunu erteleyelim mi?", "Erteleyelim mi", "Öncelik ne olsun?", "İptal edebilir misin bunu",
                       "Bunu yapalım mı acaba", "Karar ne oldu?"]:
            self.assertIsNone(self.hook("Cevap.", [PROPOSAL, human(prompt)]), prompt)

    def test_question_word_without_question_mark_is_not_a_decision(self):
        for prompt in ["şimdi en öncelikli işler ne durumda", "Kararı kim verdi", "Öncelik sırası nasıl",
                       "hangi iş öncelikli", "kaç tane öncelikli iş var"]:
            self.assertIsNone(self.hook("Cevap.", [PROPOSAL, human(prompt)]), prompt)

    def test_question_word_with_decision_verb_is_a_decision(self):
        for prompt in ["önceliklerini artıralım", "ne durumda bilmiyorum, cumaya erteleyelim", "Neden bekliyoruz, başla."]:
            self.assertEqual(self.hook("Tamam.", [say("Durum."), human(prompt)])["decision"], "block", prompt)

    def test_decision_record_missing_question_allows(self):
        rows = [human("önceliklerini artıralım")]
        for msg in ["Durum özeti.\nKarar kaydı: yok (soru)", "Karar kaydi: yok (soru)", "karar KAYDI: yok (soru)",
                    unicodedata.normalize("NFD", "Karar kaydı: yok (soru)"), "KARAR KAYDİ: yok (soru)"]:
            self.assertIsNone(self.hook(msg, rows), msg)

    def test_cp1252_locale_reads_utf8_correctly(self):
        rows = [say("Öneri: şu işi öne alalım mı?"), human("Şu işin önceliğini artıralım, gönderimi ertele.")]
        # the payload message and the transcript fallback (no last_assistant_message) both carry Turkish letters
        self.assertIsNone(self.hook("Yaptım.\nKarar kaydı: yok (soru)", rows, cp1252=True))
        self.assertIsNone(self.hook(None, rows + [say("Yaptım.\nKarar kaydı: yok (soru)")], cp1252=True))
        r = self.hook("Uyguluyorum, öncelik değişti.", rows, cp1252=True)
        self.assertEqual(r["decision"], "block")
        self.assertIn("Karar kaydı", r["reason"])  # ASCII-escaped JSON keeps the letters intact
        self.assertIn("Ürün kararları", r["reason"])
        self.assertIsNone(self.hook("Cevap.", [human("şimdi en öncelikli işler ne durumda")], cp1252=True))

    def test_short_approval_after_proposal_blocks_unless_recorded(self):
        for prompt in ["tamam", "Evet.", "aynen öyle", "ok uygula"]:
            self.assertEqual(self.hook("Uyguluyorum.", [PROPOSAL, human(prompt)])["decision"], "block", prompt)
        rows = [PROPOSAL, human("tamam"), tool("Bash", command="gh issue comment 5 --body 'Karar: eşik 5'"), OK_RESULT]
        self.assertIsNone(self.hook("Uyguladım.", rows))

    def test_short_approval_without_proposal_and_plain_chat_allow(self):
        self.assertIsNone(self.hook("Rica ederim.", [say("Bitti, dev'e alındı."), human("tamam")]))
        self.assertIsNone(self.hook("Rica ederim.", [PROPOSAL, human("tamam, ama önce şu log satırını bir açıklar mısın?")]))
        self.assertIsNone(self.hook("Özet.", [human("Dünkü loglara bak ve özetle.")]))
        self.assertIsNone(self.hook("Özet.", [human("Hatayı bul: `iptal edildi` satırı neden çıkıyor")]))

    def test_stop_hook_active_allows(self):
        self.assertIsNone(self.hook("Tamam.", [human("Erteleyelim.")], stop_hook_active=True))

    def test_subagent_stop_is_not_checked(self):
        self.assertIsNone(self.hook("Bitti.", [human("Bunu yapalım.")], event="SubagentStop"))

    def test_non_human_turns_are_not_checked(self):
        notif = {"type": "user", "origin": {"kind": "task-notification"},
                 "message": {"content": "<task-notification>Agent: erteleyelim dedi</task-notification>"}}
        meta = {"type": "user", "isMeta": True, "message": {"content": "Fallback agent check: başla"}}
        cmd = {"type": "user", "message": {"content": "<command-name>/clear</command-name> iptal"}}
        summary = {"type": "user", "isCompactSummary": True, "message": {"content": "Summary: let's erteleyelim"}}
        brief = human("Görev: " + "bağlam satırı. " * 120 + "Sonra dev'e gönder.")  # a pasted brief, not a chat decision
        for row in [notif, meta, cmd, summary, brief]:
            self.assertIsNone(self.hook("Tamam.", [human("Erteleyelim."), say("x"), row]), row)

    def test_record_earlier_in_the_turn_counts_and_earlier_turn_does_not(self):
        rows = [human("Erteleyelim."), tool("Bash", command="gh issue comment 1 --body x"), OK_RESULT,
                tool("Bash", command="ls"), OK_RESULT, say("Yazdım.")]
        self.assertIsNone(self.hook("Yazdım.", rows))
        rows += [human("Şunu da iptal edelim.")]
        self.assertEqual(self.hook("Tamam.", rows)["decision"], "block")

    def test_decision_reason_combines_with_hedge(self):
        r = self.hook("Muhtemelen olur.", [human("Erteleyelim.")])
        self.assertIn("'muhtemelen'", r["reason"])
        self.assertIn("Karar kaydı", r["reason"])
        self.assertEqual(r["reason"].count("COMPLETE final report"), 1)


if __name__ == "__main__":
    unittest.main()
