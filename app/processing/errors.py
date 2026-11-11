class ProcessingError(Exception):
    """A document-level failure with a human-readable reason (corrupt, encrypted, unsupported...)."""
