"""What in a closed ticket is not somebody writing something.

Judged by shape rather than by subject text: their feedback request is English
today and could be Arabic tomorrow, but it will still be a job posting on the
internal channel with From set to the literal string "Customer".

Seen on ticket 2025050882000651: 2,954 articles, of which 2,715 were that
feedback job and 222 were OTRS notifications. Fourteen were real.
"""

from __future__ import annotations


def is_noise(article: dict) -> bool:
    """True when the article was generated rather than written."""
    if (article.get("From") or "").strip() == "Customer":
        return True
    if article.get("SenderType") == "system":
        return True
    return False


def real_articles(articles: list[dict]) -> list[dict]:
    return [a for a in articles if not is_noise(a)]