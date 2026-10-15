"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)
"""
import json

from bson import json_util
import os
import re
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient,ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson.decimal128 import Decimal128

load_dotenv()  # does not override variables that are already set in the environment

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=10000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MS_MIN = 100_000_000_000      # below this it looks like seconds -> 422
MS_MAX = 4_102_444_800_000
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

# strict int: floats, strings and bools are rejected with 422; range rejects seconds-looking values
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_LIMIT = 4.5

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# --------------------------------------------------------------------------- #
# Time conversion
# --------------------------------------------------------------------------- #
def trunc_seconds(dt: datetime) -> datetime:
    """R1: drop microseconds before any calculation."""
    return dt.replace(microsecond=0)


def ensure_utc(dt: datetime) -> datetime:
    """Naive datetimes from MongoDB are UTC; aware ones are converted to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (integer division, no float)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime -> epoch ms (integer math, no float rounding). None stays None."""
    if dt is None:
        return None
    delta = ensure_utc(dt) - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def now_utc() -> datetime:
    return trunc_seconds(datetime.now(UTC))


def is_valid_date(value: str) -> bool:
    if not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Shift helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in; overnight shift + punch earlier than shift_end -> previous day."""
    ist = trunc_seconds(ensure_utc(punch_in)).astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < parse_hhmm(shift_end):
        day = day - timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str):
    """Actual start and end instants (IST-aware) of the shift that belongs to attendance date `date_str`."""
    base = datetime.fromisoformat(date_str).date()
    start = datetime.combine(base, parse_hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(base, parse_hhmm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# Business rules R2 - R5, R8
# --------------------------------------------------------------------------- #
def late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; minutes counted from shift_start (floor)."""
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    secs = int((trunc_seconds(ensure_utc(punch_in)) - start).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def overtime_minutes(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) after shift_end, counted only if >= 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    secs = int((trunc_seconds(ensure_utc(punch_out)) - end).total_seconds())
    if secs < 0:
        return 0
    mins = secs // 60
    return mins if mins >= MIN_OVERTIME_MINUTES else 0


def round_half_up(value, places: int = 2) -> float:
    """R8: half-up rounding (not banker's, not float). Goes through str() to avoid binary float artefacts."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up, using exact Decimal arithmetic."""
    secs = int((trunc_seconds(ensure_utc(punch_out)) - trunc_seconds(ensure_utc(punch_in))).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50 (None = not punched out yet -> False)."""
    return hours is not None and hours < HALF_DAY_LIMIT


# --------------------------------------------------------------------------- #
# Output shaping (Mongo document -> API JSON)
# --------------------------------------------------------------------------- #
def _ms(value):
    return to_ms(value) if isinstance(value, datetime) else value


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def record_out(doc: dict) -> dict:
    """Attendance document -> API record. Missing legacy fields get their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [
            {
                "at": to_ms(h["at"]),
                "by": h["by"],
                "reason": h["reason"],
                "changes": {
                    k: {"from": _ms(v.get("from")), "to": _ms(v.get("to"))}
                    for k, v in h["changes"].items()
                },
            }
            for h in doc.get("history", [])
        ],
    }


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    # employees
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    # attendance_logs
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v: str) -> str:
        if not is_valid_date(v):
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index on emp_code decides the winner
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "emp_code not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(ts, d, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    """Shared by GET /attendance and the explain endpoint, so both build the query the same way."""
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None and not is_valid_date(value):
            raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None

class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = ensure_utc(rec["punch_in"])
    if ts <= p_in or ts - p_in > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    hours = work_hours(p_in, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": hours,
            "overtime_minutes": overtime_minutes(ts, rec["date"], emp["shift_start"], emp["shift_end"]),
            "half_day": is_half_day(hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:  # someone else punched out between our read and our update
        raise HTTPException(409, "already punched out")
    return record_out(updated)

def _norm(v):
    return trunc_seconds(ensure_utc(v)) if isinstance(v, datetime) else v


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    if not is_valid_date(date):
        raise HTTPException(422, "date must be a valid YYYY-MM-DD date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")
    ss, se = emp["shift_start"], emp["shift_end"]

    # 1) final status and punch times
    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        new_in = _norm(new_in)
        new_out = _norm(new_out) if new_out is not None else None
        if body.punch_in is not None and attendance_date(new_in, ss, se) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # 2) recompute derived fields (R2-R5)
    if new_in is None:
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    else:
        hours = work_hours(new_in, new_out) if new_out is not None else None
        derived = {
            "late_minutes": late_minutes(new_in, date, ss, se),
            "work_hours": hours,
            "overtime_minutes": overtime_minutes(new_out, date, ss, se) if new_out is not None else 0,
            "half_day": is_half_day(hours),
        }
    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    old_vals = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }

    # 3) only fields that really changed go into history
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if _norm(old_vals[k]) != _norm(v)}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "request changes nothing")

    # 4) compare-and-set: the update only applies if the record is still as we read it
    cas = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
    }
    hist = rec.get("history")
    cas["history"] = {"$exists": False} if hist is None else {"$size": len(hist)}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        cas,
        {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)

MONTH_RE = r"^[1-9]\d{3}-(0[1-9]|1[0-2])$"
MonthQ = Annotated[str, Query(pattern=MONTH_RE)]

def month_bounds(month: str):
    y, m = int(month[:4]), int(month[5:])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def count_weekdays(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def pct_half_up(numerator, denominator) -> float:
    """R8: numerator / denominator * 100, 2 decimals half-up, exact Decimal arithmetic."""
    value = Decimal(str(numerator)) * 100 / Decimal(denominator)
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def monthly_pipeline(emp_code: str, month: str) -> list:
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
    present = {"$cond": [
        {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, weekday]},
        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
        0,
    ]}
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    working_days = count_weekdays(max(first, date.fromisoformat(emp["joined_on"])), last)
    agg = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)), None) or {}
    present_days = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct_half_up(present_days, working_days) if working_days else None,
    }
def dec(value) -> Decimal:
    return value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    _, last = month_bounds(month)
    emp_match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        emp_match["department"] = department
    present = {"$in": ["$logs.status", list(PRESENT_STATUSES)]}
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
    late = {"$ifNull": ["$logs.late_minutes", 0]}
    has_hours = {"$and": [present, {"$isNumber": "$logs.work_hours"}]}
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f"{month}-01", "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "work_hours": 1, "late_minutes": 1, "half_day": 1}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {  # one row per employee
            "_id": {"emp": "$emp_code", "dept": "$department"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [present, weekday]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "late_minutes": {"$sum": late},
            "wh_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$logs.work_hours"}, 0]}},
            "wh_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
        }},
        {"$group": {  # one row per department
            "_id": "$_id.dept",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "wh_sum": {"$sum": "$wh_sum"},
            "wh_count": {"$sum": "$wh_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        avg = None
        if r["wh_count"]:
            avg = float((dec(r["wh_sum"]) / Decimal(r["wh_count"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}

def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list:
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline = [
        {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": late},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthQ,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}
TEN_K = Decimal128("10000")
HALF = Decimal128("0.5")


def _round4(expr):
    """Half-up to 4 decimals on a Decimal128 expression, computed inside MongoDB."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, TEN_K]}, HALF]}}, TEN_K]}


def _num(v):
    return float(v.to_decimal()) if isinstance(v, Decimal128) else v


def department_trend_pipeline(department: str, frm: date, to: date) -> list:
    start = datetime(frm.year, frm.month, frm.day, tzinfo=UTC)
    end = datetime(to.year, to.month, to.day, tzinfo=UTC) + timedelta(days=1)  # $densify upper bound is exclusive
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": frm.isoformat(), "$lte": to.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$unwind": "$logs"},
        {"$group": {
            "_id": "$logs.date",
            "present": {"$sum": {"$cond": [
                {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id"}}, "present": 1, "late": 1}},
        {"$sort": {"day": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end]}}},
        {"$addFields": {
            "present": {"$ifNull": ["$present", 0]},
            "late": {"$ifNull": ["$late", 0]},
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$lookup": {
            "from": "employees", "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$addFields": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _round4({"$divide": [{"$toDecimal": "$present"}, {"$toDecimal": "$headcount"}]}),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"mavg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": "$present", "late_count": "$late", "attendance_rate": "$rate",
            "moving_avg_7d": {"$cond": [{"$eq": [{"$type": "$mavg"}, "decimal"]}, _round4("$mavg"), None]},
        }},
        {"$sort": {"date": 1}},
    ]

def parse_trend_range(from_: str, to: str):
    if not is_valid_date(from_) or not is_valid_date(to):
        raise HTTPException(422, "from and to must be valid YYYY-MM-DD dates")
    frm, end = date.fromisoformat(from_), date.fromisoformat(to)
    if end < frm:
        raise HTTPException(422, "to must not be before from")
    if (end - frm).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return frm, end

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    if not is_valid_date(from_) or not is_valid_date(to):
        raise HTTPException(422, "from and to must be valid YYYY-MM-DD dates")
    frm, end = date.fromisoformat(from_), date.fromisoformat(to)
    if end < frm:
        raise HTTPException(422, "to must not be before from")
    if (end - frm).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    rows = db.employees.aggregate(department_trend_pipeline(department, frm, end))
    items = [{
        "date": r["date"],
        "is_working_day": r["is_working_day"],
        "headcount": r["headcount"],
        "present_count": _num(r["present_count"]),
        "late_count": r["late_count"],
        "attendance_rate": _num(r["attendance_rate"]),
        "moving_avg_7d": _num(r["moving_avg_7d"]),
    } for r in rows]
    return {"department": department, "items": items}

def explain_command(cmd: dict) -> dict:
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required query parameter(s): {', '.join(missing)}")


ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        cmd = {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT),
               "projection": {"_id": 0}, "skip": (page - 1) * page_size, "limit": page_size}
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_command(cmd)}
    if endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        coll, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
    elif endpoint == "department_summary":
        _need(month=month)
        coll, pipeline = "employees", department_summary_pipeline(month, department)
    elif endpoint == "late_leaderboard":
        _need(month=month)
        coll, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
    else:
        _need(department=department, **{"from": from_, "to": to})
        frm, end = parse_trend_range(from_, to)
        coll, pipeline = "employees", department_trend_pipeline(department, frm, end)
    explained = explain_command({"aggregate": coll, "pipeline": pipeline, "cursor": {}})
    return {"endpoint": endpoint, "collection": coll, "explain": explained}

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #