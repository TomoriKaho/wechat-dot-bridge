"""Owner-only, offline course reminders; no campus credentials or model client.

Period rules and Guangzhou times adapted from gdufe-campus-agent client-core
at 8ff2c0feccb9cbb6df66a0cc874f5ed14d36f023 (see UPSTREAM_AUDIT).
"""

from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from .errors import BridgeError
from .message_format import split_text

TIMEZONE = ZoneInfo("Asia/Shanghai")
MAX_IMPORT_BYTES = 4 * 1024 * 1024
DAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
CHINESE_PERIODS = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二")
PERIOD_RANGES = {
    "第一二节": (1, 2), "第三四节": (3, 4), "第五六节": (5, 6),
    "第七八节": (7, 8), "第九十节": (9, 10),
    "第十一十二节": (11, 12), "第十十一二节": (10, 12),
}
GUANGZHOU_TIMES = (
    ("08:00", "08:45"), ("08:55", "09:40"),
    ("10:00", "10:45"), ("10:55", "11:40"),
    ("14:10", "14:55"), ("15:05", "15:50"),
    ("16:10", "16:55"), ("17:05", "17:50"),
    ("18:40", "19:25"), ("19:35", "20:20"),
    ("20:30", "21:15"), ("21:25", "22:10"),
)


def parse_date(value, *, monday=False):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        result = date.fromisoformat(value)
        if monday and result.weekday() != 0:
            raise ValueError
        # Leave enough room for all supported teaching weeks.
        result + timedelta(weeks=60)
        return result
    except (ValueError, OverflowError):
        raise BridgeError("schedule_date_invalid") from None


def periods(value):
    """Same actual-section precedence and syntax as campus client-core."""
    if not isinstance(value, str):
        return []
    text = re.sub(r"\s", "", value)
    if text in PERIOD_RANGES:
        first, last = PERIOD_RANGES[text]
        return list(range(first, last + 1))
    for index, name in enumerate(CHINESE_PERIODS, 1):
        if text == f"第{name}节":
            return [index]
    text = re.sub(r"^第|[\[\]【】()（）]|节$", "", text)
    text = re.sub(r"[—–－~～至]", "-", text)
    text = re.sub(r"[，、]", ",", text)
    if not re.fullmatch(r"\d+(?:-\d+)*(?:,\d+(?:-\d+)*)*", text):
        return []
    result = set()
    for part in text.split(","):
        numbers = [int(n) for n in part.split("-")]
        if any(n < 1 or n > 12 for n in numbers):
            return []
        if len(numbers) > 2:
            result.update(numbers)
        else:
            first, last = numbers[0], numbers[-1]
            if first > last:
                return []
            result.update(range(first, last + 1))
    return sorted(result)


def _text(value, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise BridgeError("schedule_invalid")
    value = value.strip()
    if required and not value:
        raise BridgeError("schedule_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise BridgeError("schedule_invalid") from None
    return value


def normalize_schedule(payload):
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise BridgeError("schedule_invalid")
    data = payload.get("data", payload)
    weekly = data.get("weekly") if isinstance(data, dict) else None
    by_week = weekly.get("coursesByWeek") if isinstance(weekly, dict) else None
    if not isinstance(by_week, dict) or len(by_week) > 60:
        raise BridgeError("schedule_invalid")
    if not by_week and any(isinstance(data.get(key), list) and data[key] for key in ("courses", "structured")):
        # The campus parser could not assign teaching weeks. Do not silently
        # replace a working semester with an unparsed, apparently empty one.
        raise BridgeError("schedule_invalid")
    result, count = {}, 0
    for week, days in by_week.items():
        if not isinstance(week, str) or not re.fullmatch(r"[1-9]\d?", week) or int(week) > 60:
            raise BridgeError("schedule_invalid")
        if not isinstance(days, dict) or set(days) - set(DAY_NAMES):
            raise BridgeError("schedule_invalid")
        normalized_days = {}
        for day, slots in days.items():
            if not isinstance(slots, dict) or len(slots) > 128:
                raise BridgeError("schedule_invalid")
            courses, seen = [], set()
            for slot, course in slots.items():
                _text(slot, 64, required=True)
                if course is None:
                    continue
                if not isinstance(course, dict):
                    raise BridgeError("schedule_invalid")
                section = _text(course.get("section") or "", 128)
                fallback = _text(course.get("timeSlot") or slot, 128)
                actual = periods(section) or periods(fallback)
                if not actual:
                    raise BridgeError("schedule_time_invalid")
                name = _text(course.get("name") or course.get("courseName") or "", 512, required=True)
                location = _text(course.get("location") or course.get("classroom") or "", 256)
                start, end = GUANGZHOU_TIMES[actual[0] - 1][0], GUANGZHOU_TIMES[actual[-1] - 1][1]
                key = (name, location, start, end)
                if key in seen:
                    continue
                seen.add(key)
                courses.append(dict(name=name, location=location, start=start, end=end))
                count += 1
                if count > 10000:
                    raise BridgeError("schedule_invalid")
            normalized_days[str(DAY_NAMES.index(day))] = sorted(courses, key=lambda c: (c["start"], c["end"], c["name"]))
        result[week] = normalized_days
    return result


def load_schedule_file(filename):
    """Read one explicitly supplied private regular file; never discover sessions."""
    path = Path(os.path.abspath(Path(filename).expanduser()))
    fd = None
    try:
        for component in (path, *path.parents):
            if stat.S_ISLNK(component.lstat().st_mode):
                raise BridgeError("unsafe_schedule_file")
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise BridgeError("unsafe_schedule_file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise BridgeError("unsafe_schedule_file")
        with os.fdopen(fd, "rb") as source:
            fd = None
            raw = source.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            raise BridgeError("schedule_invalid")
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError("schedule_invalid") from None
    except OSError:
        raise BridgeError("schedule_import_failed") from None
    finally:
        if fd is not None:
            os.close(fd)


def day_schedule(data, week1_monday, target):
    week = (target - parse_date(week1_monday, monday=True)).days // 7 + 1
    courses = data.get(str(week), {}).get(str(target.weekday()), [])
    result = {"date": target.isoformat(), "week": week, "courses": courses, "remind_at": None}
    if courses:
        first = datetime.fromisoformat(f"{target.isoformat()}T{courses[0]['start']}:00").replace(tzinfo=TIMEZONE)
        result["remind_at"] = (first - timedelta(minutes=15)).isoformat()
    return result


def render_day(day):
    if not day["courses"]:
        return ""
    target = date.fromisoformat(day["date"])
    lines = [f"今日课表 · {day['date']} {DAY_NAMES[target.weekday()]} · 第{day['week']}周"]
    lines.extend(f"{c['start']}–{c['end']}  {c['name']}｜{c['location'] or '地点待定'}" for c in day["courses"])
    return "\n".join(lines)


class ScheduleStore:
    """Shares Store's connection, lock, owner guard and atomic transactions."""

    def _schedule_row(self, binding):
        return self.db.execute("SELECT * FROM course_schedule WHERE account=? AND peer=?",
                               (binding["id"], binding["owner"])).fetchone()

    def import_schedule(self, payload, week1_monday):
        parse_date(week1_monday, monday=True)
        data = normalize_schedule(payload)
        with self._transaction():
            binding = self._binding()
            previous = self._schedule_row(binding)
            self.db.execute("""INSERT INTO course_schedule(account,peer,week1_monday,data,enabled,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(account,peer) DO UPDATE SET week1_monday=excluded.week1_monday,
                data=excluded.data,updated_at=excluded.updated_at,last_error=NULL""",
                (binding["id"], binding["owner"], week1_monday,
                 json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                 int(previous["enabled"]) if previous else 1, self.clock()))
            # Import is offline. Never use it to erase an uncertain or accepted send.
            self.db.execute("""UPDATE course_reminders SET status='skipped',error='schedule_replaced'
                WHERE account=? AND peer=? AND status='queued'""", (binding["id"], binding["owner"]))
        return self.schedule_status()

    def set_schedule_enabled(self, enabled):
        if type(enabled) is not bool:
            raise BridgeError("invalid_enabled")
        with self._transaction():
            binding = self._binding()
            if self._schedule_row(binding) is None:
                raise BridgeError("schedule_not_imported")
            self.db.execute("UPDATE course_schedule SET enabled=?,last_error=NULL WHERE account=? AND peer=?",
                            (int(enabled), binding["id"], binding["owner"]))
            if not enabled:
                self.db.execute("""UPDATE course_reminders SET status='skipped',error='schedule_paused'
                    WHERE account=? AND peer=? AND status='queued'""", (binding["id"], binding["owner"]))
        return self.schedule_status()

    def schedule_preview(self, target_date):
        target = parse_date(target_date)
        with self.lock:
            row = self._schedule_row(self._binding())
            if row is None:
                raise BridgeError("schedule_not_imported")
            day = day_schedule(json.loads(row["data"]), row["week1_monday"], target)
            return {**day, "text": render_day(day)}

    def _next_schedule_at(self, row, now, *, include_due=False):
        data = json.loads(row["data"])
        monday = parse_date(row["week1_monday"], monday=True)
        for week in sorted(data, key=int):
            for weekday in sorted(data[week], key=int):
                target = monday + timedelta(days=(int(week) - 1) * 7 + int(weekday))
                day = day_schedule(data, row["week1_monday"], target)
                if day["remind_at"] is None:
                    continue
                due = datetime.fromisoformat(day["remind_at"]).timestamp()
                if (due > now or (include_due and due <= now < due + 60)) and not self.db.execute("""SELECT 1 FROM course_reminders
                    WHERE account=? AND peer=? AND date=?""", (row["account"], row["peer"], day["date"])).fetchone():
                    return due
        return None

    def schedule_status(self):
        with self.lock:
            binding = self.get_binding()
            row = self._schedule_row(binding) if binding else None
            result = {"imported": bool(row), "enabled": bool(row and row["enabled"]),
                      "course_count": 0, "week_count": 0, "next_reminder_at": None,
                      "send_status": {}, "last_error": None}
            if row:
                data = json.loads(row["data"])
                result["week_count"] = len(data)
                result["course_count"] = sum(len(courses) for days in data.values() for courses in days.values())
                due = self._next_schedule_at(row, self.clock()) if row["enabled"] else None
                if due is not None:
                    result["next_reminder_at"] = datetime.fromtimestamp(due, TIMEZONE).isoformat()
                scope = (binding["id"], binding["owner"])
                result["send_status"] = {r[0]: r[1] for r in self.db.execute("""SELECT status,count(*)
                    FROM course_reminders WHERE account=? AND peer=? GROUP BY status""", scope)}
                last = self.db.execute("""SELECT error FROM course_reminders WHERE account=? AND peer=?
                    ORDER BY date DESC LIMIT 1""", scope).fetchone()
                result["last_error"] = row["last_error"] or (last[0] if last else None)
            return result

    def record_schedule_error(self):
        # Independent from channel errors: a successful poll cannot hide a
        # failed timetable scheduler, and raw exception details never persist.
        with self._transaction():
            binding = self.get_binding()
            if binding:
                self.db.execute("UPDATE course_schedule SET last_error='schedule_failed' WHERE account=? AND peer=?",
                                (binding["id"], binding["owner"]))

    def schedule_tick(self, started_at):
        with self._transaction():
            binding = self.get_binding()
            row = self._schedule_row(binding) if binding else None
            if row is None or not row["enabled"]:
                return False
            self.db.execute("UPDATE course_schedule SET last_error=NULL WHERE account=? AND peer=?",
                            (binding["id"], binding["owner"]))
            now = self.clock()
            self.db.execute("""UPDATE course_reminders SET status='skipped',error='schedule_expired'
                WHERE account=? AND peer=? AND status='queued' AND expires_at<=?""",
                (binding["id"], binding["owner"], now))
            day = day_schedule(json.loads(row["data"]), row["week1_monday"], datetime.fromtimestamp(now, TIMEZONE).date())
            if day["remind_at"] is None:
                return False
            due = datetime.fromisoformat(day["remind_at"]).timestamp()
            if now < due:
                return False
            status, error = "queued", None
            if started_at > due or now >= due + 60:
                status, error = "skipped", "schedule_missed"
            elif not binding["enabled"] or binding["status"] != "ready":
                status, error = "skipped", "account_disabled" if not binding["enabled"] else "auth_required"
            elif self._latest_context(binding) is None:
                status, error = "skipped", "fresh_context_required"
            material = json.dumps([binding["id"], binding["owner"], day["date"]]).encode()
            identifier = "schedule_" + hashlib.sha256(material).hexdigest()
            inserted = self.db.execute("""INSERT OR IGNORE INTO course_reminders
                VALUES(?,?,?,?,?,?,?,?,?)""", (identifier, binding["id"], binding["owner"], day["date"],
                due, due + 60, status, error, now)).rowcount
            if not inserted or status != "queued":
                return False
            self.db.executemany("INSERT INTO course_reminder_parts(reply_id,part,text) VALUES(?,?,?)",
                                ((identifier, i, text) for i, text in enumerate(split_text(render_day(day)))))
            return True

    def _claim_schedule_send(self, binding):
        """Called under the same transaction as the ordinary sender claim."""
        now = self.clock()
        self.db.execute("""UPDATE course_reminders SET status='skipped',error='schedule_expired'
            WHERE account=? AND peer=? AND status='queued' AND expires_at<=?""",
            (binding["id"], binding["owner"], now))
        row = self.db.execute("""SELECT r.* FROM course_reminders r JOIN course_schedule s
            ON s.account=r.account AND s.peer=r.peer WHERE r.account=? AND r.peer=?
            AND s.enabled=1 AND r.status='queued' AND r.due_at<=? ORDER BY r.due_at LIMIT 1""",
            (binding["id"], binding["owner"], now)).fetchone()
        if row is None:
            return None
        context = self._latest_context(binding)
        if context is None:
            self.db.execute("UPDATE course_reminders SET status='skipped',error='fresh_context_required' WHERE client_id=?",
                            (row["client_id"],))
            return None
        part = self.db.execute("""SELECT part,text FROM course_reminder_parts
            WHERE reply_id=? AND accepted=0 ORDER BY part LIMIT 1""", (row["client_id"],)).fetchone()
        self.db.execute("UPDATE course_reminders SET status='sending',error=NULL WHERE client_id=?", (row["client_id"],))
        return {"message_id": None, "origin": "schedule", "reply_id": row["client_id"],
                "client_id": row["client_id"] + (f"_part_{part['part']}" if part["part"] else ""),
                "peer": binding["owner"], "context_token": context["context_token"], "text": part["text"]}

    def _schedule_send_row(self, binding, reply_id):
        row = self.db.execute("SELECT * FROM course_reminders WHERE client_id=? AND account=? AND peer=?",
                              (reply_id, binding["id"], binding["owner"])).fetchone()
        if row is None:
            raise BridgeError("unknown_reply")
        return row

    def _accept_schedule_part(self, reply_id):
        self.db.execute("""UPDATE course_reminder_parts SET accepted=1 WHERE reply_id=? AND part=
            (SELECT min(part) FROM course_reminder_parts WHERE reply_id=? AND accepted=0)""", (reply_id, reply_id))
        return bool(self.db.execute("SELECT 1 FROM course_reminder_parts WHERE reply_id=? AND accepted=0", (reply_id,)).fetchone())

    def _finish_schedule_send(self, reply_id, status, error):
        with self._transaction():
            binding = self._binding()
            row = self._schedule_send_row(binding, reply_id)
            if row["status"] != "sending":
                if row["status"] == status and row["error"] == error:
                    return
                raise BridgeError("reply_not_sending")
            if status == "accepted" and self._accept_schedule_part(reply_id):
                status = "queued"  # Next part only; rejected/uncertain parts never retry.
            self.db.execute("UPDATE course_reminders SET status=?,error=? WHERE client_id=?", (status, error, reply_id))
            if status == "auth_required":
                self.db.execute("UPDATE binding SET status='auth_required',last_error='auth_required' WHERE id=?", (binding["id"],))

    def recover_schedule_sends(self, binding):
        self.db.execute("""UPDATE course_reminders SET status='uncertain',error='restart_during_send'
            WHERE account=? AND peer=? AND status='sending'""", (binding["id"], binding["owner"]))
        self.db.execute("""UPDATE course_reminders SET status='skipped',error='schedule_missed'
            WHERE account=? AND peer=? AND status='queued'""", (binding["id"], binding["owner"]))

    def resolve_schedule_uncertain(self, reply_id, delivered):
        with self._transaction():
            row = self._schedule_send_row(self._binding(), reply_id)
            if row["status"] != "uncertain":
                raise BridgeError("uncertain_reply_required")
            remaining = self._accept_schedule_part(reply_id) if delivered else True
            status = "skipped" if remaining else "accepted"
            error = "schedule_expired" if remaining and delivered else "schedule_not_delivered" if not delivered else None
            self.db.execute("UPDATE course_reminders SET status=?,error=? WHERE client_id=?", (status, error, reply_id))
        return {"reply_id": reply_id, "status": status, "error": error, "delivered": None}


class ScheduleWorker:
    def __init__(self, store):
        self.store = store
        self.started_at = store.clock()

    def tick(self):
        return self.store.schedule_tick(self.started_at)

    def delay(self):
        # Bound sleeps to detect wall-clock adjustments, preserving the absolute
        # local reminder minute rather than accumulating relative timer drift.
        with self.store.lock:
            binding = self.store.get_binding()
            row = self.store._schedule_row(binding) if binding else None
            now = self.store.clock()
            due = self.store._next_schedule_at(row, now, include_due=True) if row and row["enabled"] else None
            return min(30, max(0.01, due - now)) if due is not None else 30
