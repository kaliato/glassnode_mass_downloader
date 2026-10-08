from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import download_all_metrics as downloader
import config


def csv_headers() -> Message:
    headers = Message()
    headers["Content-Type"] = "text/csv; charset=utf-8"
    return headers


class DownloadAllMetricsTests(TestCase):
    def test_download_function_loads_api_key_from_env_file(self) -> None:
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("GLASSNODE_API_KEY=file-key\n", encoding="utf-8")

            with patch.object(config, "DEFAULT_ENV_FILE", env_file), patch.dict(
                os.environ,
                {"GLASSNODE_API_KEY": ""},
            ), patch.object(downloader, "request_json", return_value=[]) as request_json:
                summary = downloader.download_all_metrics(
                    api_key=None,
                    asset="BTC",
                    output_dir=Path(directory) / "output",
                    since=None,
                    until=None,
                    interval=None,
                    currency=None,
                    extra_parameters={},
                    delay=0,
                    timeout=1,
                    retries=0,
                    overwrite=False,
                )

        self.assertEqual(summary, downloader.DownloadSummary(0, 0, 0))
        self.assertEqual(request_json.call_args.args[2], "file-key")

    def test_http_error_redacts_api_key(self) -> None:
        error = downloader.HTTPError(
            "https://api.glassnode.com/test?api_key=top-secret",
            401,
            "Unauthorized",
            Message(),
            BytesIO(b"request failed for api_key=top-secret"),
        )

        with patch.object(downloader, "urlopen", side_effect=error):
            with self.assertRaises(downloader.GlassnodeError) as raised:
                downloader.request_bytes(
                    "/metadata/metrics",
                    {"a": "BTC"},
                    "top-secret",
                    timeout=1,
                    retries=0,
                )

        self.assertNotIn("top-secret", str(raised.exception))
        self.assertIn("api_key=<redacted>", str(raised.exception))

    def test_get_available_metrics_uses_an_optional_api_key_for_metadata(self) -> None:
        progress = StringIO()
        metric_metadata = [
            {
                "path": "/addresses/active_count",
                "parameters": {"a": ["PUMP"]},
                "queried": {"a": "PUMP"},
            },
            {
                "path": "/market/price_usd_close",
                "parameters": {"a": ["PUMP"], "i": ["24h"]},
                "queried": {"a": "PUMP"},
            },
        ]
        with patch.object(
            downloader,
            "request_json",
            side_effect=[
                ["/market/price_usd_close", "/addresses/active_count"],
                *metric_metadata,
            ],
        ) as request_json:
            metrics = downloader.get_available_metrics_for_asset(
                api_key="metadata-key",
                asset="PUMP",
                delay=0,
                progress_stream=progress,
                timeout=1,
                retries=0,
            )

        self.assertEqual(
            metrics,
            metric_metadata,
        )
        self.assertEqual(
            [request.args[:3] for request in request_json.call_args_list],
            [
                ("/metadata/metrics", {"a": "PUMP"}, "metadata-key"),
                (
                    "/metadata/metric",
                    {"path": "/addresses/active_count", "a": "PUMP"},
                    "metadata-key",
                ),
                (
                    "/metadata/metric",
                    {"path": "/market/price_usd_close", "a": "PUMP"},
                    "metadata-key",
                ),
            ],
        )
        self.assertEqual(
            progress.getvalue().splitlines(),
            [
                "[list-metrics] discovering metrics for PUMP ...",
                "[list-metrics] discovered 2 metrics for PUMP",
                "[list-metrics] resolved 1/2 metrics",
                "[list-metrics] resolved 2/2 metrics",
            ],
        )

    def test_list_metrics_mode_uses_env_key_and_saves_json_without_downloading(self) -> None:
        available_metrics = [
            {
                "path": "/addresses/count",
                "parameters": {"a": ["PUMP"]},
                "queried": {"a": "PUMP"},
            }
        ]
        stdout = StringIO()
        stderr = StringIO()

        def list_metrics_with_progress(**kwargs: object) -> list[dict[str, object]]:
            print(
                "[list-metrics] resolved 1/1 metrics",
                file=kwargs["progress_stream"],
                flush=True,
            )
            return available_metrics

        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "pump.json"
            with patch.object(
                downloader,
                "get_glassnode_api_key",
                return_value="file-key",
            ), patch.object(
                downloader,
                "get_available_metrics_for_asset",
                side_effect=list_metrics_with_progress,
            ) as get_metrics, patch.object(
                downloader, "download_all_metrics"
            ) as download, redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = downloader.main(
                    [
                        "--asset",
                        "pump",
                        "--list-metrics",
                        "--output-json",
                        str(output_path),
                    ]
                )

            saved_metrics = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_metrics, available_metrics)
        self.assertIn(f"Saved 1 metric metadata records to {output_path}", stdout.getvalue())
        self.assertEqual(
            stderr.getvalue(), "[list-metrics] resolved 1/1 metrics\n"
        )
        self.assertEqual(get_metrics.call_args.kwargs["api_key"], "file-key")
        download.assert_not_called()

    def test_extract_metric_paths_accepts_wrapped_data_and_deduplicates(self) -> None:
        payload = {"data": ["/market/price_usd_close", "/addresses/count", "/addresses/count"]}

        self.assertEqual(
            downloader.extract_metric_paths(payload),
            ["/addresses/count", "/market/price_usd_close"],
        )

    def test_parse_extra_parameters_preserves_repeated_values(self) -> None:
        self.assertEqual(
            downloader.parse_extra_parameters(["e=binance", "e=coinbase"]),
            {"e": ["binance", "coinbase"]},
        )

    def test_downloads_each_discovered_metric_to_a_nested_csv(self) -> None:
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            responses = [
                (["/addresses/count", "/market/price_usd_close"], csv_headers()),
                (b"timestamp,value\n2024-01-01,1\n", csv_headers()),
                (b"timestamp,value\n2024-01-01,2\n", csv_headers()),
            ]

            with patch.object(
                downloader,
                "request_json",
                return_value=responses[0][0],
            ) as request_json, patch.object(
                downloader, "request_bytes", side_effect=responses[1:]
            ) as request_bytes:
                summary = downloader.download_all_metrics(
                    api_key="secret",
                    asset="BTC",
                    output_dir=output_dir,
                    since=None,
                    until=None,
                    interval="24h",
                    currency="USD",
                    extra_parameters={},
                    delay=0,
                    timeout=1,
                    retries=0,
                    overwrite=False,
                )

            self.assertEqual(summary, downloader.DownloadSummary(2, 0, 0))
            self.assertTrue((output_dir / "BTC" / "addresses" / "count.csv").is_file())
            self.assertTrue((output_dir / "BTC" / "market" / "price_usd_close.csv").is_file())
            self.assertEqual(request_bytes.call_count, 2)
            self.assertEqual(request_json.call_args.args[1]["f"], "csv")

    def test_existing_non_empty_metric_is_skipped(self) -> None:
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            destination = output_dir / "BTC" / "addresses" / "count.csv"
            destination.parent.mkdir(parents=True)
            destination.write_text("timestamp,value\n", encoding="utf-8")

            with patch.object(
                downloader,
                "request_json",
                return_value=["/addresses/count"],
            ), patch.object(downloader, "request_bytes") as request_bytes:
                summary = downloader.download_all_metrics(
                    api_key="secret",
                    asset="BTC",
                    output_dir=output_dir,
                    since=None,
                    until=None,
                    interval=None,
                    currency=None,
                    extra_parameters={},
                    delay=0,
                    timeout=1,
                    retries=0,
                    overwrite=False,
                )

            self.assertEqual(summary, downloader.DownloadSummary(0, 1, 0))
            request_bytes.assert_not_called()

    def test_asset_name_cannot_escape_output_directory(self) -> None:
        destination = downloader.metric_output_path(
            Path("metrics_csv"),
            "..",
            "/addresses/count",
        )

        self.assertEqual(destination, Path("metrics_csv/asset___/addresses/count.csv"))

    def test_metric_filename_keeps_an_existing_suffix(self) -> None:
        destination = downloader.metric_output_path(
            Path("metrics_csv"),
            "BTC",
            "/market/value.v2",
        )

        self.assertEqual(destination, Path("metrics_csv/BTC/market/value.v2.csv"))

    def test_rejects_metric_paths_with_query_characters(self) -> None:
        with self.assertRaises(downloader.GlassnodeError):
            downloader.extract_metric_paths(["/addresses/count?f=json"])

    def test_rejects_malformed_csv_after_the_header(self) -> None:
        with self.assertRaises(downloader.GlassnodeError):
            downloader.validate_csv(b'header\n"unterminated', csv_headers())
