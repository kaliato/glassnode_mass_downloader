# Glassnode Mass Downloader

Download every CSV-compatible Glassnode metric available for a selected asset.
The downloader discovers metric paths through Glassnode's metadata API instead
of relying on a hard-coded list, so newly available metrics can be picked up
automatically.

Each metric is stored in its own CSV file because Glassnode metrics can have
different columns and shapes.

## Requirements

- Python 3.13 or newer
- A Glassnode API key with API access

The command-line downloader uses only the Python standard library. The JSON-file
Parquet downloader uses `requests`, `pandas`, and `pyarrow`; run `uv sync` to
install all project dependencies.

## Quick start

Open the `.env` file in the repository root and add your Glassnode API key:

```dotenv
GLASSNODE_API_KEY=your-api-key
```

Then run the downloader from the repository root:

```console
python download_all_metrics.py --asset BTC
```

The `.env` file is excluded by `.gitignore` so the key is not accidentally
committed. Configuration precedence is:

1. The `--api-key` command-line option
2. The `GLASSNODE_API_KEY` process environment variable
3. `GLASSNODE_API_KEY` in the project `.env` file

Using `.env` or a process environment variable is recommended because a key
passed on the command line may be visible to other processes.

Other Python code in this project can use the same configuration source:

```python
from config import get_glassnode_api_key

api_key = get_glassnode_api_key()
```

## Output

By default, files are written below `metrics_csv/<asset>/` and grouped by the
metric path:

```text
metrics_csv/
`-- BTC/
    |-- addresses/
    |   `-- active_count.csv
    |-- market/
    |   `-- price_usd_close.csv
    `-- _errors.csv
```

`_errors.csv` is created only when one or more metrics fail. It records the
metric path and error so failed downloads can be investigated or retried.

Existing non-empty CSV files are skipped. This makes it safe to rerun the same
command after an interruption. Use `--overwrite` to download them again.

## Common examples

Download all CSV-compatible ETH metrics:

```console
python download_all_metrics.py --asset ETH
```

Download daily USD metrics only:

```console
python download_all_metrics.py --asset BTC --interval 24h --currency USD
```

Limit the requested time range with Unix timestamps:

```console
python download_all_metrics.py --asset BTC --since 1704067200 --until 1735689599
```

Write files to another directory:

```console
python download_all_metrics.py --asset BTC --output-dir data
```

Filter metrics using an additional Glassnode parameter. Repeat `--param` to
provide multiple values:

```console
python download_all_metrics.py --asset BTC --param e=binance --param e=coinbase
```

Replace files downloaded by an earlier run:

```console
python download_all_metrics.py --asset BTC --overwrite
```

Show every option:

```console
python download_all_metrics.py --help
```

## List available metrics without downloading data

Use `--list-metrics` to discover every metric available for one asset without
requesting metric data:

```console
uv run python download_all_metrics.py --asset PUMP --list-metrics
```

The command first requests:

```text
https://api.glassnode.com/v1/metadata/metrics?a=PUMP
```

It then requests the full metadata for every returned path. For example:

```text
https://api.glassnode.com/v1/metadata/metric?path=/addresses/active_count&a=PUMP
```

The command automatically uses `GLASSNODE_API_KEY` from the environment or
project `.env` file for these metadata requests. Glassnode metadata requests
do not consume metric API credits, but the current API gateway rejects
unauthenticated requests with HTTP 401. The complete metadata responses are
saved as a JSON array in `pump_metrics.json`. The default name is
`<asset>_metrics.json` in the current directory.

Progress is printed immediately before metric discovery, after discovery,
after the first metric, every 10 metrics, and when resolution finishes.
The final console message shows the output filename.

Use `--output-json` to choose a different destination:

```console
uv run python download_all_metrics.py --asset PUMP --list-metrics --output-json output/pump.json
```

```json
[
  {
    "path": "/addresses/active_count",
    "parameters": {
      "a": ["PUMP"],
      "i": ["1h", "24h"]
    },
    "queried": {
      "a": "PUMP"
    }
  }
]
```

The exact fields are supplied by Glassnode and can vary by metric. This mode
calls only the two metadata endpoints; it never calls a metric data endpoint.

The same behavior is available to Python callers:

```python
from download_all_metrics import get_available_metrics_for_asset

metrics = get_available_metrics_for_asset(
    asset="PUMP",
)
```

## Download a metrics JSON file to Parquet

Use `download_metrics_file` to download the metrics described by a JSON file
such as `metrics_list/pump_metrics.json`. Files are written below
`data/<asset>/`:

```python
from metrics_file_downloader import download_metrics_file

download_metrics_file(
    "metrics_list/pump_metrics.json",
    data_dir="data",
    limit=5,  # omit this argument to process the complete file
)
```

The function selects CSV parameters, downloads 24-hour data plus the minimum
available resolution (only once when the minimum is already 24-hour), appends
the resolution to each Parquet filename, and waits five seconds after every API
call.

The same function is available as a CLI command. The API key is read from
`GLASSNODE_API_KEY` or the project `.env` file:

```powershell
uv run glassnode-download-metrics-file metrics_list/pump_metrics.json --limit 5
```

Use `--data-dir` to change the output directory, `--timeout` to change the HTTP
timeout, or omit `--limit` to process the complete metrics file.

## Reliability behavior

- Requests are made sequentially with a small configurable delay.
- Rate-limit responses and temporary server errors are retried automatically.
- Numeric retry delays from `Retry-After` and Glassnode rate-limit reset headers
  are used, capped at 120 seconds.
- Each CSV is validated before it is atomically moved into place.
- A run exits with status `1` if any metric fails, while successful CSV files
  remain available.

Use `--delay`, `--timeout`, and `--retries` to adjust request behavior.

## API usage and credits

Downloading all metrics can make many API requests and consume significant API
credits. Optional filters such as `--interval`, `--currency`, and `--param`
reduce the discovered metric set to compatible endpoints. Review Glassnode's
[API credits and rate limits](https://docs.glassnode.com/basic-api/api-credits)
before running a large download.

Metric discovery uses Glassnode's
[metadata endpoints](https://docs.glassnode.com/basic-api/metadata), while each
CSV is fetched from its corresponding regular metric endpoint.

## Run the tests

```powershell
python -m unittest discover -s tests -v
```
