# Preserved radar recovery helpers

These files preserve the versioned helpers used during radar archive recovery.
This directory is not an installable Ansible role: it has no tasks that deploy
these programs. A checkout or Git merge does not change the live coordinator.

The helpers depend on exact host release directories, frozen manifests, queue
schemas, worker pins and durable receipts. Many paths and operation IDs are
historical on purpose. Do not copy the directory over an installed release or
replay a dated operation as a general repair command.

Before a future release, compare installed hashes, preserve later host fixes,
inspect live workers/locks and review the complete imported helper set. Reconcile
outstanding operations before admitting another transfer. Test the exact package
in Python 3.10 and verify its next independent archive readback afterward.

`completed_coordinator_gate.py` recognizes only the frozen historical cohort's
successful completion. It does not certify fresh files. Every helper keeps
retention and eviction unauthorized; only the separate signed retention process
can grant source deletion.

Start with [archive recovery](../../docs/ARCHIVE_RECOVERY.md) and
[archive tests](../../docs/ARCHIVE_TESTING.md).
