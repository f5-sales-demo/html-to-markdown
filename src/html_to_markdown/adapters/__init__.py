"""Built-in source adapters."""

from collections.abc import Callable

from .base import SourceAdapter
from .community import CommunityAdapter
from .docs_cloud import DocsCloudAdapter
from .my_f5 import MyF5Adapter
from .www_f5 import WwwF5Adapter

ADAPTERS: dict[str, Callable[[], SourceAdapter]] = {
    "community-f5-com": CommunityAdapter,
    "docs-cloud-f5-com": DocsCloudAdapter,
    "my-f5-com": MyF5Adapter,
    "www-f5-com": WwwF5Adapter,
}

__all__ = ["ADAPTERS", "DocsCloudAdapter", "MyF5Adapter", "WwwF5Adapter"]
