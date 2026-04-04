# AGENTS

- Default to non-trivial when unsure
- Non-trivial tasks: new features, route changes, model/schema changes, sensitive areas, multi-file work, or anything affecting business logic, auth, or calculations
- For non-trivial tasks, read `/context/PROJECT_MAP.md`, `/context/CURRENT_STATE.md`, `/context/ROLES.md`, and `/context/DECISIONS.md` first
- For non-trivial tasks, classify the task using `/context/ROLES.md` and state `Role: <name>` before acting
- Trivial / isolated tasks: commit messages, small UI tweaks, HTML/CSS edits, variable renames, simple single-file bug fixes
- For trivial / isolated tasks, work only from the provided file or snippet; skip full context loading and role routing
- Avoid full repo scans unless ownership is unclear or the work truly spans modules
- Prefer targeted file reads from `/context/PROJECT_MAP.md`
- Before major edits, state a short plan
- Update `/context/CURRENT_STATE.md` after meaningful non-trivial changes
- Respect `/context/DECISIONS.md` for non-trivial tasks; note any deviation briefly
