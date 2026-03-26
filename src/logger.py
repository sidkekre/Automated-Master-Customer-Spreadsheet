from datetime import datetime, timezone


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _log(level: str, message: str) -> None:
    print(f"[{level}] {_utc_timestamp()} {message}")


def InfoLogger(message: str) -> None:
    _log("INFO", message)

def WarnLogger(message: str) -> None:
    _log("WARN", message)

def ErrorLogger(message: str) -> None:
    _log("ERROR", message)