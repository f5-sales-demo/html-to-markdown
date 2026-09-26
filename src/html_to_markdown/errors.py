"""Domain errors and terminal classifications."""


class ScrapeError(Exception):
    """Base scrape error."""


class AllowlistError(ScrapeError):
    """A page or redirect escaped its source boundary."""


class AuthenticationWallError(ScrapeError):
    """Anonymous content was replaced by an authentication wall."""


class NotFoundError(ScrapeError):
    """The source returned a hard or rendered soft 404."""


class NavigationOnlyError(ScrapeError):
    """The URL is a terminal navigation-only removal."""


class ValidationError(ScrapeError):
    """Content or snapshot validation failed."""


class PublicationBlockedError(ScrapeError):
    """A release snapshot failed its publication gates."""
