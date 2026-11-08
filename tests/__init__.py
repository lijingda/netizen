"""Test setup shared by discovery and module-based unittest runs."""

import logging
import sys

# Import both SDKs before replacing the streams they capture during import.
from lark_channel.core.log import logger as channel_logger
from lark_oapi.core.log import logger as oapi_logger


class _CurrentStdout:
    def write(self, text: str) -> int:
        return sys.stdout.write(text)

    def flush(self) -> None:
        sys.stdout.flush()


_stdout = _CurrentStdout()
for _logger in {channel_logger, oapi_logger}:
    for _handler in _logger.handlers:
        if isinstance(_handler, logging.StreamHandler):
            _handler.setStream(_stdout)
