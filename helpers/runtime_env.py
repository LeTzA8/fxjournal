import os

from dotenv import load_dotenv


DEFAULT_ENV_FILE_CANDIDATES = (
    ".env",
    "FXJournal Main.env",
    "fxjournal.env",
)


def load_runtime_env():
    configured_env_file = os.getenv("FXJ_ENV_FILE", "").strip()
    env_candidates = []
    if configured_env_file:
        env_candidates.append(configured_env_file)
    env_candidates.extend(DEFAULT_ENV_FILE_CANDIDATES)

    seen = set()
    for candidate in env_candidates:
        candidate = str(candidate or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        load_dotenv(candidate, override=False)
