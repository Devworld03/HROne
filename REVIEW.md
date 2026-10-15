# REVIEW.md

## How I reviewed the starter

I started the starter app against the sample data and tried every endpoint from Swagger UI. Then I read `app/main.py` line by line and compared it with `openapi.yaml` (rules R1 to R10) and `DATA_MODEL.md`. Below is everything I found. For each point I say where it is, what is wrong, how you would notice it, and how I fixed it.

---

## Problems in the helper functions

### 1. `compute_late_minutes` gives the wrong answer around the grace limit
- **Where:** `compute_late_minutes`
- **What is wrong:** It first rounds the minutes down and only then checks "more than 10". So a punch-in at 09:40:01 (shift 09:30) becomes 10 minutes, and 10 is not more than 10, so it returns 0. The rule (R2) says the answer should be 10. It also never cuts off the seconds, it builds the shift start from the punch-in's own date instead of the IST attendance date, and it ignores overnight shifts.
- **How you would notice:** Shift 09:30, punch-in 09:40:01 IST returns 0 instead of 10. With a 22:00 night shift the numbers make no sense.
- **How I fixed it:** I wrote a new `late_minutes(punch_in, date, shift_start, shift_end)`. It cuts the seconds, builds the shift start for the record's IST date, and says "late" only when the gap is more than 600 seconds. Then it returns the whole minutes since the shift start.

### 2. `compute_work_hours` rounds the wrong way
- **Where:** `compute_work_hours`
- **What is wrong:** It uses Python's `round()` on a float. That is not "half-up" rounding, which R4 asks for. It also keeps the seconds fraction.
- **How you would notice:** 1 hour 0 minutes 18 seconds is exactly 1.005 hours. The starter returns 1.0, but the rule says 1.01.
- **How I fixed it:** I use `Decimal` with `ROUND_HALF_UP` on whole seconds, so there is no float error.

### 3. `compute_overtime` has no 30 minute minimum and breaks on night shifts
- **Where:** `compute_overtime`
- **What is wrong:** R3 says overtime counts only if it is at least 30 minutes. The starter counts any amount. It also puts the shift end on the same date as the record, so for a night shift (22:00 to 06:00) the end time is on the wrong day.
- **How you would notice:** Shift ends 18:30, punch-out 18:35 returns 5 instead of 0. A night shift worker who leaves at 06:40 next morning gets 0 overtime instead of 40.
- **How I fixed it:** A helper `shift_bounds` adds one day to the shift end for night shifts, and `overtime_minutes` returns 0 when the result is below 30.

---

## Problems in `punch_in`

### 4. Time handling depends on the server's time zone
- **Where:** `punch_in`, the lines that create `ts` and `d`
- **What is wrong:** `datetime.fromtimestamp(...)` and `datetime.now()` give local server time without a time zone. So the attendance date changes depending on where the server runs. On a UTC server, a punch at 02:00 IST is filed under the previous day. It also divides by 1000 with a float, keeps the milliseconds, and ignores the night shift date rule in R1.
- **How you would notice:** Run the server in UTC and punch in at 02:00 IST. In my own test the stored punch time also kept milliseconds (`...692`) instead of whole seconds.
- **How I fixed it:** `from_ms` converts with integer division into a UTC time. `now_utc` cuts the seconds. `attendance_date` works out the IST date and applies the night shift rule. I also created the Mongo client with `tz_aware=True`, so times coming from the database have a time zone too.

### 5. Unknown employee gives a 500 error
- **Where:** `punch_in`
- **What is wrong:** If the employee does not exist, `emp` is `None` and `emp["shift_start"]` crashes with a `TypeError`.
- **How you would notice:** I sent `emp_code: EMP9999` and saw the 500 and the traceback in the server log.
- **How I fixed it:** Return 404 as soon as the employee is not found.

### 6. Any status is accepted
- **Where:** `PunchInIn.status`
- **What is wrong:** `status` is a plain string, so `ABSENT`, `LEAVE` or even random text is accepted. The contract allows only `PRESENT`, `WFH` and `ON_DUTY` here.
- **How you would notice:** I sent `status: ABSENT` and got 201. An ABSENT record with a punch-in time was created in my database.
- **How I fixed it:** The field is now a `Literal` of the three allowed values, so anything else returns 422.

### 7. `punched_at` is not checked properly
- **Where:** `PunchInIn.punched_at` and the `if body.punched_at` check
- **What is wrong:** It is a plain `int`. A value in seconds, a negative number or `0` is accepted. `0` is treated as "not given" and quietly becomes the current time.
- **How you would notice:** `punched_at: 1700000000` (seconds) returns 201, but the contract says 422.
- **How I fixed it:** I made an `EpochMs` type: a strict integer between 100000000000 and 4102444800000. Floats, strings and seconds-looking numbers now return 422. The code checks `is not None` instead of truthiness.

### 8. Two parallel punch-ins can both succeed
- **Where:** `punch_in`, `find_one` followed by `insert_one`
- **What is wrong:** It first looks for an existing record and then inserts. Two requests at the same moment can both pass the check and both insert. There was also no unique index.
- **How you would notice:** Send 10 identical punch-ins at once. The contract wants one 201 and nine 409s.
- **How I fixed it:** A unique index on `(emp_code, date)` now decides the winner. I just insert and catch `DuplicateKeyError`, then return 409.

### 9. The response shape does not match the contract
- **Where:** end of `punch_in`
- **What is wrong:** It returns the Mongo `_id` as `id`, and `punch_in` as a datetime instead of epoch milliseconds.
- **How you would notice:** Just look at the response body.
- **How I fixed it:** A `record_out()` function builds the exact response fields, converts times to milliseconds, and fills defaults for old records.

---

## Problems in the employee endpoints

### 10. `create_employee` has the same race and no validation
- **Where:** `create_employee` and the `EmployeeIn` model
- **What is wrong:** It checks and then inserts, so two parallel requests can create the same `emp_code`. There was no unique index. Nothing is validated: the `emp_code` format, email, shift times as `HH:MM`, `shift_start` equal to `shift_end`, and `joined_on`. `created_at` is local time without a time zone and is returned as text, not epoch milliseconds.
- **How you would notice:** Send `emp_code: "abc"` and it returns 201. Send several identical creates at once and more than one succeeds.
- **How I fixed it:** A unique index on `emp_code` and catching `DuplicateKeyError` for the 409. The model now has format rules and a validator for the dates and shifts. `created_at` uses `now_utc()` and is returned in milliseconds.

### 11. `list_employees` is wrong in several ways
- **Where:** `list_employees`
- **What is wrong:** `skip = page * page_size` skips the whole first page. `total` counts all employees, not the filtered ones. There is no sorting, and `page` and `page_size` have no limits (R10).
- **How you would notice:** `page=1` returns the second page. Filtering by department does not change `total`.
- **How I fixed it:** `skip = (page - 1) * page_size`, `count_documents` uses the filter, results are sorted by `emp_code`, and `page` is at least 1 and `page_size` between 1 and 100.

---

## Problems in `list_attendance`

### 12. It loads the whole collection into memory
- **Where:** `list_attendance`
- **What is wrong:** It reads every matching document into a Python list, sorts it in Python, and then cuts out one page. On 100,000 records this is slow and uses a lot of memory.
- **How you would notice:** It gets slower as data grows. It would be a collection scan in `explain`.
- **How I fixed it:** Sorting, skip and limit now run inside MongoDB and use indexes.

### 13. Ordering, validation and old records
- **Where:** `list_attendance`
- **What is wrong:** It sorts only by date, so records of the same day come back in no fixed order (the contract wants `emp_code` after date). `date_from` later than `date_to` does not return 422. Bad dates and a wrong `status` are not rejected. It also leaks `id`, returns raw datetimes, and old records without `history` or `half_day` are not given defaults.
- **How you would notice:** `date_from=2026-07-05&date_to=2026-07-01` returns 200. In my test run the response showed `id` and raw time values.
- **How I fixed it:** A shared `attendance_filter()` validates the inputs. Sort is date descending, then `emp_code` ascending. `record_out()` shapes the output and fills the defaults.

---

## Problems that affect the whole app

### 14. No indexes at all
- **Where:** whole app
- **What is wrong:** The starter creates no indexes, so every query scans the whole collection.
- **How you would notice:** `explain` shows `COLLSCAN`.
- **How I fixed it:** `ensure_indexes()` runs at startup. It is safe to run many times.

### 15. `/health` always says ok
- **Where:** `health`
- **What is wrong:** It returns ok without ever talking to MongoDB. The contract says 503 when the database does not answer.
- **How you would notice:** Start the app with a wrong `MONGO_URI` and `/health` still returns 200.
- **How I fixed it:** It now pings MongoDB and returns 503 if that fails.

---

## Things I checked and found fine

- `load_dotenv()` does not overwrite variables that are already set, so real environment variables win over `.env`, which is what the task asks for.
- Creating one `MongoClient` at module level is fine. It is thread-safe and is meant to be shared.
- The 409 for a duplicate `emp_code` and the paged response shape (`items`, `total`, `page`, `page_size`) already match the contract.