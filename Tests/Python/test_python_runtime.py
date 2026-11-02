"""Focused checks for the pinned Python runtime cache."""

import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import python_runtime
import release_build


PINNED_BUILD = "20261013"
PINNED_SHA256 = "994adadc202c5df0b4570192cc9a70e3f2c131e512f4a125c56efa8562f44b28"


def make_runtime_archive(base, version="3.14.8"):
    archive_source = base / "archive" / "python"
    bin_directory = archive_source / "bin"
    bin_directory.mkdir(parents=True)
    executable = bin_directory / "python3.14"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + version + "'\n", encoding="utf-8")
    executable.chmod(0o755)
    (bin_directory / "python3").symlink_to("python3.14")
    library = archive_source / "lib" / "python3.14"
    library.mkdir(parents=True)
    (library / "LICENSE.txt").write_text("license\n", encoding="utf-8")
    archive = base / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(archive_source, arcname="python")
    return archive


def product_for(archive, version="3.14.8"):
    checksum = python_runtime.sha256_file(archive)
    return {
        "python_version": version,
        "python_runtime": {
            "build": PINNED_BUILD,
            "url": "https://example.invalid/runtime.tar.gz",
            "sha256": checksum,
        },
    }


def copy_download(archive):
    def download(url, destination):
        shutil.copyfile(archive, destination)

    return download


def make_existing_runtime(root, build=PINNED_BUILD, version="3.14.8"):
    runtime = root / ".build" / "python" / "cpython-3.14.8-macos-aarch64-none"
    bin_directory = runtime / "bin"
    bin_directory.mkdir(parents=True)
    executable = bin_directory / "python3.14"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + version + "'\n", encoding="utf-8")
    executable.chmod(0o755)
    (bin_directory / "python3").symlink_to("python3.14")
    (runtime / "BUILD").write_text(build, encoding="utf-8")
    return runtime


class PinTests(unittest.TestCase):
    def test_manifest_pins_the_verified_upstream_archive(self):
        product = json.loads((ROOT / "product.json").read_text(encoding="utf-8"))
        self.assertEqual(product["python_version"], "3.14.8")
        pin = product["python_runtime"]
        self.assertEqual(pin["build"], PINNED_BUILD)
        self.assertEqual(pin["sha256"], PINNED_SHA256)
        self.assertIn("install_only_stripped", pin["url"])
        self.assertIn("20261013", pin["url"])
        self.assertIn("aarch64-apple-darwin", pin["url"])
        self.assertNotIn("python_version", pin)

    def test_manifest_keeps_the_existing_product_identity(self):
        product = json.loads((ROOT / "product.json").read_text(encoding="utf-8"))
        self.assertEqual(product["name"], "Goblin")
        self.assertEqual(product["vendor"], "MaverickKB")
        self.assertEqual(product["bundle_id"], "com.maverickkb.goblin")
        self.assertEqual(product["default_model"]["api_kind"], "chat_completions")
        self.assertEqual(product["oauth"]["xai"]["client_id"], "b1a00492-073a-47ea-816f-4c329264a828")
        self.assertEqual(product["native_helpers"]["computer_use"]["name"], "GoblinCUA")

    def test_cache_path_matches_the_existing_directory_name(self):
        product = json.loads((ROOT / "product.json").read_text(encoding="utf-8"))
        expected = (
            ROOT.resolve()
            / ".build"
            / "python"
            / "cpython-3.14.8-macos-aarch64-none"
        )
        self.assertEqual(python_runtime.runtime_cache_path(ROOT, product), expected)


class RuntimeCacheTests(unittest.TestCase):
    def test_existing_cache_is_reused_without_a_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "app"
            runtime = make_existing_runtime(root)
            product = {
                "python_version": "3.14.8",
                "python_runtime": {
                    "build": PINNED_BUILD,
                    "url": "https://example.invalid/runtime.tar.gz",
                    "sha256": PINNED_SHA256,
                },
            }
            with mock.patch.object(python_runtime, "download_file", side_effect=AssertionError):
                result = python_runtime.ensure_python_runtime(root, product)
            self.assertEqual(result, runtime.resolve())
            self.assertEqual((runtime / "BUILD").read_text(encoding="utf-8"), PINNED_BUILD)

    def test_pinned_archive_is_downloaded_verified_and_extracted(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = make_runtime_archive(base)
            root = base / "app"
            product = product_for(archive)
            with mock.patch.object(python_runtime, "download_file", side_effect=copy_download(archive)):
                runtime = python_runtime.ensure_python_runtime(root, product)
            self.assertTrue(runtime.is_dir())
            self.assertEqual((runtime / "BUILD").read_text(encoding="utf-8"), PINNED_BUILD)
            self.assertTrue((runtime / "bin" / "python3").is_file())
            self.assertTrue((runtime / "lib" / "python3.14" / "LICENSE.txt").is_file())
            with mock.patch.object(python_runtime, "download_file", side_effect=AssertionError):
                second = python_runtime.ensure_python_runtime(root, product)
            self.assertEqual(second, runtime)

    def test_checksum_mismatch_does_not_create_a_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = make_runtime_archive(base)
            root = base / "app"
            product = product_for(archive)
            product["python_runtime"]["sha256"] = "0" * 64
            with mock.patch.object(python_runtime, "download_file", side_effect=copy_download(archive)):
                with self.assertRaises(python_runtime.PythonRuntimeError):
                    python_runtime.ensure_python_runtime(root, product)
            runtime = python_runtime.runtime_cache_path(root, product)
            self.assertFalse(runtime.exists())

    def test_wrong_build_marker_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "app"
            runtime = make_existing_runtime(root, build="20250101")
            product = {
                "python_version": "3.14.8",
                "python_runtime": {
                    "build": PINNED_BUILD,
                    "url": "https://example.invalid/runtime.tar.gz",
                    "sha256": PINNED_SHA256,
                },
            }
            with mock.patch.object(python_runtime, "download_file", side_effect=AssertionError):
                with self.assertRaises(python_runtime.PythonRuntimeError):
                    python_runtime.ensure_python_runtime(root, product)
            self.assertEqual((runtime / "BUILD").read_text(encoding="utf-8"), "20250101")

    def test_unsafe_archive_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = base / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("../escape.txt")
                payload = b"escape\n"
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
            with self.assertRaises(python_runtime.PythonRuntimeError):
                python_runtime.extract_archive(archive, base / "output")
            self.assertFalse((base / "escape.txt").exists())


class NoticeAndLicenseTests(unittest.TestCase):
    def test_notices_name_every_redistributed_static_component(self):
        notice = (ROOT / "Runtime" / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
        names = (
            "CPython 3.14.8",
            "OpenSSL 3.5.9",
            "SQLite 3.53.1",
            "Expat 2.9.0",
            "libffi 3.8.0",
            "bzip2 1.0.8",
            "liblzma 5.8.4",
            "mpdecimal 4.0.0",
            "zstd 1.5.7",
            "libuuid 1.0.3",
            "HACL*",
        )
        for name in names:
            self.assertIn(name, notice)

    def test_notices_distinguish_system_libraries_and_pruned_components(self):
        notice = (ROOT / "Runtime" / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
        self.assertIn("Apple-provided dynamic libraries", notice)
        self.assertIn("libedit", notice)
        self.assertIn("editline", notice)
        self.assertIn("GNU Readline", notice)
        self.assertIn("Pruned components", notice)
        self.assertIn("Tcl 9.0.4", notice)
        self.assertIn("pip 26.2.1", notice)
        self.assertIn("idle", notice)

    def test_notice_versions_match_the_release_probe(self):
        notice = (ROOT / "Runtime" / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
        release_build.verify_notice_versions(notice, {
            "python": "3.14.8",
            "openssl": "OpenSSL 3.5.9 29 Sep 2026",
            "sqlite": "3.53.1",
        })

    def test_every_referenced_license_file_exists_and_has_text(self):
        notice = (ROOT / "Runtime" / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
        expected = {
            "CPython-3.14.8.txt": "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2",
            "OpenSSL-3.5.9.txt": "Apache License",
            "SQLite-3.53.1.txt": "public domain",
            "Expat-2.9.0.txt": "Permission is hereby granted",
            "libffi-3.8.0.txt": "Permission is hereby granted",
            "bzip2-1.0.8.txt": "Redistribution and use in source and binary forms",
            "liblzma-5.8.4.txt": "Permission to use, copy, modify",
            "mpdecimal-4.0.0.txt": "Redistribution and use in source and binary forms",
            "zstd-1.5.7.txt": "BSD License",
            "libuuid-1.0.3.txt": "Redistribution and use in source and binary forms",
            "HACL-star-8ba599b2f6c9.txt": "Apache License",
        }
        for filename, phrase in expected.items():
            relative = "Runtime/THIRD_PARTY_LICENSES/" + filename
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            text = path.read_text(encoding="utf-8")
            self.assertGreater(len(text), 500, relative)
            self.assertIn(phrase, text, relative)
            self.assertIn(relative, notice)

    def test_helper_has_no_global_installer_or_home_path(self):
        source = (ROOT / "scripts" / "python_runtime.py").read_text(encoding="utf-8")
        for forbidden in ("uv python install", "expanduser", "Path.home", "/Users/"):
            self.assertNotIn(forbidden, source)
        self.assertIn(".build", source)


if __name__ == "__main__":
    unittest.main()
