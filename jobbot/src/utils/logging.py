"""Centralized logging configuration."""

import logging
import os
import sys


def setup_logging(name: str = "jobbot") -> logging.Logger:
    """Configure and return a logger."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level, logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level, logging.INFO))

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    return logger
