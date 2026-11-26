# Agent notes

Scheduled workflows in this repository must set `timezone:` to an IANA
name on every `on.schedule` entry. Copying the hour from the ticket into
`cron: '0 9 * * *'` is not enough: GitHub Actions still defaults to UTC
when the field is omitted.

A five-field comment check ("the comment contains 9 点") is not a merge
rule. Use `tools/cron_timezone_gate.py`.
