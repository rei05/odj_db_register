"""Google Drive の Secrets 設定とログ秘匿の回帰テスト。"""

from __future__ import annotations

import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from odj import crawl, drive  # noqa: E402


ROOT_ID = "rootFolderIdForTests123"
MASTER_ID = "masterSheetIdForTests456"


class DriveConfigurationTest(unittest.TestCase):
    def test_root_folder_url_is_read_from_environment(self) -> None:
        urls = (
            f"https://drive.google.com/drive/folders/{ROOT_ID}",
            f"https://drive.google.com/drive/u/0/folders/{ROOT_ID}?usp=drive_link",
        )
        for url in urls:
            with self.subTest(url=url), mock.patch.dict(
                os.environ, {drive.ROOT_FOLDER_URL_ENV: url}, clear=True
            ):
                self.assertEqual(drive.root_folder_id(), ROOT_ID)

    def test_master_db_url_is_read_from_environment(self) -> None:
        urls = (
            f"https://docs.google.com/spreadsheets/d/{MASTER_ID}",
            f"https://docs.google.com/spreadsheets/u/1/d/{MASTER_ID}/edit#gid=0",
        )
        for url in urls:
            with self.subTest(url=url), mock.patch.dict(
                os.environ, {drive.MASTER_DB_URL_ENV: url}, clear=True
            ):
                self.assertEqual(drive.master_db_id(), MASTER_ID)

    def test_missing_setting_has_a_clear_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, drive.ROOT_FOLDER_URL_ENV):
                drive.root_folder_id()
            with self.assertRaisesRegex(RuntimeError, drive.MASTER_DB_URL_ENV):
                drive.master_db_id()

    def test_invalid_urls_are_rejected_without_echoing_the_value(self) -> None:
        invalid_urls = (
            f"http://drive.google.com/drive/folders/{ROOT_ID}",
            f"https://example.com/drive/folders/{ROOT_ID}",
            f"https://drive.google.com/spreadsheets/d/{ROOT_ID}",
            "not-a-url-containing-sensitive-text",
        )
        for url in invalid_urls:
            with self.subTest(url=url), mock.patch.dict(
                os.environ, {drive.ROOT_FOLDER_URL_ENV: url}, clear=True
            ):
                with self.assertRaises(RuntimeError) as caught:
                    drive.root_folder_id()
                self.assertNotIn(url, str(caught.exception))

    def test_crawl_uses_configured_root_without_writing_it_to_manifest(self) -> None:
        url = f"https://drive.google.com/drive/folders/{ROOT_ID}"
        with (
            mock.patch.dict(
                os.environ, {drive.ROOT_FOLDER_URL_ENV: url}, clear=True
            ),
            mock.patch.object(drive, "list_folder", return_value=[]) as list_folder,
        ):
            manifest = crawl.crawl()

        list_folder.assert_called_once_with(ROOT_ID)
        self.assertNotIn("root_folder_id", manifest)

    def test_fetch_master_db_uses_configured_id(self) -> None:
        url = f"https://docs.google.com/spreadsheets/d/{MASTER_ID}/edit"
        expected = Path("/tmp/master.xlsx")
        with (
            mock.patch.dict(
                os.environ, {drive.MASTER_DB_URL_ENV: url}, clear=True
            ),
            mock.patch.object(drive, "fetch", return_value=expected) as fetch,
        ):
            actual = drive.fetch_master_db(Path("/tmp/cache"))

        self.assertEqual(actual, expected)
        item = fetch.call_args.args[0]
        self.assertEqual(item.id, MASTER_ID)
        self.assertEqual(item.mime, drive.GSHEET_MIME)

    def test_download_failure_does_not_expose_url(self) -> None:
        sensitive_url = f"https://drive.google.com/drive/folders/{ROOT_ID}"
        with mock.patch.object(
            drive.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                drive._get(sensitive_url, retries=1)

        self.assertNotIn(ROOT_ID, str(caught.exception))
        self.assertNotIn(sensitive_url, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_folder_parse_failure_does_not_expose_id(self) -> None:
        with mock.patch.object(drive, "_get", return_value=b"unexpected page"):
            with self.assertRaises(RuntimeError) as caught:
                drive.list_folder(ROOT_ID)

        self.assertNotIn(ROOT_ID, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
