"""sources.yaml'daki RSS kaynaklarinin hala calisip calismadigini kontrol eder.

collector.py'den bagimsizdir; veritabanina hicbir sey yazmaz. Her kaynagi
indirip HTTP durumunu ve feedparser ile ayristirilabilirligini kontrol eder.
Bozuk bir kaynak bulunursa sys.exit(1) ile cikar; bu sayede GitHub Actions
uzerinde calistirildiginda is akisi kirmizi (basarisiz) olarak isaretlenir
ve repo sahibine bildirim gider.

Kullanim:
    python check_sources.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import feedparser
import requests

from collector import SOURCES_PATH, USER_AGENT, load_sources

CHECK_TIMEOUT_SECONDS = 20


def check_source(source: dict[str, Any], timeout: int = CHECK_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Tek bir kaynagin RSS akisinin erisilebilir ve ayristirilabilir oldugunu kontrol eder.

    Args:
        source: sources.yaml'daki tek bir kaynak sozlugu ('name', 'url' iceren).
        timeout: Saniye cinsinden istek zaman asimi.

    Returns:
        (basarili_mi, mesaj) ikilisi. basarili_mi False ise mesaj hata aciklamasidir,
        True ise mesaj kac entry bulundugunu bildirir.
    """
    url = source.get("url", "")
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        return False, f"NETWORK: {exc}"

    feed = feedparser.parse(response.content)
    if getattr(feed, "bozo", False) and not feed.entries:
        reason = getattr(feed, "bozo_exception", "bilinmeyen XML hatasi")
        return False, f"PARSE: {reason}"

    if not feed.entries:
        return False, "PARSE: feed bos (0 entry)"

    return True, f"OK: {len(feed.entries)} entry"


def main() -> None:
    """Tum kaynaklari kontrol eder, insan-okur rapor basar ve bozuk kaynak varsa hata koduyla cikar."""
    sources = load_sources(SOURCES_PATH)
    print(f"=== Kaynak URL Kontrolu ({len(sources)} kaynak) ===\n")

    broken: list[tuple[str, str, str]] = []
    for source in sources:
        name = source.get("name", "?")
        url = source.get("url", "")
        ok, message = check_source(source)
        status = "OK  " if ok else "HATA"
        print(f"[{status}] {name}: {message}")
        if not ok:
            broken.append((name, url, message))

    print(f"\n{len(sources) - len(broken)}/{len(sources)} kaynak calisiyor.")

    if broken:
        print("\nBozuk kaynaklar (sources.yaml'da guncellenmesi gerekebilir):")
        for name, url, message in broken:
            print(f"  - {name} ({url}): {message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
