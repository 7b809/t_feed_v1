import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def read_json(
    path: Path,
    default: Any = None,
) -> Any:
    if not path.exists():
        return default

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except (
        OSError,
        json.JSONDecodeError,
    ):
        return default


def write_json_atomic(
    path: Path,
    payload: Any,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(
                payload,
                temporary_file,
                indent=4,
                ensure_ascii=False,
                default=str,
            )

            temporary_file.flush()
            os.fsync(temporary_file.fileno())

            temporary_path = Path(temporary_file.name)

        for attempt in range(1, 6):
            try:
                os.replace(
                    temporary_path,
                    path,
                )

                temporary_path = None
                return

            except PermissionError:
                if attempt == 5:
                    raise

                time.sleep(0.05 * attempt)

    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
