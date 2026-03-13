class NonRetryableTaskError(Exception):
    """Task failure that should never be retried."""

    pass
