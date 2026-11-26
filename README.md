# iotxing-lab-cron-timezone-20261126

Lab fixture for a scheduled reconciliation job. The ticket in `TICKET.md`
asks for 09:00 Asia/Shanghai. The gate in `tools/cron_timezone_gate.py`
requires every `on.schedule` entry to declare a valid IANA `timezone:`.

This repository is an experiment. The next-run tables are computed with
Python's `zoneinfo` from a frozen clock; they are not records of a live
GitHub Actions schedule firing.
