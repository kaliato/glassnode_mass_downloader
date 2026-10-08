"""Download every Glassnode metric described by a metrics JSON file."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import requests

from .config import get_glassnode_api_key


GLASSNODE_API_BASE_URL = "https://api.glassnode.com"
REQUEST_DELAY_SECONDS = 5
RESOLUTION_ORDER = ("10m", "1h", "24h", "1w", "1month")


def download_metrics_file(
    metrics_file: str | Path,
    *,
    data_dir: str | Path = "data",
    api_key: str | None = None,
    timeout: float = 60,
    limit: int | None = None,
) -> list[Path]:
    """Download all metrics in *metrics_file* and save them as Parquet files.

    Each JSON item must contain ``path`` and ``parameters``. For example::

        {
            "path": "/v1/metrics/market/price_usd_close",
            "parameters": {"a": "PUMP", "f": "csv", "i": "24h"}
        }

    The asset parameter (``a``) determines the output directory. The example
    above is written below ``data/PUMP``. The API key is read from
    ``GLASSNODE_API_KEY`` when it is not passed explicitly.

    For each metric, the 24-hour resolution and the minimum available
    resolution are downloaded. If both resolutions are the same, only one
    request is made. ``limit`` can be used to process only the first N metrics,
    which is useful for validating a metrics file before downloading the
    complete list.

    Returns a list containing every Parquet path written by this call.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be zero or greater")

    metrics_path = Path(metrics_file)
    with metrics_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    metrics = payload.get("metrics", payload) if isinstance(payload, dict) else payload
    if not isinstance(metrics, list):
        raise ValueError("Metrics JSON must be a list or an object with a 'metrics' list")

    resolved_api_key = api_key or os.getenv("GLASSNODE_API_KEY")
    if not resolved_api_key:
        raise ValueError(
            "A Glassnode API key is required. Pass api_key or set GLASSNODE_API_KEY."
        )

    output_root = Path(data_dir)
    written_files: list[Path] = []

    metrics_to_download = metrics[:limit] if limit is not None else metrics
    for index, metric in enumerate(metrics_to_download):
        if not isinstance(metric, dict):
            raise ValueError(f"Metric at index {index} must be a JSON object")

        path = metric.get("path")
        parameters = metric.get("parameters")
        if not isinstance(path, str) or not path:
            raise ValueError(f"Metric at index {index} has no valid 'path'")
        if not isinstance(parameters, Mapping):
            raise ValueError(f"Metric at index {index} has no valid 'parameters' object")

        for resolution in _resolutions_to_download(parameters):
            param_dict = _request_parameters(parameters, resolution=resolution)
            asset = param_dict.get("a")
            if not isinstance(asset, str) or not asset:
                raise ValueError(f"Metric at index {index} has no asset parameter 'a'")

            request_params = {**param_dict, "api_key": resolved_api_key}
            url = _metric_url(path)

            try:
                response = requests.get(url, params=request_params, timeout=timeout)
                response.raise_for_status()
            finally:
                time.sleep(REQUEST_DELAY_SECONDS)

            frame = pd.read_csv(io.StringIO(response.text))

            asset_dir = output_root / _safe_filename(asset)
            asset_dir.mkdir(parents=True, exist_ok=True)
            resolution_suffix = f"_{resolution}" if resolution else ""
            output_path = asset_dir / (
                f"{_metric_filename(path)}{resolution_suffix}.parquet"
            )
            frame.to_parquet(output_path, index=False)
            written_files.append(output_path)

    return written_files


def _resolutions_to_download(parameters: Mapping[str, Any]) -> list[str | None]:
    """Return 24h and the minimum available resolution without duplicates."""
    value = parameters.get("i")
    if isinstance(value, (list, tuple)):
        available = list(dict.fromkeys(item for item in value if item in RESOLUTION_ORDER))
    elif isinstance(value, str):
        available = [value]
    else:
        available = []

    if not available:
        return [None]

    minimum = min(
        available,
        key=lambda resolution: (
            RESOLUTION_ORDER.index(resolution)
            if resolution in RESOLUTION_ORDER
            else len(RESOLUTION_ORDER)
        ),
    )
    resolutions: list[str] = []
    if "24h" in available:
        resolutions.append("24h")
    if minimum not in resolutions:
        resolutions.append(minimum)
    return resolutions


def _request_parameters(
    parameters: Mapping[str, Any],
    *,
    resolution: str | None = None,
) -> dict[str, Any]:
    """Choose one usable value from each parameter option in metric metadata."""
    selected: dict[str, Any] = {}
    for name, value in parameters.items():
        if name == "i" and resolution is not None:
            selected[name] = resolution
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                raise ValueError(f"Parameter {name!r} must contain at least one value")
            if name == "f" and "csv" in value:
                selected[name] = "csv"
            elif name == "i" and "24h" in value:
                selected[name] = "24h"
            else:
                selected[name] = value[0]
        else:
            selected[name] = value
    return selected


def _metric_url(path: str) -> str:
    """Resolve a metadata path to a Glassnode metric endpoint URL."""
    if path.startswith(("http://", "https://")):
        return path
    normalized_path = path.lstrip("/")
    if normalized_path.startswith("v1/"):
        return f"{GLASSNODE_API_BASE_URL}/{normalized_path}"
    return f"{GLASSNODE_API_BASE_URL}/v1/metrics/{normalized_path}"


def _metric_filename(path: str) -> str:
    """Create a stable filename while retaining enough path to avoid collisions."""
    path_without_query = path.split("?", 1)[0].rstrip("/")
    metric_path = path_without_query.split("/metrics/", 1)[-1]
    return _safe_filename(metric_path.replace("/", "__")) or "metric"


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download metrics from a Glassnode metrics JSON file as Parquet."
    )
    parser.add_argument(
        "metrics_file",
        type=Path,
        help="Metrics JSON file, for example metrics_list/pump_metrics.json",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Output directory (default: data)",
    )
    parser.add_argument(
        "--api-key",
        help="Glassnode API key (default: GLASSNODE_API_KEY or project .env)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process only the first N metrics",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be zero or greater")

    try:
        written_files = download_metrics_file(
            args.metrics_file,
            data_dir=args.data_dir,
            api_key=args.api_key or get_glassnode_api_key(),
            timeout=args.timeout,
            limit=args.limit,
        )
    except (OSError, ValueError, requests.RequestException) as error:
        parser.error(str(error))

    file_word = "file" if len(written_files) == 1 else "files"
    print(f"Downloaded {len(written_files)} Parquet {file_word}:")
    for written_file in written_files:
        print(written_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


