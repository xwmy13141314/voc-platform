"""Offline regression checks for the desktop release."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TEST_DATA = tempfile.TemporaryDirectory(prefix="voc-v12-tests-")
os.environ["VOC_DATA_DIR"] = _TEST_DATA.name
os.environ["VOC_INSTANCE_TOKEN"] = "smoke-test-instance"

import crawler
import main
from database import create_job, get_job, get_stats, init_db, init_default_brands
from version import APP_VERSION


class RuntimeSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        init_default_brands()

    def test_native_json_runtime_matches_python(self) -> None:
        import jiter
        from jiter import from_json
        import pydantic_core

        self.assertTrue(from_json(b'{"ready":true}')["ready"])
        native_jiter = importlib.import_module("jiter.jiter")
        self.assertIn("cp312", Path(native_jiter.__file__).name)
        self.assertTrue(Path(pydantic_core.__file__).is_file())

    def test_database_and_stats_are_available(self) -> None:
        stats = get_stats()
        self.assertIsInstance(stats, dict)
        self.assertTrue((Path(_TEST_DATA.name) / "voc.db").is_file())

    def test_health_identifies_exact_instance_and_database(self) -> None:
        health = asyncio.run(main.api_health())
        self.assertEqual("ok", health["status"])
        self.assertEqual(APP_VERSION, health["version"])
        self.assertEqual("smoke-test-instance", health["instance_token"])
        self.assertTrue(health["jiter"])
        self.assertEqual(Path(_TEST_DATA.name).resolve() / "voc.db", Path(health["database_path"]))


class CrawlOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.original = crawler.PLATFORM_REGISTRY.get("smoke")

    def tearDown(self) -> None:
        if self.original is None:
            crawler.PLATFORM_REGISTRY.pop("smoke", None)
        else:
            crawler.PLATFORM_REGISTRY["smoke"] = self.original

    def test_search_failure_is_not_reported_as_success(self) -> None:
        def broken_search(keyword: str, limit: int):
            raise ConnectionError("offline regression")

        crawler.PLATFORM_REGISTRY["smoke"] = {
            "name": "Smoke",
            "search": broken_search,
            "comments": lambda *_args, **_kwargs: [],
            "comment_supported": False,
            "search_supported": True,
        }
        result = crawler.crawl_competitor("Smoke Brand", "query", platform="smoke")
        self.assertEqual("failed", result["status"])
        self.assertEqual("search", result["errors"][0]["stage"])

    def test_real_empty_search_has_explicit_empty_status(self) -> None:
        crawler.PLATFORM_REGISTRY["smoke"] = {
            "name": "Smoke",
            "search": lambda _keyword, limit: [],
            "comments": lambda *_args, **_kwargs: [],
            "comment_supported": False,
            "search_supported": True,
        }
        result = crawler.crawl_competitor("Empty Brand", "query", platform="smoke")
        self.assertEqual("empty", result["status"])
        self.assertEqual([], result["errors"])


class CrawlJobStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.original_crawler = main.crawl_competitor

    def tearDown(self) -> None:
        main.crawl_competitor = self.original_crawler

    def _run(self, result: dict) -> dict:
        main.crawl_competitor = lambda *_args, **_kwargs: result
        params = {
            "brand_name": "Job Brand",
            "search_keyword": "job query",
            "max_videos": 1,
            "platform": "youtube",
        }
        job_id = create_job("crawl", params)
        main._run_crawl_job(job_id, params)
        return get_job(job_id)

    def test_empty_crawl_finishes_as_empty(self) -> None:
        job = self._run({
            "status": "empty", "brand": "Job Brand", "videos_found": 0,
            "comments_extracted": 0, "new_comments": 0, "errors": [],
        })
        self.assertEqual("empty", job["status"])
        self.assertFalse(job["error"])

    def test_crawl_error_finishes_as_failed(self) -> None:
        job = self._run({
            "status": "failed", "brand": "Job Brand", "videos_found": 0,
            "comments_extracted": 0, "new_comments": 0,
            "errors": [{"stage": "search", "error": "network unavailable"}],
        })
        self.assertEqual("failed", job["status"])
        self.assertIn("network unavailable", job["error"])


if __name__ == "__main__":
    unittest.main()
