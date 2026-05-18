"""Structured logging configuration using structlog."""
import logging

import structlog


def configure_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    """Configure structlog for the application.

    Args:
        log_level: Logging level string (DEBUG, INFO, WARNING, ERROR).
        log_format: Output format — "json" for production, "console" for development.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "json":
        shared_processors.append(structlog.processors.JSONRenderer())
    else:
        shared_processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=shared_processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = __name__) -> structlog.BoundLogger:
    """Return a bound structlog logger for the given name."""
    return structlog.get_logger(name)
