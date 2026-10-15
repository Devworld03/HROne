# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo, synchronous). All application code is in `app/main.py`.

## Run
```
pip install -r requirements.txt
set MONGO_URI=mongodb+srv://...      (or put MONGO_URI and MONGO_DB in a local .env; real environment variables win)
set MONGO_DB=attendance_db
uvicorn app.main:app --port 8000
```
Python 3.11+ and MongoDB 6.0+ (developed against Atlas, MongoDB 8.0). Indexes are created at startup and the call is idempotent.

## Status
Every endpoint in `openapi.yaml` is implemented, including the explain endpoint. Nothing is missing.

## Notes
- Endpoints are plain `def` functions, so FastAPI runs them in its thread pool. Race conditions are handled in the database: unique indexes (create employee, punch-in), an atomic conditional update (punch-out), and a compare-and-set update for PATCH, where the losing request gets 409.
- Department summary: the sum of `work_hours` is computed as `Decimal128` and divided in Python with half-up rounding, to avoid float errors such as 8.315 becoming 8.31.
- Department trend: `moving_avg_7d` is the mean of the reported (4-decimal) rates in the window.
- Verified with my own hand-calculated dataset (ties, employees with no logs, weekend records, a mid-month joiner, an overnight shift, a missing weekday, an orphan log), a parallel-request check, and about 100,000 generated attendance records for the explain plans (IXSCAN only).