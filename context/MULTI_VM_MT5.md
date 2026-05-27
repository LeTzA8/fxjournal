# Multi-VM MT5 Queue Affinity

Last updated: 2026-05-26

## Overview

MT5 Celery work is routed to VM-scoped Redis queues so each Hyonix worker only runs terminals that exist locally. One codebase and env file; VM identity comes from Windows `COMPUTERNAME`.

## Queues

| Legacy (migration drain only) | Scoped (default) |
|--------------------------------|------------------|
| `mt5_sync` | `mt5_sync.<slug>` |
| `mt5_priority` | `mt5_priority.<slug>` |
| `mt5_setup` | `mt5_setup.<slug>` |

Slug rules (`helpers/mt5_dispatch.py`): trim, lowercase, replace non-`a-z0-9` with `-`, collapse repeats, strip edges, max 48 chars. DB/display `vm_id` keeps raw `COMPUTERNAME` (e.g. `MYFXJOURNAL-SG`).

## Worker listen lists (PowerShell)

Sync (`run_mt5_sync_worker.ps1`):

```text
mt5_priority.<slug>,mt5_sync.<slug>
```

Setup (`run_mt5_setup_worker.ps1`):

```text
mt5_setup.<slug>
```

Optional migration drain: set `FXJ_MT5_LISTEN_LEGACY_QUEUES=1` on a worker to also consume legacy queue names until empty.

## Producer routing

All MT5 publish sites go through `helpers/mt5_dispatch.py`:

- `dispatch_mt5_sync` / `dispatch_mt5_priority` — use `MT5Account.vm_id` (required; skipped when missing)
- `dispatch_mt5_setup` — target VM from admin selector, `FXJ_MT5_SETUP_VM_IDS`, or `FXJ_MT5_SETUP_DEFAULT_VM_ID`
- `dispatch_mt5_cleanup` / `dispatch_mt5_pause` — account `vm_id`

## Setup failover (public/default only)

- `FXJ_MT5_SETUP_VM_IDS=MYFXJOURNAL-SG,<VM2>` — order is failover order
- `FXJ_MT5_SETUP_FAILOVER_MAX_VMS=2` (default)
- Admin targeted setup: `allow_failover=False`
- Failover only for VM/environment/reachability errors; not auth, wrong account, trading password, or invalid server string

## Wrong-VM guard

Tasks compare local slug to target `vm_id` slug at start. On mismatch, explicitly re-dispatch to the scoped queue and return `{"requeued": true}` (bounded by `_wrong_vm_redispatch_count`).

## Data backfill (accounts missing vm_id)

```sql
UPDATE mt5_account
SET vm_id = 'MYFXJOURNAL-SG'
WHERE vm_id IS NULL
  AND (terminal_path IS NOT NULL OR appdata_hash IS NOT NULL);
```

Accounts without `vm_id` are skipped by beat sync and account-bound admin dispatch until setup or a successful sync stamps affinity.

## Rollout checklist

1. Deploy code (scoped routing is always on)
2. Update PowerShell workers on all VMs (scoped queues only)
3. Run SQL backfill for accounts missing `vm_id`
4. Confirm admin Worker VMs panel shows per-VM scoped queue depths
5. Set `FXJ_MT5_SETUP_VM_IDS` on Render for setup failover order

## Legacy queue drain

If old tasks remain on `mt5_sync` / `mt5_priority` / `mt5_setup`, temporarily set `FXJ_MT5_LISTEN_LEGACY_QUEUES=1` on workers until Redis depths hit zero, then remove.
