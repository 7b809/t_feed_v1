import os
import logging

# Path to the logs directory
LOGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)

# Path to the plain-text logs directory (a mirror of LOGS_DIR,
# but containing .txt files that are simpler to read / grep / share).
TEXT_LOGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs_text"
)


class LineCountRotatingHandler(logging.FileHandler):
    """
    A file handler that rotates the log file after a maximum number of lines.
    Keeps a maximum specified count of old log files.
    """

    def __init__(self, filename, maxLines=1000, backupCount=3, encoding="utf-8"):
        super().__init__(filename, mode="a", encoding=encoding)
        self.maxLines = maxLines
        self.backupCount = backupCount
        self.line_count = self._count_existing_lines()

    def _count_existing_lines(self) -> int:
        """
        Counts existing lines in the log file on startup
        if it already exists.
        """
        if os.path.exists(self.baseFilename):
            try:
                with open(
                    self.baseFilename,
                    "r",
                    encoding=self.encoding,
                    errors="ignore",
                ) as file:
                    return sum(1 for _ in file)
            except (OSError, UnicodeError):
                return 0

        return 0

    def emit(self, record):
        """
        Emits a record and rotates if writing the record would exceed
        the configured line threshold.
        """
        try:
            formatted_message = self.format(record)
            record_line_count = formatted_message.count("\n") + 1

            if self.shouldRollover(record_line_count):
                self.doRollover()

            super().emit(record)
            self.line_count += record_line_count

        except Exception:
            self.handleError(record)

    def shouldRollover(self, new_line_count=1) -> bool:
        """
        Determines whether the new log record would exceed maxLines.
        """
        return (
            self.line_count > 0
            and self.line_count + new_line_count > self.maxLines
        )

    def doRollover(self):
        """
        Rotates log files:

        file.log.2 -> file.log.3
        file.log.1 -> file.log.2
        file.log   -> file.log.1
        """
        if self.stream:
            self.stream.close()
            self.stream = None

        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                source_file = f"{self.baseFilename}.{i}"
                destination_file = f"{self.baseFilename}.{i + 1}"

                if os.path.exists(source_file):
                    if os.path.exists(destination_file):
                        os.remove(destination_file)

                    os.replace(source_file, destination_file)

            first_backup = f"{self.baseFilename}.1"

            if os.path.exists(first_backup):
                os.remove(first_backup)

            if os.path.exists(self.baseFilename):
                os.replace(self.baseFilename, first_backup)

        elif os.path.exists(self.baseFilename):
            os.remove(self.baseFilename)

        self.stream = self._open()
        self.line_count = 0


class PlainTextHandler(logging.FileHandler):
    """
    A simple append-only plain-text log handler.

    Writes each log record to a .txt file (mirror of the .log file)
    using a slightly simpler, human-friendly format. No rotation is
    performed by default, so the file keeps a complete history in a
    single easy-to-open text file.

    Rotation can still be enabled by passing maxBytes / backupCount
    (handled by the parent FileHandler when using
    logging.handlers.RotatingFileHandler instead of FileHandler; here
    we keep it simple with a plain append-only FileHandler).
    """

    def __init__(self, filename, encoding="utf-8"):
        super().__init__(filename, mode="a", encoding=encoding)


def _build_formatter() -> logging.Formatter:
    """
    Shared formatter used by both the .log and .txt handlers.
    """
    return logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] "
        "[%(filename)s:%(lineno)d] [%(funcName)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_logger(filename: str, log_level=logging.DEBUG) -> logging.Logger:
    """
    Creates and returns a logger that logs all standard logging levels
    to:

      - the console
      - a rotating .log file under logs/
      - an append-only .txt file under logs_text/

    Supported levels:
    - DEBUG
    - INFO
    - WARNING
    - ERROR
    - CRITICAL

    The formatter automatically includes:
    - Source filename
    - Source line number
    - Function or method name

    :param filename:
        File or module name, normally passed as __file__.

    :param log_level:
        Logging level. Defaults to logging.DEBUG.
    """

    # Extract the pure filename without its path or extension.
    base_name = os.path.splitext(os.path.basename(filename))[0]

    # Ensure that the logs directories exist.
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(TEXT_LOGS_DIR, exist_ok=True)

    logger = logging.getLogger(base_name)
    logger.setLevel(log_level)

    # Prevent messages from reaching root logger handlers.
    logger.propagate = False

    # Avoid adding duplicate handlers.
    if logger.handlers:
        return logger

    formatter = _build_formatter()

    # ------------------------------------------------------------------
    # 1. Console handler
    # ------------------------------------------------------------------
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ------------------------------------------------------------------
    # 2. Rotating .log file handler
    # ------------------------------------------------------------------
    log_file_path = os.path.join(
        LOGS_DIR,
        f"{base_name}.log",
    )

    file_handler = LineCountRotatingHandler(
        log_file_path,
        maxLines=1000,
        backupCount=3,
        encoding="utf-8",
    )

    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # ------------------------------------------------------------------
    # 3. Plain-text .txt file handler (mirror)
    # ------------------------------------------------------------------
    text_file_path = os.path.join(
        TEXT_LOGS_DIR,
        f"{base_name}.txt",
    )

    text_handler = PlainTextHandler(
        text_file_path,
        encoding="utf-8",
    )

    text_handler.setLevel(log_level)
    text_handler.setFormatter(formatter)
    logger.addHandler(text_handler)

    return logger