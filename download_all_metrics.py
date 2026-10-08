"""Download all Glassnode metrics available for an asset as CSV files.

Put ``GLASSNODE_API_KEY`` in the project ``.env`` file and run::

    python download_all_metrics.py --asset BTC

The script discovers CSV-capable metric paths from Glassnode's metadata API,
then stores one CSV per metric below ``metrics_csv/<asset>/``. Existing
non-empty files are skipped by default, so interrupted downloads can be resumed
safely.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import get_glassnode_api_key


API_BASE_URL = "https://api.glassnode.com/v1"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
RESERVED_PARAMETERS = {"a", "api_key", "f", "timestamp_format"}
METRIC_PATH_PATTERN = re.compile(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+")
API_KEY_QUERY_PATTERN = re.compile(r"(?i)(api_key=)[^&\s'\"]+")


class GlassnodeError(RuntimeError):
    """Raised when Glassnode cannot return a usable response."""


@dataclass(frozen=True)
class DownloadSummary:
    downloaded: int
    skipped: int
    failed: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover all Glassnode metrics available for an asset and save "
            "each metric as a CSV file."
        )
    )
    parser.add_argument(
        "--api-key",
        default=get_glassnode_api_key(),
        help="Glassnode API key (default: environment variable or project .env)",
    )
    parser.add_argument(
        "--asset",
        default="BTC",
        help="Glassnode asset ID (default: BTC)",
    )
    parser.add_argument(
        "--list-metrics",
        action="store_true",
        help="Save all available metric metadata as JSON without downloading data",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help=(
            "JSON destination for --list-metrics "
            "(default: <asset>_metrics.json)"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("metrics_csv"),
        help="Destination directory (default: metrics_csv)",
    )
    parser.add_argument(
        "--since",
        type=int,
        help="Optional start time as a Unix timestamp",
    )
    parser.add_argument(
        "--until",
        type=int,
        help="Optional end time as a Unix timestamp",
    )
    parser.add_argument(
        "--interval",
        help=(
            "Only download metrics supporting this resolution, such as "
            "10m, 1h, 24h, 1w, or 1month"
        ),
    )
    parser.add_argument(
        "--currency",
        help="Only download metrics supporting this currency, such as USD or NATIVE",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "Additional metric parameter; repeat for multiple values "
            "(for example --param e=binance)"
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.11,
        help="Seconds between metric requests (default: 0.11)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Retries for rate limits and temporary server errors (default: 4)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing CSV files instead of skipping them",
    )
    return parser


def parse_extra_parameters(values: Sequence[str]) -> dict[str, list[str]]:
    parameters: dict[str, list[str]] = {}
    for value in values:
        name, separator, parameter_value = value.partition("=")
        name = name.strip()
        parameter_value = parameter_value.strip()
        if not separator or not name or not parameter_value:
            raise ValueError(f"invalid --param {value!r}; expected NAME=VALUE")
        if name in RESERVED_PARAMETERS:
            raise ValueError(f"--param {name!r} is managed by a dedicated option")
        parameters.setdefault(name, []).append(parameter_value)
    return parameters


def _response_charset(headers: Message) -> str:
    return headers.get_content_charset() or "utf-8"


def _retry_delay(error: HTTPError, attempt: int) -> float:
    for header in ("Retry-After", "x-rate-limit-reset"):
        value = error.headers.get(header)
        if value:
            try:
                return min(max(float(value), 0.0), 120.0)
            except ValueError:
                pass
    return min(2**attempt, 30)


def require_api_key(api_key: str | None) -> str:
    resolved_api_key = (api_key or get_glassnode_api_key() or "").strip()
    if not resolved_api_key:
        raise GlassnodeError(
            "Glassnode API key is missing; set GLASSNODE_API_KEY in .env or the environment"
        )
    return resolved_api_key


def redact_api_key(value: object, api_key: str | None) -> str:
    redacted = str(value).replace(api_key, "<redacted>") if api_key else str(value)
    return API_KEY_QUERY_PATTERN.sub(r"\1<redacted>", redacted)


def request_bytes(
    endpoint: str,
    parameters: Mapping[str, Any],
    api_key: str | None,
    *,
    timeout: float,
    retries: int,
) -> tuple[bytes, Message]:
    query_parameters = dict(parameters)
    if api_key:
        query_parameters["api_key"] = api_key
    url = f"{API_BASE_URL}{endpoint}?{urlencode(query_parameters, doseq=True)}"
    request = Request(url, headers={"User-Agent": "glassnode-mass-downloader/0.1"})

    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace").strip()
            if error.code in RETRYABLE_STATUS_CODES and attempt < retries:
                time.sleep(_retry_delay(error, attempt))
                continue
            detail = redact_api_key(body[:500] if body else error.reason, api_key)
            raise GlassnodeError(f"HTTP {error.code}: {detail}") from None
        except URLError as error:
            if attempt < retries:
                time.sleep(min(2**attempt, 30))
                continue
            detail = redact_api_key(error.reason, api_key)
            raise GlassnodeError(f"network error: {detail}") from None

    raise AssertionError("retry loop ended unexpectedly")


def request_json(
    endpoint: str,
    parameters: Mapping[str, Any],
    api_key: str | None,
    *,
    timeout: float,
    retries: int,
) -> Any:
    body, headers = request_bytes(
        endpoint,
        parameters,
        api_key,
        timeout=timeout,
        retries=retries,
    )
    try:
        return json.loads(body.decode(_response_charset(headers)))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GlassnodeError(f"invalid JSON response: {error}") from None


def extract_metric_paths(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        payload = payload.get("data")
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise GlassnodeError("metadata response did not contain a list of metric paths")

    paths = []
    for item in payload:
        path = item.strip()
        if not METRIC_PATH_PATTERN.fullmatch(path) or any(
            part in {".", ".."} for part in path.split("/")
        ):
            raise GlassnodeError(f"metadata returned an invalid metric path: {item!r}")
        paths.append(path)
    return sorted(set(paths))


def get_available_metrics_for_asset(
    *,
    api_key: str | None = None,
    asset: str,
    delay: float = 0.11,
    timeout: float = 60.0,
    retries: int = 4,
    progress_stream: TextIO | None = None,
) -> list[dict[str, Any]]:
    """Return Glassnode metadata for every metric available to one asset."""
    if delay < 0:
        raise ValueError("delay must be zero or greater")
    asset = asset.upper()
    if progress_stream is not None:
        print(
            f"[list-metrics] discovering metrics for {asset} ...",
            file=progress_stream,
            flush=True,
        )
    metric_paths = extract_metric_paths(
        request_json(
            "/metadata/metrics",
            {"a": asset},
            api_key,
            timeout=timeout,
            retries=retries,
        )
    )
    total_metrics = len(metric_paths)
    if progress_stream is not None:
        print(
            f"[list-metrics] discovered {total_metrics} metrics for {asset}",
            file=progress_stream,
            flush=True,
        )
    metrics: list[dict[str, Any]] = []
    for index, metric_path in enumerate(metric_paths, start=1):
        metadata = request_json(
            "/metadata/metric",
            {"path": metric_path, "a": asset},
            api_key,
            timeout=timeout,
            retries=retries,
        )
        if not isinstance(metadata, dict):
            raise GlassnodeError(f"invalid metadata for metric {metric_path!r}")
        metrics.append(metadata)
        if progress_stream is not None and (
            index == 1 or index % 10 == 0 or index == total_metrics
        ):
            print(
                f"[list-metrics] resolved {index}/{total_metrics} metrics",
                file=progress_stream,
                flush=True,
            )
        if delay and index < total_metrics:
            time.sleep(delay)
    return metrics


def safe_asset_name(asset: str) -> str:
    safe_asset = re.sub(r"[^A-Za-z0-9_.-]+", "_", asset)
    if safe_asset in {"", ".", ".."}:
        return "asset_" + (safe_asset.replace(".", "_") or "unknown")
    return safe_asset


def metric_output_path(output_dir: Path, asset: str, metric_path: str) -> Path:
    if not METRIC_PATH_PATTERN.fullmatch(metric_path) or any(
        part in {".", ".."} for part in metric_path.split("/")
    ):
        raise GlassnodeError(f"invalid metric path: {metric_path!r}")
    parts = metric_path.strip("/").split("/")
    csv_path = Path(*parts[:-1], f"{parts[-1]}.csv")
    destination = output_dir / safe_asset_name(asset) / csv_path
    try:
        destination.resolve().relative_to(output_dir.resolve())
    except ValueError:
        raise GlassnodeError(f"metric output escaped its destination: {metric_path!r}") from None
    return destination


def validate_csv(body: bytes, headers: Message) -> None:
    try:
        text = body.decode(_response_charset(headers))
    except UnicodeDecodeError as error:
        raise GlassnodeError(f"CSV response is not valid text: {error}") from None

    content_type = headers.get_content_type()
    if content_type == "application/json" or text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text[:500]
        raise GlassnodeError(f"expected CSV but received JSON: {payload}")

    try:
        rows = csv.reader(io.StringIO(text), strict=True)
        first_row = next(rows, None)
        for _ in rows:
            pass
    except csv.Error as error:
        raise GlassnodeError(f"invalid CSV response: {error}") from None
    if not first_row:
        raise GlassnodeError("received an empty CSV response")


def write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".part")
    try:
        temporary_path.write_bytes(body)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_failures(path: Path, failures: Sequence[tuple[str, str]]) -> None:
    if not failures:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("metric", "error"))
        writer.writerows(failures)


def download_all_metrics(
    *,
    api_key: str | None = None,
    asset: str,
    output_dir: Path,
    since: int | None,
    until: int | None,
    interval: str | None,
    currency: str | None,
    extra_parameters: Mapping[str, Sequence[str]],
    delay: float,
    timeout: float,
    retries: int,
    overwrite: bool,
) -> DownloadSummary:
    api_key = require_api_key(api_key)
    common_parameters: dict[str, Any] = {"a": asset}
    common_parameters.update(extra_parameters)
    if interval:
        common_parameters["i"] = interval
    if currency:
        common_parameters["c"] = currency

    discovery_parameters = dict(common_parameters)
    discovery_parameters["f"] = "csv"
    print(f"Discovering CSV metrics available for {asset} ...")
    metadata = request_json(
        "/metadata/metrics",
        discovery_parameters,
        api_key,
        timeout=timeout,
        retries=retries,
    )
    metric_paths = extract_metric_paths(metadata)
    print(f"Found {len(metric_paths)} CSV-compatible metrics.")

    request_parameters = dict(common_parameters)
    request_parameters.update({"f": "csv", "timestamp_format": "humanized"})
    if since is not None:
        request_parameters["s"] = since
    if until is not None:
        request_parameters["u"] = until

    downloaded = skipped = 0
    failures: list[tuple[str, str]] = []
    for index, metric_path in enumerate(metric_paths, start=1):
        destination = metric_output_path(output_dir, asset, metric_path)
        if not overwrite and destination.exists() and destination.stat().st_size > 0:
            skipped += 1
            print(f"[{index}/{len(metric_paths)}] skip {metric_path}")
            continue

        print(f"[{index}/{len(metric_paths)}] download {metric_path}")
        try:
            body, headers = request_bytes(
                f"/metrics{metric_path}",
                request_parameters,
                api_key,
                timeout=timeout,
                retries=retries,
            )
            validate_csv(body, headers)
            write_atomic(destination, body)
            downloaded += 1
        except (GlassnodeError, OSError) as error:
            failures.append((metric_path, str(error)))
            print(f"  failed: {error}", file=sys.stderr)

        if delay and index < len(metric_paths):
            time.sleep(delay)

    failure_path = output_dir / safe_asset_name(asset) / "_errors.csv"
    write_failures(failure_path, failures)
    return DownloadSummary(downloaded, skipped, len(failures))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.list_metrics and not args.api_key:
        parser.error("provide --api-key or set GLASSNODE_API_KEY in the environment or .env")
    if args.delay < 0:
        parser.error("--delay must be zero or greater")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.retries < 0:
        parser.error("--retries must be zero or greater")
    if args.since is not None and args.until is not None and args.since > args.until:
        parser.error("--since cannot be later than --until")

    try:
        asset = args.asset.upper()
        if args.list_metrics:
            metrics = get_available_metrics_for_asset(
                api_key=args.api_key,
                asset=asset,
                delay=args.delay,
                timeout=args.timeout,
                retries=args.retries,
                progress_stream=sys.stderr,
            )
            output_path = args.output_json or Path(
                f"{safe_asset_name(asset).lower()}_metrics.json"
            )
            body = (json.dumps(metrics, indent=2, ensure_ascii=False) + "\n").encode(
                "utf-8"
            )
            write_atomic(output_path, body)
            print(
                f"Saved {len(metrics)} metric metadata records to {output_path}"
            )
            return 0

        extra_parameters = parse_extra_parameters(args.param)
        summary = download_all_metrics(
            api_key=args.api_key,
            asset=asset,
            output_dir=args.output_dir,
            since=args.since,
            until=args.until,
            interval=args.interval,
            currency=args.currency,
            extra_parameters=extra_parameters,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            overwrite=args.overwrite,
        )
    except (GlassnodeError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(
        f"Finished: {summary.downloaded} downloaded, "
        f"{summary.skipped} skipped, {summary.failed} failed."
    )
    if summary.failed:
        print("See _errors.csv in the asset output directory for details.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
