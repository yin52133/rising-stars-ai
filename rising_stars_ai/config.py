"""配置文件读取：config/*.yml 只读，运行参数由命令行覆盖。"""

from __future__ import annotations

import functools
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


@functools.cache
def load(name: str) -> dict:
    with open(ROOT / "config" / f"{name}.yml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def taxonomy() -> dict:
    return load("taxonomy")


def sources() -> dict:
    return load("sources")
