"""agent_exec: project operations, owned jobs and evidence.

Layout: contracts (pure profile validation/rendering), profiles (loading +
read-only CLI), profiles/*.json (admission-state'd operation definitions),
linux_jobs (checkpoint 2 backend), reports/harness (checkpoint 3 adapters).
Importing this package must never start processes or touch services.
"""

__version__ = "0.1.0"
