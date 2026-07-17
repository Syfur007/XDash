"""Turns raw stdout/log lines from train.py / eval.py into structured data.

Two things are extracted:
  1. Every "Epoch NNN | Train Loss: ... | Val Dice: ..." line -> a metrics
     point usable directly by a chart.
  2. The loguru-style "<timestamp> | LEVEL | module:line - message" prefix,
     so the live log viewer can color lines by level.
"""
from __future__ import annotations

import re
from typing import List, Dict, Any, Optional

LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*"
    r"(?P<level>[A-Z]+)\s*\|\s*"
    r"(?P<source>[^-]+)-\s*(?P<message>.*)$"
)

# Matches the "Epoch 001 | Train Loss: 1.35 | Val Loss: ... | Val Dice: ..." line.
# Every metric after "Epoch NNN" is optional and captured generically so new
# metrics added to train.py show up automatically without editing this regex.
EPOCH_RE = re.compile(r"Epoch\s+(?P<epoch>\d+)\b")
METRIC_RE = re.compile(r"([A-Za-z][A-Za-z0-9 ]*?):\s*([-+]?[0-9]*\.?[0-9]+(?:e[-+]?\d+)?)")


def parse_log_line(line: str) -> Dict[str, Any]:
    m = LOG_LINE_RE.match(line)
    if not m:
        return {"ts": None, "level": "INFO", "source": None, "message": line.rstrip("\n")}
    return {
        "ts": m.group("ts"),
        "level": m.group("level"),
        "source": m.group("source").strip(),
        "message": m.group("message").rstrip("\n"),
    }


def parse_epoch_metrics(message: str) -> Optional[Dict[str, Any]]:
    """If `message` is an epoch summary line, return {epoch, metrics: {...}}."""
    em = EPOCH_RE.search(message)
    if not em or "|" not in message:
        return None
    epoch = int(em.group("epoch"))
    metrics: Dict[str, float] = {}
    # Only look at the part after "Epoch NNN" so we don't misparse the word
    # "Epoch" itself as a metric.
    tail = message[em.end():]
    for name, value in METRIC_RE.findall(tail):
        key = name.strip()
        if key.lower() == "epoch":
            continue
        try:
            metrics[key] = float(value)
        except ValueError:
            continue
    if not metrics:
        return None
    return {"epoch": epoch, "metrics": metrics}


def parse_log_text(text: str) -> Dict[str, Any]:
    """Parse a full log file/tail into (lines, metric series)."""
    lines: List[Dict[str, Any]] = []
    series: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parsed = parse_log_line(raw)
        lines.append(parsed)
        point = parse_epoch_metrics(parsed["message"])
        if point:
            series.append(point)
    return {"lines": lines, "series": series}
