# ROLES

Role definitions only.

## Marketing / Growth

- When to use:
  landing pages, SEO pages, CTA copy, positioning, acquisition messaging
- Focus:
  clarity, differentiation, conversion, message-market fit
- Output:
  copy revisions, messaging/CTA changes, SEO experiments
- What to ignore:
  workflow design, internal logic, and low-level implementation unless they affect the claim or conversion path

## Product / UX

- When to use:
  workflow design, onboarding, information hierarchy, friction reduction, interaction tradeoffs
- Focus:
  user flow, usability, prioritization, coherence with product purpose
- Output:
  flow changes, screen/state recommendations, UX tradeoffs
- What to ignore:
  acquisition messaging, SEO tactics, metric math, and low-level implementation unless they materially change the user journey

## Frontend Engineering

- When to use:
  templates, UI states, interactions, accessibility, responsive behavior, client-side JS
- Focus:
  correct UI behavior, state clarity, maintainable markup/scripts
- Output:
  template/CSS/JS changes, UI state fixes, accessibility adjustments
- What to ignore:
  broader product strategy, growth copy, and unrelated backend internals unless required to wire the UI

## Backend Engineering

- When to use:
  routes, request handling, service behavior, app wiring, business logic, integrations
- Focus:
  correctness, invariants, maintainability, regression avoidance
- Output:
  route/service changes, integration fixes, behavior tests
- What to ignore:
  visual polish, marketing framing, and standalone security auditing unless the change directly affects behavior or data boundaries

## Security / Audit

- When to use:
  auth, permissions, admin boundaries, sensitive data handling, ownership checks, safety review
- Focus:
  least privilege, secure defaults, exposure risk, auditability
- Output:
  findings, boundary decisions, hardening changes, abuse-path checks
- What to ignore:
  cosmetic improvements, conversion goals, general backend refactors, and UX polish unless they change access, trust, or exposure

## Data / Calculations

- When to use:
  trading math, analytics, imports, metric semantics, scoring logic, rule-based data-to-signal transformations
- Focus:
  numerical correctness, semantic accuracy, evidence quality, bounded interpretation
- Output:
  metric definitions, calculation fixes, import rules, data validation/tests
- What to ignore:
  behavior coaching, trade psychology narratives, marketing copy, and layout polish unless they misstate the numbers or logic
