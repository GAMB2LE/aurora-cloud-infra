# 24 October 2026: stale SSH wrapper cleanup

The development mirror had accumulated generated `ssh-wrapper-<pid>` files in
`/var/lib/aurora-cloud/dev-live-mirror`. The old `aurora-dev-live-pull` wrapper did
not remove them on exit. The replacement installed at 06:59 UTC includes an
`EXIT` trap; two new wrappers disappeared when their scheduled pulls finished.

I removed 664,787 regular generated files older than 24 hours after checking
their names, contents, file metadata and current process references. The removed
files accounted for 2,722,967,552 allocated bytes. No raw data or products were
changed. Recent files and files whose PID existed at the deletion check stayed
in place. The final scan found 7,601 wrappers, including 26 retained by that
conservative PID check.

The cleanup completed without errors. Its compressed audit at
`/root/aurora-wrapper-cleanup-20261024/audit.jsonl.gz` contains 664,787 planned
and completed deletion records and is 17,347,870 bytes. One 541-byte backup
preserves the single distinct content hash. The audit was read back and the
backup hash verified. The containing directory is mode `0700`; the audit and
backup are mode `0600`. `progress.json` and `verification.json` in the same
directory record the counts and checks. The helper has finished; no recurring
cleanup job was installed.
