"""Shared rollout gate; rebuilding does not disable compatible writes."""
import os


def analytics_enabled() -> bool:
    return os.getenv('P108_ANALYTICS_ENABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'}
