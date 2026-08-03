import os
import logging

# Path to the logs directory
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


class LineCountRotatingHandler(logging.FileHandler):
    """
    A file handler that rotates the log file after a maximum number of lines.
    Keeps a maximum specified count of old log files (backupCount).
    """
    def __init__(self, filename, maxLines=1000, backupCount=3, encoding="utf-8"):
        super().__init__(filename, mode="a", encoding=encoding)
        self.maxLines = maxLines
        self.backupCount = backupCount
        self.line_count = self._count_existing_lines()

    def _count_existing_lines(self) -> int:
        """Counts existing lines in the log file on startup if it already exists."""
        if os.path.exists(self.baseFilename):
            try:
                with open(self.baseFilename, "r", encoding=self.encoding, errors="ignore") as f:
                    return sum(1 for _ in f)
            except Exception:
                return 0
        return 0

    def emit(self, record):
        """Emits a record and rotates if the line threshold is exceeded."""
        if self.shouldRollover(record):
            self.doRollover()
        super().emit(record)
        # Count newlines in formatted log message
        msg = self.format(record)
        self.line_count += msg.count("\n") + 1

    def shouldRollover(self, record) -> bool:
        """Determines if the line count has exceeded maxLines."""
        return self.line_count >= self.maxLines

    def doRollover(self):
        """Rotates log files: file.log -> file.log.1, file.log.1 -> file.log.2, etc."""
        if self.stream:
            self.stream.close()
            self.stream = None

        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                sfn = f"{self.baseFilename}.{i}"
                dfn = f"{self.baseFilename}.{i + 1}"
                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    os.rename(sfn, dfn)
            
            dfn = f"{self.baseFilename}.1"
            if os.path.exists(dfn):
                os.remove(dfn)
            if os.path.exists(self.baseFilename):
                os.rename(self.baseFilename, dfn)

        # Reopen file in write mode to clear active log
        self.stream = self._open()
        self.line_count = 0


def get_logger(filename: str, log_level=logging.INFO) -> logging.Logger:
    """
    Creates and returns a logger that logs to both console and a file 
    named `logs/<filename>.log`, capping active log files at 1,000 lines.

    :param filename: Base name for the file or module (e.g. 'token_service' or 'main.py')
    :param log_level: Logging level (default: logging.INFO)
    """
    # Sanitize and extract pure filename without path/extension
    base_name = os.path.splitext(os.path.basename(filename))[0]
    
    # Ensure the logs directory exists
    os.makedirs(LOGS_DIR, exist_ok=True)
    
    logger = logging.getLogger(base_name)
    logger.setLevel(log_level)
    
    # Avoid adding duplicate handlers if get_logger is called multiple times for same module
    if logger.hasHandlers():
        return logger

    # Formatter for log lines
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 2. Line-Capped File Handler (logs/{filename}.log)
    log_file_path = os.path.join(LOGS_DIR, f"{base_name}.log")
    file_handler = LineCountRotatingHandler(
        log_file_path,
        maxLines=1000,    # Capped strictly to 1,000 lines
        backupCount=3,    # Keeps up to 3 old log files (e.g., .log.1, .log.2)
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger