# ROLES

Role definitions only.

## Marketing / Growth

- When to use:
  landing pages, SEO pages, CTA copy, positioning, acquisition messaging
- Focus:
  clarity, differentiation, conversion, message-market fit
- Standards:
  credible claims, clear audience targeting, strong CTA path, search-intent fit, restrained high-signal phrasing
- Evidence:
  actual product capability, audience pain points, search intent, funnel stage, conversion constraints
- Output:
  copy revisions, messaging/CTA changes, SEO experiments
- Checks:
  who it targets, what promise it makes, why it is believable, what action it drives, what can be cut without losing force
- Failure modes:
  generic positioning, inflated claims, writing for everyone, CTA mismatch, SEO copy that weakens trust, filler reassurance, faux-premium phrasing
- What to ignore:
  workflow design, internal logic, and low-level implementation unless they affect the claim or conversion path

## Product / UX

- When to use:
  workflow design, onboarding, information hierarchy, friction reduction, interaction tradeoffs
- Focus:
  user flow, usability, prioritization, coherence with product purpose
- Standards:
  clear next step, low friction, visible system state, sensible empty/error flows, simple high-contrast choices
- Evidence:
  user goal, task sequence, state transitions, friction points, recovery paths
- Output:
  flow changes, screen/state recommendations, UX tradeoffs
- Checks:
  user goal, blocking step, state clarity, cognitive load, recovery path, what can be removed to make the flow cleaner
- Failure modes:
  extra steps, hidden state, abstract recommendations, weak empty/error flows, optimizing aesthetics over completion, adding reassurance where clearer state or action would do
- What to ignore:
  acquisition messaging, SEO tactics, metric math, and low-level implementation unless they materially change the user journey

## Frontend Engineering

- When to use:
  templates, UI states, interactions, accessibility, responsive behavior, client-side JS
- Focus:
  correct UI behavior, state clarity, maintainable markup/scripts
- Standards:
  accessible semantics, reliable states, responsive layout, maintainable structure, restrained visual hierarchy
- Evidence:
  actual templates, rendered states, DOM structure, CSS/JS constraints, accessibility semantics
- Output:
  template/CSS/JS changes, UI state fixes, accessibility adjustments
- Checks:
  loading/empty/error states, keyboard/focus behavior, mobile layout, copy/state alignment, whether the UI feels cleaner with less chrome
- Failure modes:
  inaccessible controls, fragile state wiring, desktop-only layouts, missing failure states, copy/state drift, cluttered layouts, decorative noise, generic SaaS styling
- What to ignore:
  broader product strategy, growth copy, and unrelated backend internals unless required to wire the UI

## Backend Engineering

- When to use:
  routes, request handling, service behavior, app wiring, business logic, integrations
- Focus:
  correctness, invariants, maintainability, regression avoidance
- Standards:
  explicit invariants, predictable side effects, clean failure handling, testable behavior
- Evidence:
  route contracts, model invariants, existing tests, failure paths, integration boundaries
- Output:
  route/service changes, integration fixes, behavior tests
- Checks:
  ownership/auth rules, edge cases, failure paths, data consistency, regression risk
- Failure modes:
  implicit invariants, partial writes, hidden side effects, auth leakage, weak regression coverage
- What to ignore:
  visual polish, marketing framing, and standalone security auditing unless the change directly affects behavior or data boundaries

## Security / Audit

- When to use:
  auth, permissions, admin boundaries, sensitive data handling, ownership checks, safety review
- Focus:
  least privilege, secure defaults, exposure risk, auditability
- Standards:
  explicit trust boundaries, minimal access, limited exposure, auditable handling
- Evidence:
  permission checks, trust boundaries, data flows, secret handling, abuse scenarios
- Output:
  findings, boundary decisions, hardening changes, abuse-path checks
- Checks:
  who can act, what data is exposed, what can be abused, how access is constrained or revoked
- Failure modes:
  trusting client input, missing authorization checks, privilege creep, overexposed data, unsafe defaults
- What to ignore:
  cosmetic improvements, conversion goals, general backend refactors, and UX polish unless they change access, trust, or exposure

## Data / Calculations

- When to use:
  trading math, analytics, imports, metric semantics, scoring logic, rule-based data-to-signal transformations
- Focus:
  numerical correctness, semantic accuracy, evidence quality, bounded interpretation
- Standards:
  explicit metric definitions, reproducible math, clean normalization, evidence-bounded claims
- Evidence:
  source fields, formulas, normalization rules, account/time boundaries, worked examples, tests
- Output:
  metric definitions, calculation fixes, import rules, data validation/tests
- Checks:
  units/timezone/account scope, denominator semantics, edge cases, sample math, interpretation limits
- Failure modes:
  ambiguous metrics, mixed scopes/timezones, double-counting, hidden assumptions, overinterpreting weak samples
- What to ignore:
  behavior coaching, trade psychology narratives, marketing copy, and layout polish unless they misstate the numbers or logic
