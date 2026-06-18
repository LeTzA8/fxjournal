# DECISIONS

Major decisions with rationale only.

## D-009 Futures Prices Snap To Catalog Tick Size

- Decision:
  futures entry/exit/stop/target prices display and persist on each instrument's tick grid; default unknown roots to `0.25` (2 dp) unless `futures_symbols` research specifies otherwise (e.g. YM `1.0`, CL `0.01`, RTY `0.1`)
- Why:
  keeps PnL/tick math, charts, and UI aligned with exchange minimum increments and avoids float garbage on axes

## D-001 Shared AI Context Lives Under `/context/`

- Decision:
  `PROJECT_MAP.md`, `CURRENT_STATE.md`, `ROLES.md`, and `DECISIONS.md` live only under `/context/`
- Why:
  keeps one shared memory tree and avoids path drift or duplicate sources of truth

## D-002 Thin Tool Wrappers Only

- Decision:
  `AGENTS.md`, `CLAUDE.md`, and `.cursor/rules/project.mdc` stay thin wrappers
- Why:
  wrapper drift is reduced when shared logic lives in context files

## D-003 Role Definitions And Role Routing Stay Separate

- Decision:
  `ROLES.md` defines roles only; wrappers handle role selection and use
- Why:
  avoids overlapping authority between role definitions and tool-specific instructions

## D-004 Reviews Stay Account-Scoped

- Decision:
  dashboard review, MT5 state, and weekly AI stay anchored to the active `TradeAccount`
- Why:
  preserves the real trading context and avoids cross-account review drift

## D-005 Bundle Normalization Precedes Behavior Interpretation

- Decision:
  likely split entries should be grouped before behavior scoring or weekly review interpretation when applicable
- Why:
  reduces false signals from one trade idea being counted as several impulsive trades

## D-006 MT5 Sync Stays Read-Only And Request-Based

- Decision:
  MT5 sync uses investor/read-only positioning with admin-mediated setup
- Why:
  lowers operational and security risk while keeping broker history useful

## D-007 Weekly AI Reviews Persist Per Account And Week

- Decision:
  generated weekly AI reviews are stored in durable DB rows scoped to the active trade account and New York market week; the dashboard reuses stored text/metadata until explicit regeneration or a new eligible week
- Why:
  keeps review context stable for citations, follow-up chat, and experiment tracking without regenerating on every dashboard visit

## D-008 MT5 Queue Affinity Uses VM-Scoped Celery Queues

- Decision:
  MT5 sync/priority/setup/cleanup/pause tasks always publish to `mt5_*.<slug>` queues derived from `MT5Account.vm_id` / setup target VM; workers consume scoped queues per VM (`COMPUTERNAME` slug). Optional `FXJ_MT5_LISTEN_LEGACY_QUEUES=1` drains legacy queues during migration.
- Why:
  keeps each Hyonix VM on local terminal paths only, enables controlled setup failover, and avoids cross-VM task theft without separate codebases
