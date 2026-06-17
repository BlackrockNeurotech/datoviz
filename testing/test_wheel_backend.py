from __future__ import annotations

import csv
import hashlib
import zipfile
from base64 import urlsafe_b64encode
from io import StringIO
from pathlib import Path

import pytest

from tools.datoviz_build_backend.config import parse_config_settings
from tools.datoviz_build_backend.manifest import PayloadEntry, write_manifest
from tools.datoviz_build_backend.wheel import write_wheel_from_stage
from tools.datoviz_build_backend.validate import validate_wheel


def _write_project(root: Path) -> None:
    (root / "README.md").write_text("# Datoviz test\n", encoding="utf8")
    (root / "pyproject.toml").write_text(
        """
[project]
name = "datoviz"
version = "0.4.0.dev0"
requires-python = ">=3.10"
dependencies = ["numpy"]
description = "test"
readme = "README.md"
license = { text = "MIT" }

[project.scripts]
datoviz-config = "datoviz.cli:main"
""".lstrip(),
        encoding="utf8",
    )


def test_parse_release_config_namespaced(tmp_path: Path) -> None:
    _write_project(tmp_path)
    config = parse_config_settings(
        {
            "datoviz.release-wheel": "true",
            "datoviz.platform-tag": "manylinux_2_34_x86_64",
            "datoviz.native-build-dir": "native",
            "datoviz.include-qtbridge": "yes",
            "datoviz.skip-repair": "1",
        },
        root=tmp_path,
    )

    assert config.release_wheel is True
    assert config.platform_tag == "manylinux_2_34_x86_64"
    assert config.native_build_dir == tmp_path / "native"
    assert config.include_qtbridge is True
    assert config.skip_repair is True


def test_parse_config_rejects_unknown_datoviz_setting(tmp_path: Path) -> None:
    _write_project(tmp_path)
    with pytest.raises(ValueError, match="unknown Datoviz"):
        parse_config_settings({"datoviz.unknown": "1"}, root=tmp_path)


def test_direct_wheel_writer_validates_record_and_manifest(tmp_path: Path) -> None:
    _write_project(tmp_path)
    stage = tmp_path / "stage"
    package = stage / "datoviz"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.4.0.dev0'\n", encoding="utf8")
    (package / "cli.py").write_text("def main(): return 0\n", encoding="utf8")
    manifest = package / "_wheel_payload.json"
    write_manifest(
        [
            PayloadEntry(
                source=str(package / "__init__.py"),
                wheel_path="datoviz/__init__.py",
                kind="python",
                required=True,
                reason="python-package",
            ),
            PayloadEntry(
                source=str(package / "cli.py"),
                wheel_path="datoviz/cli.py",
                kind="python",
                required=True,
                reason="python-package",
            ),
            PayloadEntry(
                source=str(manifest),
                wheel_path="datoviz/_wheel_payload.json",
                kind="metadata",
                required=True,
                reason="payload-manifest",
            ),
        ],
        manifest,
    )

    wheel = write_wheel_from_stage(
        stage,
        tmp_path / "dist",
        "manylinux_2_34_x86_64",
        root=tmp_path,
    )

    validate_wheel(wheel)
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    assert "datoviz-0.4.0.dev0.dist-info/METADATA" in names
    assert "datoviz-0.4.0.dev0.dist-info/RECORD" in names
    assert "datoviz/_wheel_payload.json" in names


def test_validate_accepts_repair_added_filename_tags(tmp_path: Path) -> None:
    _write_project(tmp_path)
    stage = tmp_path / "stage"
    package = stage / "datoviz"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.4.0.dev0'\n", encoding="utf8")
    manifest = package / "_wheel_payload.json"
    write_manifest(
        [
            PayloadEntry(
                source=str(package / "__init__.py"),
                wheel_path="datoviz/__init__.py",
                kind="python",
                required=True,
                reason="python-package",
            ),
            PayloadEntry(
                source=str(manifest),
                wheel_path="datoviz/_wheel_payload.json",
                kind="metadata",
                required=True,
                reason="payload-manifest",
            ),
        ],
        manifest,
    )
    wheel = write_wheel_from_stage(stage, tmp_path / "dist", "manylinux_2_34_x86_64", root=tmp_path)
    repaired = wheel.with_name(
        wheel.name.replace("manylinux_2_34_x86_64.whl", "manylinux_2_34_x86_64.manylinux_2_39_x86_64.whl")
    )

    records: list[tuple[str, bytes]] = []
    record_name = "datoviz-0.4.0.dev0.dist-info/RECORD"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(repaired, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename == record_name:
                continue
            data = source.read(info.filename)
            if info.filename.endswith(".dist-info/WHEEL"):
                data = data.replace(
                    b"Tag: py3-none-manylinux_2_34_x86_64\n",
                    b"Tag: py3-none-manylinux_2_34_x86_64\n"
                    b"Tag: py3-none-manylinux_2_39_x86_64\n",
                )
            target.writestr(info, data)
            records.append((info.filename, data))
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer, lineterminator="\n")
        for filename, data in records:
            digest = urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            writer.writerow([filename, f"sha256={digest}", str(len(data))])
        writer.writerow([record_name, "", ""])
        target.writestr(record_name, csv_buffer.getvalue())

    validate_wheel(repaired)
