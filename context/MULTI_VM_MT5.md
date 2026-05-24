# Multi-VM MT5 Queue Affinity

Last updated: 2026-05-24

## Overview

MT5 Celery work can be routed to VM-scoped Redis queues so each Hyonix worker only runs terminals that exist locally. One codebase and env file; VM identity comes from Windows `COMPUTERNAME`.

## Queues

| Legacy (migration) | Scoped (multi-VM) |
|--------------------|-------------------|
| `mt5_sync` | `mt5_sync.<slug>` |
| `mt5_priority` | `mt5_priority.<slug>` |
| `mt5_setup` | `mt5_setup.<slug>` |

Slug rules (`helpers/mt5_dispatch.py`): trim, lowercase, replace non-`a-z0-9` with `-`, collapse repeats, strip edges, max 48 chars. DB/display `vm_id` keeps raw `COMPUTERNAME` (e.g. `MYFXJOURNAL-SG`).

## Feature flag

- `FXJ_MT5_MULTI_VM=1` — producers publish to scoped queues; missing `vm_id` on active accounts is logged/skipped.
- `FXJ_MT5_MULTI_VM=0` (default) — legacy queue names only; safe to deploy before VM script rollout.

## Worker listen lists (PowerShell)

Sync (`run_mt5_sync_worker.ps1`):

```text
mt5_priority.<slug>,mt5_sync.<slug>,mt5_priority,mt5_sync
```

Setup (`run_mt5_setup_worker.ps1`):

```text
mt5_setup.<slug>,mt5_setup
```

Later: `FXJ_MT5_LISTEN_LEGACY_QUEUES=0` drops legacy queues after migration confidence.

## Producer routing

All MT5 publish sites go through `helpers/mt5_dispatch.py`:

- `dispatch_mt5_sync` / `dispatch_mt5_priority` — use `MT5Account.vm_id`
- `dispatch_mt5_setup` — target VM from admin selector, `FXJ_MT5_SETUP_VM_IDS`, or `FXJ_MT5_SETUP_DEFAULT_VM_ID`
- `dispatch_mt5_cleanup` / `dispatch_mt5_pause` — account `vm_id`

## Setup failover (public/default only)

- `FXJ_MT5_SETUP_VM_IDS=MYFXJOURNAL-SG,<VM2>` — order is failover order
- `FXJ_MT5_SETUP_FAILOVER_MAX_VMS=2` (default)
- Admin targeted setup: `allow_failover=False`
- Failover only for VM/environment/reachability errors; not auth, wrong account, trading password, or invalid server string

## Wrong-VM guard

Tasks compare local slug to target `vm_id` slug at start. On mismatch, explicitly re-dispatch to the scoped queue and return `{"requeued": true}` (bounded by `_wrong_vm_redispatch_count`).

## Data backfill (before enabling flag)

```sql
UPDATE mt5_account
SET vm_id = 'MYFXJOURNAL-SG'
WHERE vm_id IS NULL
  AND (terminal_path IS NOT NULL OR appdata_hash IS NOT NULL);
```

## Rollout

1. Deploy code with `FXJ_MT5_MULTI_VM=0`
2. Deploy updated PowerShell workers on all VMs (dual-listen is harmless with flag off)
3. Run SQL backfill
4. Confirm admin Worker VMs panel shows SG worker + account buckets
5. Enable `FXJ_MT5_MULTI_VM=1` on Render; set `FXJ_MT5_SETUP_VM_IDS`
6. Exness test: admin setup target → VM2; verify scoped setup/sync queues

## Rollback

Set `FXJ_MT5_MULTI_VM=0` and restart Render. Workers still consume legacy queues if dual-listen is enabled. Leave `vm_id` data in place.
