# DECISIONS.md

## 1. Which indexes did I create, and why?
`employees`: unique `emp_code` (identity, 409 on duplicates); `(department, emp_code)` for the filtered list and the trend; `joined_on` and `(department, joined_on)` for headcount. `attendance_logs`: unique `(emp_code, date)` (race safety, monthly summary, `$lookup`); `(date -1, emp_code)` for list order and the leaderboard date range; `(status, date -1, emp_code)` for status filters; `(emp_code, punch_in -1)` to find the record to punch out. Each matches a filter-then-sort pattern, so explain shows IXSCAN.

## 2. What happens when two identical punch-ins arrive at the same instant?
Both pass validation and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept exactly one; the other gets `DuplicateKeyError`, which I return as 409. There is no check-then-insert.

## 3. What does the leaderboard return for a tie at the cutoff?
`$rank` gives 1, 2, 2, 4, and the limit is `rank <= limit` applied after ranking, so every tied employee is returned (limit 2 with a tie at rank 2 gives three rows).

## 4. How does the department summary count employees with no logs?
The pipeline starts from `employees` (`joined_on` on or before month end) and `$lookup`s logs with `preserveNullAndEmptyArrays`, so they count in headcount with zero totals. Later joiners are excluded; logs of unknown employees drop out.

## 5. What would I change for 100 times the data?
Pre-aggregated daily and monthly rollups (`$merge`) instead of scanning logs, department denormalised into each log to remove `$lookup`, cursor pagination instead of `skip`, archiving old months, and sharding by `emp_code`.