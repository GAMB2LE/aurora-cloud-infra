# HATPRO source sync

I follow a new acquisition through ASS, production raw, the product and both
archives. Recovered old files are useful history, not proof of a new cycle.

| Data | Path |
| --- | --- |
| Source | `aurora@100.124.55.22:/home/aurora/data/hatprog5` |
| Production raw | `/project/aurora/raw/hatprog5` |
| Product | `/data/aurora/products/hatprog5/hatpro.zarr` |

The sync keeps every `HATPROG5-AURORA-ICELAND_*` companion and NetCDF file at
its source-relative path. The product uses LWP, IWV, IRT, TPC, TPB, CMP.TPC and
MET NetCDFs. Standard and composite temperature profiles remain separate as
`T_PROF` and `T_PROF_CMP`, even when their timestamps overlap.

## Discovery and copying

The two-minute live scan compares relative path, size and mtime. It finds new,
changed, moved and old-mtime paths rather than relying on the timestamp cursor
alone. Flat paths stay required while the source uses them. A dated file with
the same basename is not a substitute. Old flat archive duplicates are ignored
only when the source no longer requires those exact paths.

The copier uses the existing rsync implementation and archive dispatcher.
Live and backfill copies share a lock. The independent six-hour backfill
reconciles missing paths without advancing the live cursor.

Pending archive handoffs are saved before copying and survive restarts.
Failed listing, copy or enqueue leaves that debt outstanding. Preserve
`hatpro-sync.last`, `.pending.json` and `.baseline.json`; changing source
bindings with old state must fail rather than silently reuse it.

With `hatpro_source_start_fresh: false`, first discovery reconciles existing
history. `true` creates an explicit baseline instead. Source and old production
copies are retained in both cases. Access uses
[Tailscale SSH](OPERATOR_QUICKSTART.md#source-access).

## If the product is stale

Check whether ASS is receiving new files before changing a cloud job. If not,
check the acquisition user's Windows output share and confirm it can write.
An SSH account and the interactive acquisition session can have different
network-drive mappings. Restore the output path before considering an
instrument restart.

Compare decoded sample times after the repair, then follow two new cycles to
raw, Zarr, GWS and object storage. An active service, filename or old recovered
batch is insufficient.

`aurora-hatpro-append.timer` and `aurora-hatpro-quicklooks.timer` process the
new data. Append uses a bounded lookback, prefers the dated source paths and
keeps samples newer than the product frontier. `--rebuild` is an intentional
full product rewrite, not a routine retry.

## Release and rollback

`playbooks/hatpro_discovery.yml` updates only the discovery code and wrapper.
Use a unique `hatpro_discovery_release`, inspect `--check --diff`, and wait
for idle sync/backfill workers. The playbook preserves schedules, source
bindings, checkpoints and archive gates. Check the executing payload on a
storage-guarded host. Roll back the recorded code, not an older cursor over
newer runtime progress.

Raw files reach both archives additively. ASS deletion still needs exact
signed proof after seven days. See [Archives](ARCHIVE_SERVICES.md) and
[ASS retention](ASS_BACKUP_AND_RETENTION.md).
