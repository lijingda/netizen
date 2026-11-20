"""Prepared prompt values shared by Channel input preparation and SDK rendering."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PromptImage:
    label: str
    data_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedPrompt:
    text: str
    images: tuple[PromptImage, ...] = ()
