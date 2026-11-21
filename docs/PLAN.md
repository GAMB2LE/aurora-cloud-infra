# Original cloud rebuild plan

This was the May 2026 plan. It records the first host audit, proposed storage
layout and initial source connections. Its addresses, capacities and service
states are historical.

I keep the [full original plan in Git](https://github.com/GAMB2LE/aurora-cloud-infra/blob/7f25708/docs/PLAN.md)
so the decisions remain traceable. Use these pages for current work:

- [Operator guide](OPERATOR_QUICKSTART.md): where to start and what to check.
- [Production and development](PRODUCTION_DEVELOPMENT.md): who owns the writers.
- [Archive services](ARCHIVE_SERVICES.md): both archives, verification and retention.
- [Releases](RELEASES.md): deploy an exact reviewed revision.
- [Recovery records](records/index.md): dated outcomes and unfinished work.

The early GWS-only design has been replaced by independent GWS and object-store
copies. The old plan cannot establish archive parity or authorize pruning.
