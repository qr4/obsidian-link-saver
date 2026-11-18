class SaveError(RuntimeError):
    """A save that cannot go on (yt-dlp missing, metadata unavailable,
    unknown GitHub owner...). Fails the job, not the service."""


class ContentFreeError(SaveError):
    """The summarizer's verdict that a page has nothing worth filing.

    Its own subclass so callers can tell it apart from genuine failures:
    import_captured_file deletes the capture on this one (a retry would
    reach the same verdict every time) and must not do that to a capture
    that failed for a reason that may not recur."""
