# Keep the old radar quarantine on the existing root disk

I want to recover space on `/data` while keeping every observation and the old
path usable. This procedure moves only the closed radar family in the 29 August
quarantine. It does not change the current raw tree, archive policy, retention,
services or timers. It uses the already allocated root disk and adds no mount.

This is a prepared procedure. A merged helper is not evidence that the copy ran.
Record the reviewed main commit, pinned request, actual capacity and final receipt
before calling the preservation complete.

## Exact scope

The source is
`/data/quarantine/aurora-root-relocation/ass-mirror-20260829/rpgfmcw94`.
The target is `/var/lib/aurora-recovery/ass-mirror-20260829/rpgfmcw94`.
The source path becomes a symlink to the target after the copy has been verified.

The 24 October 09:58 UTC inspection found 166 regular files and 61 directories:
141,618,292,399 logical bytes, or 141,618,941,952 allocated bytes. All entries were
owned by uid/gid 56781 on `/data`. There were no symlinks, hardlinks, special files,
extended attributes, open users or mount aliases. Two complete metadata reads
matched. The bounded installed configuration and script search found no reference
to this old mirror. These are dated observations; repeat the checks before copying.

The helper is `scripts/preserve_old_radar_quarantine_20261024.py`. Its command-line
entry point accepts only these paths and this exact file count and logical size.
The reviewed JSON request also pins the complete source metadata, helper SHA-256,
source and root device identities, and a root reserve of at least 100 GiB. Any new
file, changed identity, link or unsupported type stops this specific operation.

## Capacity and access

The initial projection left about 236.57 GB on root and 201.44 GB on `/data` after
successful preservation. Refresh both numbers. Root must retain 100 GiB throughout
the copy and release. The separate 24 October `/data` planning budget is 146 GiB,
plus 2.3 GB/hour for the finite preservation and radar verification window. Its
seven-day allowances cover recovery, forecast excursions, scientific products,
GRIB downloads, media/state and additional archive generations. They are planning
allowances, not newly enforced caps. Runtime acceptance still needs actual free
space and successful producer/archive cycles. Keep native backfill and retention
behind their existing independent gates.

The original mirror parent is writable by its owner. The tool therefore renames
the original into a separate root-owned `0700` backup container under
`/data/quarantine/aurora-root-relocation/.radar-preservation-20261024` before any
duplicate release. It keeps the copied tree beneath a new root-owned `0700`
`/var/lib/aurora-recovery/ass-mirror-20260829` throughout verification and release.
This prevents the retained payload owner from opening a new writer through the
published target while the original duplicate is being removed.

Only after every original duplicate has been released does the helper set the
new target parent to `0755`. The copied directory and file ownership, modes,
modification times and extended attributes remain unchanged. Ordinary access
then follows those original permissions. The brief publication window is for a
closed historical quarantine; no active source or product path is included.

## Review, preflight and apply

Use an exact reviewed commit reachable from main. Install that exact script into
a root-owned location, verify its SHA, and keep the JSON request root-owned and
not writable by other users. The request contains:

- `source`, `target` and `backup_parent` with the exact paths above;
- `root_device`, `data_device`, `root_reserve_bytes` and `tool_sha256`;
- `metadata`, the reviewed per-path identity and attribute inventory from the
  audit, including the `.` directory entry.

The request SHA is supplied separately. Without `--apply`, the command only
observes and prints the admission result; it creates no receipt or lock file.

```sh
sudo python3 /usr/local/lib/preserve-old-radar-quarantine-20261024.py \
  --request /var/lib/aurora-recovery/old-radar-request-20261024.json \
  --request-sha256 REVIEWED_REQUEST_SHA
```

Preflight checks complete source membership and metadata, source stability,
root capacity, protected ancestors, installed references, visible process users
and mount aliases. It checks file descriptors, memory maps, working directories
and process roots by path and device/inode. Incomplete scans fail. Do not bypass
an unexpected reference, user or alias; establish what owns it first.

After the exact main commit, independent tests and fresh admission are reviewed,
run the same command with `--apply` in a durable operator session. There is no
recurring service. The fixed exclusive lock prevents another copy of this helper
from acting concurrently. Do not force a busy lock or use Python optimization.

## What proves the copy

The receipt is
`/var/lib/aurora-recovery/old-radar-preservation-20261024.json`.
Each phase and file intent is fsynced before its mutation. File content is copied
into a private temporary file while computing SHA-256, then fsynced, given its
original attributes and promoted atomically. The source identity must remain
unchanged. Completed copies are pinned by identity and hash.

After all copies exist, the helper independently reads and hashes every target
file. Whole-tree snapshots bracket that read, so changing an earlier verified
file while a later one is being read cannot be adopted. The complete original
inventory must still match. Only then does a recorded rename publish the target,
preserve the original inside the private backup container and create the exact
logical symlink.

The helper opens every logical file and checks that it resolves to the exact
already hashed target inode and metadata. The link identity, complete target file
set and target attributes must stay unchanged before and after. This is an
identity-backed logical read-through, not a third SHA pass over the same unchanged
inodes. It does not change the separate [current radar relocation](RADAR_RAW_RELOCATION.md)
requirement for full mounted verification before original release.

Only a successful publication and logical proof permit duplicate release. The
helper rechecks closure and capacity, then journals each original filename before
unlinking that exact entry through a pinned backup directory descriptor. Every
remaining original and every verified target must still match. It removes only
known empty payload directories with `rmdir`; it never recursively removes a tree.
The empty private backup container remains as a recovery marker. All copied
observations, hashes and the receipt remain.

## Interruption and acceptance

Use the identical installed helper and request with `--apply --resume`. Resume
requires the existing root-owned receipt and its exact request/helper/inventory
pins. A changed or missing receipt, foreign file, changed verified copy, wrong
symlink or missing original without a recorded removal intent stops recovery.

A pending incomplete temporary copy can be restarted only inside the owned
private container, with its recorded inode and unchanged original. A promoted
copy whose completion was interrupted needs full source/target byte verification
before it can be adopted. Original release recognizes a missing file only under
its own pending or completed intent. A staging-only proof can never authorize an
original unlink. Completed reruns verify and report without rewriting the receipt.

Do not delete a receipt, repair it by hand, recursively clean a partial tree or
restart from a new request over an existing operation. Retain the originals and
inspect the exact failed boundary. Before publication, the original path remains
authoritative. After publication, the proven target and durable receipt remain
the recovery basis even when some original duplicates have already been released.

Acceptance requires `phase: complete`, all 166 target files with their recorded
hashes/identities, the exact logical symlink, restored target-parent traversal,
no original backup payload, and measured free bytes on both filesystems. Capture
normal source, product and archive results separately. This operation preserves
an old quarantine; it does not by itself prove the dashboard, native backlog or
all archive frontiers are current.
