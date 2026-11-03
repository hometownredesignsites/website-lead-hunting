"""Shared rules for automatically routing candidates to visual review."""
from __future__ import annotations


def hero_media_review_trigger(row: dict) -> bool:
    hero = row.get("hero_media")
    if not isinstance(hero, dict):
        evidence = row.get("evidence") or {}
        desktop = evidence.get("desktop") or {}
        hero = desktop.get("heroMedia") or {}
    return isinstance(hero, dict) and hero.get("status") in {"absent", "uncertain"}


def is_visual_review_eligible(row: dict) -> bool:
    """Include technical High/Medium candidates and unclear/absent hero media."""
    return row.get("automated_priority") in {"high", "medium"} or hero_media_review_trigger(row)
