from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

import metrics_file_downloader as downloader


class MetricsFileDownloaderTests(TestCase):
    def test_cli_downloads_metrics_file_with_options(self) -> None:
        output = StringIO()
        downloaded_files = [Path("data/PUMP/metric_24h.parquet")]

        with patch.object(
            downloader, "get_glassnode_api_key", return_value="env-key"
        ), patch.object(
            downloader, "download_metrics_file", return_value=downloaded_files
        ) as download, redirect_stdout(output):
            exit_code = downloader.main(
                [
                    "metrics_list/pump_metrics.json",
                    "--data-dir",
                    "output",
                    "--timeout",
                    "30",
                    "--limit",
                    "5",
                ]
            )

        self.assertEqual(exit_code, 0)
        download.assert_called_once_with(
            Path("metrics_list/pump_metrics.json"),
            data_dir=Path("output"),
            api_key="env-key",
            timeout=30.0,
            limit=5,
        )
        self.assertIn("Downloaded 1 Parquet file", output.getvalue())

    def test_downloads_first_five_metrics_at_required_resolutions(self) -> None:
        project_root = Path(__file__).parents[1]
        metrics_file = project_root / "metrics_list" / "pump_metrics.json"

        with TemporaryDirectory() as directory:
            response = MagicMock()
            response.text = "timestamp,value\n2024-01-01,1\n"

            with patch.object(
                downloader.requests, "get", return_value=response
            ) as request, patch.object(
                downloader.time, "sleep"
            ) as sleep:
                written_files = downloader.download_metrics_file(
                    metrics_file,
                    data_dir=Path(directory) / "data",
                    api_key="test-key",
                    limit=5,
                )
            parquet_files_exist = all(path.is_file() for path in written_files)

        self.assertEqual(len(written_files), 7)
        self.assertEqual(request.call_count, 7)
        self.assertEqual(sleep.call_args_list, [((5,), {})] * 7)
        self.assertEqual(
            request.call_args_list[0].args[0],
            "https://api.glassnode.com/v1/metrics/addresses/active_count",
        )
        self.assertEqual(
            request.call_args_list[0].kwargs["params"],
            {"a": "PUMP", "f": "csv", "i": "24h", "api_key": "test-key"},
        )
        self.assertEqual(
            request.call_args_list[1].kwargs["params"],
            {"a": "PUMP", "f": "csv", "i": "10m", "api_key": "test-key"},
        )
        self.assertTrue(all(path.parent.name == "PUMP" for path in written_files))
        self.assertTrue(all(path.suffix == ".parquet" for path in written_files))
        self.assertEqual(
            written_files[:2],
            [
                Path(directory) / "data" / "PUMP" / "addresses__active_count_24h.parquet",
                Path(directory) / "data" / "PUMP" / "addresses__active_count_10m.parquet",
            ],
        )
        self.assertTrue(parquet_files_exist)
