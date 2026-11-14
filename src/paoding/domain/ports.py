"""Stable contracts for replacing collectors and model vendors without changing services."""

from typing import Protocol

from paoding.domain.schemas import Analysis, CollectorItem, CollectorPage, NormalizedContent


class SourceFetcher(Protocol):
    def fetch(self, url: str) -> tuple[bytes, str, str]: ...


class Collector(Protocol):
    def resolve(self, url: str) -> CollectorItem: ...

    def account_items(self, url: str, cursor: str | None, cold_start: bool) -> CollectorPage: ...


class Analyzer(Protocol):
    def analyze(
        self, content: NormalizedContent, instruction: str = ""
    ) -> tuple[Analysis, dict]: ...
