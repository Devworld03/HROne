"""Run from repo root after pasting helpers into app/main.py:  python test_helpers.py"""
from datetime import datetime, timezone

from app.main import (
    IST, UTC, attendance_date, from_ms, is_half_day, is_overnight, late_minutes,
    overtime_minutes, round_half_up, to_ms, work_hours,
)


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


DAY = ("09:30", "18:30")
NIGHT = ("22:00", "06:00")

# ---- R2 late (spec examples, shift 09:30) ----
assert late_minutes(ist(2026, 7, 6, 9, 40, 0), "2026-07-06", *DAY) == 0
assert late_minutes(ist(2026, 7, 6, 9, 40, 1), "2026-07-06", *DAY) == 10
assert late_minutes(ist(2026, 7, 6, 10, 15, 59), "2026-07-06", *DAY) == 45
assert late_minutes(ist(2026, 7, 6, 9, 0, 0), "2026-07-06", *DAY) == 0          # early
assert late_minutes(ist(2026, 7, 6, 9, 40, 0, 900000), "2026-07-06", *DAY) == 0  # truncation: 09:40:00.900 -> 09:40:00
assert late_minutes(ist(2026, 7, 6, 10, 5), "2026-07-06", *DAY) == 35           # sample 2

# ---- R1 attendance date ----
assert attendance_date(ist(2026, 7, 6, 9, 28), *DAY) == "2026-07-06"
assert attendance_date(datetime(2026, 7, 5, 20, 0, tzinfo=UTC), *DAY) == "2026-07-06"   # 01:30 IST next day
assert attendance_date(ist(2026, 7, 6, 21, 55), *NIGHT) == "2026-07-06"
assert attendance_date(ist(2026, 7, 7, 3, 0), *NIGHT) == "2026-07-06"           # before shift_end -> previous day
assert attendance_date(ist(2026, 7, 7, 6, 0, 0), *NIGHT) == "2026-07-07"        # exactly shift_end -> same day
assert is_overnight(*NIGHT) and not is_overnight(*DAY)
assert late_minutes(ist(2026, 7, 6, 21, 55), "2026-07-06", *NIGHT) == 0

# ---- R3 overtime ----
assert overtime_minutes(ist(2026, 7, 6, 18, 35), "2026-07-06", *DAY) == 0       # sample 1: 5 min < 30
assert overtime_minutes(ist(2026, 7, 6, 18, 59, 59), "2026-07-06", *DAY) == 0   # 29 min 59 s
assert overtime_minutes(ist(2026, 7, 6, 19, 0, 0), "2026-07-06", *DAY) == 30
assert overtime_minutes(ist(2026, 7, 6, 19, 10), "2026-07-06", *DAY) == 40      # sample 2
assert overtime_minutes(ist(2026, 7, 6, 17, 0), "2026-07-06", *DAY) == 0        # left early
assert overtime_minutes(ist(2026, 7, 7, 6, 40), "2026-07-06", *NIGHT) == 40     # sample 7 (overnight)

# ---- R4 work hours (half-up) ----
assert work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12      # sample 1
assert work_hours(ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10)) == 9.08      # sample 2
assert work_hours(ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)) == 8.75      # sample 7
assert work_hours(ist(2026, 7, 7, 9, 30), ist(2026, 7, 7, 13, 0)) == 3.5        # sample 4
assert work_hours(ist(2026, 7, 6, 9, 0, 0), ist(2026, 7, 6, 10, 0, 18)) == 1.01 # 1.005 -> half-up (float round gives 1.0)

# ---- R5 half day ----
assert is_half_day(4.49) is True
assert is_half_day(4.5) is False
assert is_half_day(None) is False

# ---- R8 generic rounding ----
assert round_half_up(2.675, 2) == 2.68
assert round_half_up(0.12345, 4) == 0.1235

# ---- epoch ms conversion ----
x = 1783312500900
assert from_ms(x).microsecond == 0
assert to_ms(from_ms(x)) == 1783312500000
assert to_ms(datetime(2026, 7, 6, 3, 58)) == to_ms(datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc))  # naive = UTC
assert to_ms(None) is None

print("all helper tests passed")