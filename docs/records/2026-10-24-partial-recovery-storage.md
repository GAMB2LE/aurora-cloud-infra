# Preserve the interrupted-transfer recovery bundle on the system disk

I need to leave the existing 50 GiB archive-recovery reserve on `/data`, with
room for live growth while the radar copies and mounted verification finish.
The completed interrupted-transfer recovery bundle occupies 1,708,167,168
allocated bytes on that disk. The system disk had about 380 GB free at 08:57 UTC.

The bounded helper `scripts/relocate_partial_recovery_20261024.py` moves only
`/data/aurora/recovery/radar-partials-20261024` to
`/var/lib/aurora-recovery/radar-partials-20261024`. It preserves the old logical
path as a symlink, so the absolute recovery paths in the original manifest still
work. This directory is outside the normal raw/product archive catalogue. The
live configuration audit found no service using it; the helper checks open
descriptors, mappings, working directories and process roots again before the move.

There are 103 compressed recovery files and one manifest, totalling
1,707,812,212 bytes. The original preservation job already checked each raw
partial against a full decompression before removing that partial. This move
checks every compressed byte and the unchanged manifest. It preserves that
earlier recovery proof without decompressing the 248.6 GB again.

## Run and verify

Wait for the existing four-day radar stage to finish. Use the helper from an
exact reviewed commit on `main`, first without arguments, then with `--apply`.
Both commands require root and the original stage to be inactive with no PID.
The apply command takes its own lock, checks the exact file count/size and 103
completed manifest pairs, and requires 64 GiB to remain free on the system disk.

The helper copies to a new staging directory, preserves ownership, permissions,
modification times and extended attributes, fsyncs the files/directories and
independently hashes both copies. It rejects changed source identities and
unknown existing destinations. It then publishes the saved directory, renames
the original to a sibling `.relocated-original` directory and installs the old
path's symlink. No duplicate is removed until full logical-path readback passes.
Each removal checks the original identity and saved bytes again and writes a
durable intent and completion record.

The receipt is
`/var/lib/aurora-recovery/radar-partials-20261024.relocation.json`. It contains
the complete file map, compressed SHA-256 values, metadata, completed phases and
duplicate removals. The original manifest remains inside the preserved bundle.
After completion, measure actual free space again and rerun the radar capacity
gate with the remaining copy, drain and verification growth allowance.

## If interrupted

The helper refuses automatic retries when any destination, staging directory,
backup or receipt already exists. Keep every copy and inspect the receipt and
actual paths. A failed check does not trigger cleanup.

Before the first duplicate-removal intent, the complete original backup can be
renamed back after checking that the old logical path is either absent (an
interruption between rename and symlink creation) or still exactly our symlink.
Remove only that verified symlink before restoring the original name. Keep the
saved target as evidence. After removal begins, the target is
the complete preserved copy; rollback needs a separately verified copy back and
enough free space. Do not remove that target or treat the partial backup as a
complete original. The brief rename/symlink interval is acceptable only while
the completed bundle has no consumers, as checked for this dated operation.

This is an operator relocation, not a new service or retention policy. It does
not remove observations, change the raw archive or release the retention hold.
