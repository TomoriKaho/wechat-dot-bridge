"""Synthetic semester snapshots, offline commands and the real serial sender."""

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import anyio

from wechat_dot_bridge.cli import _run_runtime, acquire_lock
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge import migrations
from wechat_dot_bridge.net_transport import reset_cancellation
from wechat_dot_bridge.operations import diagnostics, queue
from wechat_dot_bridge.schedule import (
    MAX_IMPORT_BYTES, ScheduleWorker, load_schedule_file, normalize_schedule, periods,
)
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import WeixinWorker

ROOT = Path(__file__).absolute().parents[1]
MONDAY = "2026-10-12"


def timestamp(value):
    return datetime.fromisoformat(value + "+08:00").timestamp()


def snapshot():
    morning = dict(name="合成课程甲", location="合成教室 A", section="[01-02]节", timeSlot="第一二节")
    afternoon = dict(name="合成课程乙", location="合成教室 B", section="[05-06-07]节", timeSlot="第五六节")
    return {"success": True, "studentName": "fixture-private-name", "username": "fixture-private-identity",
            "data": {"rawHtml": "fixture-private-html", "weekly": {"coursesByWeek": {
                "1": {"星期一": {"第一二节": morning, "第五六节": afternoon},
                      "星期二": {"第五六节": afternoon}, "星期日": {"第一二节": morning}},
                "2": {"星期一": {"第五六节": afternoon}},
            }}}}


class Client:
    def __init__(self, response=None, error=None):
        self.response, self.error = response or {}, error
        self.sends = []
        self.sent = threading.Event()

    def poll(self, binding):
        return {"msgs": [], "get_updates_buf": "fixture-cursor"}

    def send(self, binding, reply):
        self.sends.append(dict(reply))
        self.sent.set()
        if self.error:
            raise BridgeError(self.error)
        return self.response

    def cancel(self):
        pass


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.now = timestamp("2026-10-12T07:44:00")
        self.store = Store(self.state, clock=lambda: self.now)
        self.store.import_binding({"id": "fixture-account", "owner": "fixture-owner",
                                  "token": "fixture-token", "base_url": "https://ilinkai.weixin.qq.com"})
        self.store.set_enabled(True)
        self.store.ingest("fixture-account", [{"source_id": "fixture-input", "peer": "fixture-owner",
                          "text": "synthetic input", "created_at": self.now,
                          "context_token": "fixture-context"}], "fixture-cursor")
        self.mid = self.store.list_pending()[0]["message_id"]
        self.store.import_schedule(snapshot(), MONDAY)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()
        reset_cancellation()

    def due(self, value="2026-10-12T07:45:00"):
        worker = ScheduleWorker(self.store)
        self.now = timestamp(value)
        return worker

    def row(self):
        return self.store.db.execute("SELECT * FROM course_reminders ORDER BY date DESC LIMIT 1").fetchone()

    def test_actual_sections_and_guangzhou_times(self):
        for raw, expected in [("[02]节", [2]), ("第十二节", [12]), ("1、3节", [1, 3]),
                              ("[05-06-07]节", [5, 6, 7]), ("第十十一二节", [10, 11, 12]),
                              ("1-16周", []), ("13节", []), ("3-1节", [])]:
            with self.subTest(raw=raw):
                self.assertEqual(periods(raw), expected)
        day = self.store.schedule_preview(MONDAY)
        self.assertEqual(day["remind_at"], "2026-10-12T07:45:00+08:00")
        self.assertEqual(day["courses"][1]["end"], "16:55")
        self.assertEqual(self.store.schedule_preview("2026-10-13")["remind_at"], "2026-10-13T13:55:00+08:00")
        data = snapshot()
        data["data"]["weekly"]["coursesByWeek"]["1"]["星期一"]["第一二节"]["section"] = "[02]节"
        self.store.import_schedule(data, MONDAY)
        self.assertEqual(self.store.schedule_preview(MONDAY)["remind_at"], "2026-10-12T08:40:00+08:00")

    def test_week_selection_weekend_and_no_courses(self):
        self.assertEqual(self.store.schedule_preview("2026-10-18")["week"], 1)
        self.assertEqual(len(self.store.schedule_preview("2026-10-18")["courses"]), 1)
        self.assertEqual(self.store.schedule_preview("2026-10-19")["remind_at"], "2026-10-19T13:55:00+08:00")
        for value in ("2026-10-11", "2026-10-14", "2026-10-26"):
            with self.subTest(value=value):
                day = self.store.schedule_preview(value)
                self.assertIsNone(day["remind_at"])
                self.assertEqual(day["text"], "")
                self.now = timestamp(value + "T20:00:00")
                self.assertFalse(self.store.schedule_tick(self.now - 86400))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM course_reminders").fetchone()[0], 0)

    def test_discard_private_fields_and_diagnostics_do_not_expose_courses(self):
        persisted = self.store.db.execute("SELECT data FROM course_schedule").fetchone()[0]
        self.assertNotIn("fixture-private", persisted)
        self.assertNotIn("rawHtml", persisted)
        self.assertNotIn("section", persisted)
        worker = self.due()
        worker.tick()
        output = json.dumps([self.store.schedule_status(), diagnostics(self.store), queue(self.store)], ensure_ascii=False)
        self.assertNotIn("合成课程", output)
        self.assertNotIn("fixture-context", output)
        self.assertNotIn("fixture-owner", output)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.state / "bridge.sqlite3").st_mode & 0o777, 0o600)

    def test_invalid_import_preserves_snapshot_and_pause(self):
        before = tuple(self.store.db.execute("SELECT * FROM course_schedule").fetchone())
        bad = snapshot()
        course = bad["data"]["weekly"]["coursesByWeek"]["1"]["星期一"]["第一二节"]
        course.update(section="未知", timeSlot="未知")
        with self.assertRaisesRegex(BridgeError, "schedule_time_invalid"):
            self.store.import_schedule(bad, MONDAY)
        with self.assertRaisesRegex(BridgeError, "schedule_date_invalid"):
            self.store.import_schedule(snapshot(), "2026-10-13")
        self.assertEqual(tuple(self.store.db.execute("SELECT * FROM course_schedule").fetchone()), before)
        self.store.set_schedule_enabled(False)
        self.store.import_schedule(snapshot(), MONDAY)
        self.assertFalse(self.store.schedule_status()["enabled"])
        self.assertTrue(self.store.set_schedule_enabled(True)["enabled"])

    def test_section_fallback_duplicates_and_empty_refresh(self):
        data = snapshot()
        slots = data["data"]["weekly"]["coursesByWeek"]["1"]["星期一"]
        slots["第一二节"]["section"] = "未知"
        slots["重复行"] = dict(slots["第一二节"])
        normalized = normalize_schedule(data)
        self.assertEqual(len(normalized["1"]["0"]), 2)
        before = self.store.schedule_preview(MONDAY)
        with self.assertRaisesRegex(BridgeError, "schedule_invalid"):
            self.store.import_schedule({"weekly": {"coursesByWeek": {}}, "courses": [dict(name="未解析") ]}, MONDAY)
        self.assertEqual(self.store.schedule_preview(MONDAY), before)
        self.store.import_schedule({"weekly": {"coursesByWeek": {}}}, MONDAY)
        self.assertEqual(self.store.schedule_status()["course_count"], 0)
        self.assertFalse(self.due().tick())
        self.assertIsNone(self.store.claim_send())

    def test_deadline_crossing_between_tick_and_delay_is_not_lost(self):
        worker = ScheduleWorker(self.store)
        self.assertFalse(worker.tick())
        self.now = timestamp("2026-10-12T07:45:00") + 0.001
        self.assertEqual(worker.delay(), 0.01)
        self.assertTrue(worker.tick())

    def test_owner_scope_does_not_claim_or_resolve_other_reminders(self):
        worker = self.due()
        worker.tick()
        identifier = self.row()["client_id"]
        self.store.db.execute("UPDATE course_reminders SET account='fixture-other-account',status='uncertain'")
        self.assertIsNone(self.store.claim_send())
        with self.assertRaisesRegex(BridgeError, "unknown_reply"):
            self.store.resolve_uncertain(identifier, True)
        self.assertFalse(queue(self.store))

    def test_one_daily_reminder_uses_serial_sender_without_inbound_work(self):
        before = self.store.work_status(self.mid)
        worker = self.due()
        self.assertTrue(worker.tick())
        self.assertFalse(worker.tick())
        client = Client()
        sender = WeixinWorker(self.store, client)
        self.assertTrue(sender.send_once())
        self.assertFalse(sender.send_once())
        self.assertEqual(self.row()["status"], "accepted")
        self.assertEqual(client.sends[0]["context_token"], "fixture-context")
        self.assertEqual(client.sends[0]["peer"], "fixture-owner")
        self.assertIsNone(client.sends[0]["message_id"])
        self.assertEqual(self.store.work_status(self.mid), before)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM replies").fetchone()[0], 0)
        self.store.import_schedule(snapshot(), MONDAY)
        self.assertFalse(worker.tick())
        self.store.recover_inflight()
        self.assertFalse(worker.tick())

    def test_restart_after_due_and_runtime_missing_minute_are_skipped(self):
        self.now = timestamp("2026-10-12T07:45:01")
        self.assertFalse(ScheduleWorker(self.store).tick())
        self.assertEqual(self.row()["error"], "schedule_missed")
        self.assertIsNone(self.store.claim_send())
        self.store.db.execute("DELETE FROM course_reminders")
        worker = self.due("2026-10-12T07:46:00")
        self.assertFalse(worker.tick())
        self.assertEqual(self.row()["status"], "skipped")

    def test_queued_expiry_pause_and_restart_do_not_catch_up(self):
        worker = self.due()
        self.assertTrue(worker.tick())
        self.now += 60
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.row()["error"], "schedule_expired")
        for action, expected in [(lambda: self.store.set_schedule_enabled(False), "schedule_paused"),
                                 (self.store.recover_inflight, "schedule_missed")]:
            self.store.db.execute("DELETE FROM course_reminder_parts")
            self.store.db.execute("DELETE FROM course_reminders")
            self.store.set_schedule_enabled(True)
            self.now = timestamp("2026-10-12T07:45:00")
            self.assertTrue(self.store.schedule_tick(self.now - 1))
            action()
            self.assertEqual(self.row()["error"], expected)
            self.assertIsNone(self.store.claim_send())

    def test_missing_context_or_disabled_channel_does_not_resume_later(self):
        worker = self.due()
        self.store.db.execute("UPDATE messages SET context_token=NULL")
        self.assertFalse(worker.tick())
        self.assertEqual(self.row()["error"], "fresh_context_required")
        self.store.db.execute("UPDATE messages SET context_token='fixture-new-context'")
        self.assertFalse(worker.tick())
        self.assertIsNone(self.store.claim_send())
        self.store.db.execute("DELETE FROM course_reminders")
        self.store.set_enabled(False)
        self.assertFalse(worker.tick())
        self.assertEqual(self.row()["error"], "account_disabled")
        self.store.set_enabled(True)
        self.assertFalse(worker.tick())

    def test_uncertain_persists_across_reopen_and_manual_resolution_never_resends(self):
        worker = self.due()
        worker.tick()
        claimed = self.store.claim_send()
        identifier = claimed["reply_id"]
        self.store.close()
        self.store = Store(self.state, clock=lambda: self.now)
        self.store.recover_inflight()
        self.assertEqual(self.row()["status"], "uncertain")
        self.assertIsNone(self.store.claim_send())
        self.assertFalse(ScheduleWorker(self.store).tick())
        with self.assertRaisesRegex(BridgeError, "delivery_uncertain_review_required"):
            self.store.retry_reply(identifier)
        self.assertEqual(queue(self.store)[0]["reply_id"], identifier)
        self.assertEqual(self.store.resolve_uncertain(identifier, False)["status"], "skipped")
        self.assertIsNone(self.store.claim_send())
        with self.assertRaisesRegex(BridgeError, "schedule_retry_disabled"):
            self.store.retry_reply(identifier)

    def test_network_errors_and_context_rejections_do_not_retry(self):
        for response, error, expected in [({}, "network_timeout", "uncertain"),
                                          ({"ret": -2}, None, "failed"),
                                          ({"ret": -1, "errmsg": "context_token fixture-private"}, None, "context_required")]:
            with self.subTest(expected=expected):
                self.store.db.execute("DELETE FROM course_reminder_parts")
                self.store.db.execute("DELETE FROM course_reminders")
                worker = self.due()
                worker.tick()
                client = Client(response, error)
                sender = WeixinWorker(self.store, client)
                sender.send_once()
                self.assertEqual(self.row()["status"], expected)
                sender.send_once()
                self.assertEqual(len(client.sends), 1)
                self.assertNotIn("fixture-private", self.row()["error"] or "")

    def test_long_summary_parts_preserve_acceptance_on_interruption(self):
        data = snapshot()
        slots = data["data"]["weekly"]["coursesByWeek"]["1"]["星期一"]
        for i in range(15):
            slots[f"第{i}个合成条目"] = dict(name=f"课程{i}" + "甲" * 500, location="A", section="[01-02]节")
        self.store.import_schedule(data, MONDAY)
        worker = self.due()
        worker.tick()
        first = self.store.claim_send()
        self.store.finish_send(None, "accepted", reply_id=first["reply_id"])
        second = self.store.claim_send()
        self.assertNotEqual(first["client_id"], second["client_id"])
        self.store.recover_inflight()
        self.assertEqual(self.row()["status"], "uncertain")
        accepted = self.store.db.execute("SELECT sum(accepted) FROM course_reminder_parts").fetchone()[0]
        self.assertEqual(accepted, 1)
        result = self.store.resolve_uncertain(first["reply_id"], True)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.store.db.execute("SELECT sum(accepted) FROM course_reminder_parts").fetchone()[0], 2)
        self.assertIsNone(self.store.claim_send())

    def test_real_runtime_wakes_sender_at_absolute_due_time(self):
        reset_cancellation()
        origin = time.monotonic()
        due = timestamp("2026-10-12T07:45:00")
        self.store.clock = lambda: due - 0.15 + time.monotonic() - origin
        client = Client()
        sender = WeixinWorker(self.store, client)
        server = SimpleNamespace(wake_workers=None)
        noop = lambda: None
        typing = SimpleNamespace(prewarm=noop, tick=noop, close=noop, received=lambda ids: None)

        async def stdio(server):
            self.assertTrue(await anyio.to_thread.run_sync(client.sent.wait, 1))

        with patch("wechat_dot_bridge.sdk_adapter.run_stdio", stdio):
            origin = time.monotonic()
            anyio.run(_run_runtime, self.store, server, sender,
                      SimpleNamespace(deliver_once=noop), SimpleNamespace(tick=noop),
                      SimpleNamespace(tick=noop), typing)
        self.assertEqual(len(client.sends), 1)
        self.assertEqual(self.row()["status"], "accepted")
        self.assertIsNone(server.wake_workers)

    def test_runtime_schedule_failure_records_only_fixed_category(self):
        reset_cancellation()
        server = SimpleNamespace(wake_workers=None)
        noop = lambda: None
        typing = SimpleNamespace(prewarm=noop, tick=noop, close=noop, received=lambda ids: None)

        async def stdio(server):
            with anyio.fail_after(1):
                while self.store.schedule_status()["last_error"] != "schedule_failed":
                    await anyio.sleep(0.01)

        with patch("wechat_dot_bridge.sdk_adapter.run_stdio", stdio), patch.object(
                ScheduleWorker, "tick", side_effect=RuntimeError("fixture-private-error")):
            anyio.run(_run_runtime, self.store, server, WeixinWorker(self.store, Client()),
                      SimpleNamespace(deliver_once=noop), SimpleNamespace(tick=noop),
                      SimpleNamespace(tick=noop), typing)
        self.assertEqual(self.store.schedule_status()["last_error"], "schedule_failed")
        self.assertNotIn("fixture-private", json.dumps(diagnostics(self.store)))


class ScheduleFileAndCLITests(unittest.TestCase):
    def test_private_input_rejects_symlinks_hardlinks_and_large_or_invalid_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            path.write_text(json.dumps(snapshot()))
            path.chmod(0o600)
            self.assertEqual(load_schedule_file(path), snapshot())
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(BridgeError, "unsafe_schedule_file"):
                load_schedule_file(link)
            path.chmod(0o644)
            with self.assertRaisesRegex(BridgeError, "unsafe_schedule_file"):
                load_schedule_file(path)
            path.chmod(0o600)
            hardlink = Path(directory) / "hard.json"
            os.link(path, hardlink)
            with self.assertRaisesRegex(BridgeError, "unsafe_schedule_file"):
                load_schedule_file(path)
            hardlink.unlink()
            path.write_bytes(b"x" * (MAX_IMPORT_BYTES + 1))
            with self.assertRaisesRegex(BridgeError, "schedule_invalid"):
                load_schedule_file(path)
            path.write_bytes(b"{fixture-private-broken-json")
            with self.assertRaisesRegex(BridgeError, "schedule_invalid"):
                load_schedule_file(path)

    def test_cli_import_preview_pause_and_mutation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = Store(state)
            store.import_binding({"id": "fixture-account", "owner": "fixture-owner",
                                  "token": "fixture-token", "base_url": "https://ilinkai.weixin.qq.com"})
            store.close()
            path = Path(directory) / "schedule.json"
            path.write_text(json.dumps(snapshot()))
            path.chmod(0o600)

            def command(*arguments):
                return subprocess.run([sys.executable, "-m", "wechat_dot_bridge", "--state-dir", str(state),
                                       "schedule", *arguments], cwd=ROOT, capture_output=True, text=True, timeout=10)

            result = command("import", str(path), "--week1-monday", MONDAY)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["enabled"])
            self.assertNotIn("合成课程", result.stdout)
            preview = command("preview", "--date", MONDAY)
            self.assertIn("合成课程甲", json.loads(preview.stdout)["text"])
            self.assertFalse(json.loads(command("pause").stdout)["enabled"])
            self.assertFalse(json.loads(command("import", str(path), "--week1-monday", MONDAY).stdout)["enabled"])
            self.assertTrue(json.loads(command("resume").stdout)["enabled"])
            with acquire_lock(state / "worker.lock"):
                self.assertEqual(command("pause").returncode, 2)
                result = command("import", str(path), "--week1-monday", MONDAY)
                self.assertEqual(json.loads(result.stderr), {"error": "already_running"})
                self.assertEqual(command("status").returncode, 0)
                self.assertEqual(command("preview", "--date", MONDAY).returncode, 0)

    def test_version_eight_upgrade_failure_is_atomic(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(db.close)
        for step in migrations.MIGRATIONS[:8]:
            step(db, lambda: 1000)
        db.execute("PRAGMA user_version=8")
        original = migrations.MIGRATIONS[8]

        def interrupted(db, clock):
            original(db, clock)
            raise sqlite3.OperationalError("synthetic migration failure")

        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:8], interrupted)):
            with self.assertRaises(sqlite3.OperationalError):
                migrations.migrate(db)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
        self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='course_schedule'").fetchone())
        migrations.migrate(db)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)


if __name__ == "__main__":
    unittest.main()

