# DECISIONS

Major decisions with rationale only.

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

## D-007 Weekly AI Output Is Persisted

- Decision:
  prompt history, payloads, and generated weekly AI output are stored
- Why:
  supports auditability, admin review, and quality iteration
