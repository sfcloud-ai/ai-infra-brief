# Optional T4 scheduled delivery

The scheduler is disabled unless the existing relay is started with an explicit
`--scheduler-config` pointing to a validated configuration. Installing this code
does not enable a listener or change any service permissions. The existing relay
process retains its exclusive lock, SQLite ledger, rate limit and WeCom client.

The companion `sfcloud-ai/ai-infra-reports` change must supply
`scripts/verify-scheduled-delivery.mjs` and its complete, pinned dependency tree,
using Node 24.22.2. The example paths and UID are placeholders, not an installation
plan. All verifier code and dependencies must be immutable to the Publisher.

The relay-owned `scheduled.sock` accepts only `status` and `admit`. Kernel peer
credentials restrict both operations to the configured, distinct Publisher UID.
Admission independently verifies the original bundle and live publication, saves
the original bytes transactionally and never sends by itself. The serial relay
loop delivers due reports at 08:00 Asia/Shanghai (00:00 UTC for the report date),
only while active. A confirmed pause also prevents scheduled sends and new
admissions; status remains readable. Any unfinished scheduled job blocks a
deployment cutover, including jobs waiting for their due time or a known retry.

Both paths share report-date and edition claims. Before enabling the scheduler,
every historical `notice_edition` claim must be mapped from its original bundle
and source commit using `NodeHistoricalVerifier` and `reconcile_daily_claims` in
a separately controlled offline migration with the old consumer stopped. Missing
bundles or conflicting dates block activation. This source-only migration neither
verifies old delivery receipts nor authorizes another send. Do not construct
`relay.Store` against a live ledger for a read-only inspection: startup performs
recovery writes.

Retries cover publication reads and explicitly rejected WeCom parts, with bounded
exponential delay. Confirmed parts are retained; an uncertain write is frozen and
never automatically replayed, including after restart. Other dates may continue
only when the frozen scheduled record passes its original binding checks. The
strict deployment drain check still rejects unresolved delivery intents.

Enabling this feature is a separate deployment operation: reconcile the existing
ledger and native intents, select an activation date, install the companion
verifier, and review the proposed UID/socket mounts and service configuration.
Neither the example nor the tests authorize production changes. Merging Python
changes can trigger the existing main-branch CD workflow when CD is enabled.
