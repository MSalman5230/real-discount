class PriceHistoryError(Exception):
    """A failed product lookup, with a stable reason for logging and tests."""

    def __init__(self, message, code="processing_error", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
