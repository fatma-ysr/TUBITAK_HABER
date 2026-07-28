"""RSS toplayici: Turkce ekonomi haberi RSS akislarini toplayip SQLite'a yazar.

TUBITAK 2209-A projesi icin veri toplama katmani. Skorlama/modelleme bu
repo'nun kapsami disindadir; yalnizca baslik, ozet ve metaveri toplanir.

Kullanim:
    python collector.py                    # tum kaynaklari topla
    python collector.py --dry-run          # hicbir sey yazmadan test et
    python collector.py --source "Foreks"  # tek kaynagi test et
    python collector.py --stats            # veritabani ozetini goster
"""

from __future__ import annotations

import argparse
import hashlib
import html
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import pytz
import requests
import yaml
from dateutil import parser as dateutil_parser

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "haberler.db"
SOURCES_PATH = BASE_DIR / "sources.yaml"
LOG_PATH = BASE_DIR / "logs" / "collect.log"

TR_TZ = pytz.timezone("Europe/Istanbul")
FETCH_TIMEOUT_SECONDS = 20
USER_AGENT = (
    "Mozilla/5.0 (compatible; TUBITAK-2209A-RSS-Collector/1.0; "
    "+https://github.com/)"
)
UTM_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAMS = {"fbclid", "gclid"}

logger = logging.getLogger("collector")


@dataclass
class SourceResult:
    """Tek bir kaynagin bu calismadaki toplama sonucu."""

    name: str
    yeni: int = 0
    tekrar: int = 0
    hata: str | None = None
    hata_tipi: str | None = None


def setup_logging() -> None:
    """Logger'i hem dosyaya (logs/collect.log) hem konsola yazacak sekilde kurar."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def load_sources(path: Path = SOURCES_PATH) -> list[dict[str, Any]]:
    """sources.yaml dosyasini okur ve kaynak listesini dondurur.

    Args:
        path: sources.yaml dosyasinin yolu.

    Returns:
        Her biri 'name', 'url', 'category' anahtarlarini iceren sozluklerin listesi.
    """
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    sources = config.get("sources") or []
    return sources


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """SQLite veritabanini acar, tablolari (yoksa) olusturur ve baglantiyi dondurur.

    Args:
        db_path: SQLite veritabani dosyasinin yolu.

    Returns:
        Aciklanan sqlite3 baglantisi (text_factory=str ile Turkce karakter guvenli).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.text_factory = str

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS haberler (
            hash TEXT PRIMARY KEY,
            kaynak TEXT NOT NULL,
            baslik TEXT NOT NULL,
            ozet TEXT,
            url TEXT NOT NULL,
            yayin_tarihi_utc TEXT,
            yayin_tarihi_tr TEXT,
            toplama_zamani_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kaynak_tarih "
        "ON haberler(kaynak, yayin_tarihi_tr)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_toplama ON haberler(toplama_zamani_utc)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calisma_loglari (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baslangic_utc TEXT NOT NULL,
            bitis_utc TEXT NOT NULL,
            kaynak_sayisi INTEGER,
            yeni_haber INTEGER,
            tekrar_haber INTEGER,
            hatali_kaynak INTEGER,
            ozet TEXT
        )
        """
    )
    conn.commit()
    return conn


def normalize_url(url: str) -> str:
    """URL'den izleme parametrelerini (utm_*, fbclid, gclid) temizler.

    Ayni haber farkli UTM parametreleriyle geldiginde tekillestirmenin
    bozulmamasi icin kullanilir.

    Args:
        url: Ham URL.

    Returns:
        Izleme parametreleri temizlenmis URL.
    """
    parsed = urlparse(url)
    kept_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(UTM_PARAM_PREFIXES)
        and key.lower() not in TRACKING_PARAMS
    ]
    new_query = urlencode(kept_params)
    normalized = parsed._replace(query=new_query)
    return urlunparse(normalized)


def compute_hash(url: str) -> str:
    """Normallestirilmis URL'in SHA-256 hash'ini hesaplar (birincil anahtar).

    Args:
        url: Normallestirilmis URL.

    Returns:
        Hex formatinda SHA-256 hash string'i.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def clean_title(raw_title: str) -> str:
    """Basliktaki HTML entity'lerini ve fazla bosluklari temizler.

    Args:
        raw_title: feedparser'dan gelen ham baslik.

    Returns:
        Temizlenmis baslik metni.
    """
    unescaped = html.unescape(raw_title or "")
    return re.sub(r"\s+", " ", unescaped).strip()


def clean_summary(raw_summary: str | None) -> str | None:
    """Ozet metnini temizler; bos ise None dondurur.

    Args:
        raw_summary: feedparser'dan gelen ham ozet (HTML icerebilir).

    Returns:
        Temizlenmis ozet ya da None.
    """
    if not raw_summary:
        return None
    without_tags = re.sub(r"<[^>]+>", " ", raw_summary)
    cleaned = clean_title(without_tags)
    return cleaned or None


def parse_dates(entry: Any) -> tuple[str | None, str | None]:
    """Bir feed entry'sinden UTC ve Europe/Istanbul yayin tarihlerini cikarir.

    Once entry.published_parsed kullanilir; o yoksa entry.published string'i
    dateutil ile parse edilmeye calisilir. Ikisi de basarisiz olursa
    (None, None) dondurulur.

    Args:
        entry: feedparser entry nesnesi.

    Returns:
        (yayin_tarihi_utc_iso, yayin_tarihi_tr_iso) ikilisi; parse edilemezse
        ilgili degerler None olur.
    """
    dt_utc: datetime | None = None

    struct_time = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if struct_time is not None:
        try:
            dt_utc = datetime.fromtimestamp(
                time.mktime(struct_time), tz=timezone.utc
            )
        except (OverflowError, ValueError):
            dt_utc = None

    if dt_utc is None:
        raw_date = getattr(entry, "published", None) or getattr(
            entry, "updated", None
        )
        if raw_date:
            try:
                parsed = dateutil_parser.parse(raw_date)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                dt_utc = parsed.astimezone(timezone.utc)
            except (ValueError, OverflowError):
                logger.warning(
                    "PARSE: tarih ayristirilamadi: %r", raw_date
                )
                dt_utc = None

    if dt_utc is None:
        return None, None

    dt_tr = dt_utc.astimezone(TR_TZ)
    return dt_utc.isoformat(), dt_tr.isoformat()


def fetch_feed(url: str, timeout: int = FETCH_TIMEOUT_SECONDS) -> feedparser.FeedParserDict:
    """Verilen RSS URL'ini indirir ve feedparser ile ayristirir.

    Indirme icin requests kullanilir (feedparser'in kendi indirme yolunun
    zaman asimi destegi olmadigi icin); boylece NETWORK hatalari (timeout,
    DNS, 403 vb.) PARSE hatalarindan ayri yakalanabilir.

    Args:
        url: RSS akis adresi.
        timeout: Saniye cinsinden istek zaman asimi.

    Returns:
        feedparser tarafindan ayristirilmis feed nesnesi.

    Raises:
        requests.exceptions.RequestException: Aglar/HTTP seviyesinde hata olursa.
    """
    response = requests.get(
        url, timeout=timeout, headers={"User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    return feedparser.parse(response.content)


def process_entries(
    conn: sqlite3.Connection,
    source_name: str,
    entries: list[Any],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Bir kaynaktan gelen entry listesini isleyip veritabanina yazar.

    Args:
        conn: Acik sqlite3 baglantisi.
        source_name: sources.yaml'daki kaynak adi.
        entries: feedparser entry listesi.
        dry_run: True ise veritabanina yazma yapilmaz, sadece sayilir.

    Returns:
        (yeni_haber_sayisi, tekrar_haber_sayisi) ikilisi.
    """
    yeni = 0
    tekrar = 0
    toplama_zamani_utc = datetime.now(timezone.utc).isoformat()

    for entry in entries:
        raw_url = getattr(entry, "link", None)
        raw_title = getattr(entry, "title", None)
        if not raw_url or not raw_title:
            continue

        url = normalize_url(raw_url)
        news_hash = compute_hash(url)
        baslik = clean_title(raw_title)
        ozet = clean_summary(getattr(entry, "summary", None))
        yayin_tarihi_utc, yayin_tarihi_tr = parse_dates(entry)

        if dry_run:
            cur = conn.execute(
                "SELECT 1 FROM haberler WHERE hash = ?", (news_hash,)
            )
            if cur.fetchone():
                tekrar += 1
            else:
                yeni += 1
            continue

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO haberler (
                hash, kaynak, baslik, ozet, url,
                yayin_tarihi_utc, yayin_tarihi_tr, toplama_zamani_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                news_hash,
                source_name,
                baslik,
                ozet,
                url,
                yayin_tarihi_utc,
                yayin_tarihi_tr,
                toplama_zamani_utc,
            ),
        )
        if cur.rowcount == 1:
            yeni += 1
        else:
            tekrar += 1

    if not dry_run:
        conn.commit()

    return yeni, tekrar


def classify_error(exc: Exception) -> str:
    """Bir istisnayi NETWORK / PARSE / DB / UNKNOWN kategorilerinden birine ayirir.

    Args:
        exc: Yakalanan istisna.

    Returns:
        Hata kategorisi string'i.
    """
    if isinstance(exc, requests.exceptions.RequestException):
        return "NETWORK"
    if isinstance(exc, sqlite3.Error):
        return "DB"
    if isinstance(exc, (ValueError, AttributeError, TypeError)):
        return "PARSE"
    return "UNKNOWN"


def collect_source(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    dry_run: bool = False,
) -> SourceResult:
    """Tek bir kaynagi ceker, isler ve sonucunu dondurur. Hata firlatmaz.

    Args:
        conn: Acik sqlite3 baglantisi.
        source: sources.yaml'daki tek bir kaynak sozlugu.
        dry_run: True ise veritabanina yazilmaz.

    Returns:
        Kaynagin bu calismadaki sonucunu tasiyan SourceResult.
    """
    name = source.get("name", "?")
    url = source.get("url", "")
    result = SourceResult(name=name)

    try:
        feed = fetch_feed(url)
    except Exception as exc:  # noqa: BLE001 - kasitli genis yakalama, betik cakilmasin
        error_type = classify_error(exc)
        result.hata = str(exc)
        result.hata_tipi = error_type
        logger.error("%s: kaynak='%s' hata=%s", error_type, name, exc)
        return result

    if getattr(feed, "bozo", False) and not feed.entries:
        error_type = "PARSE"
        result.hata = str(getattr(feed, "bozo_exception", "bilinmeyen XML hatasi"))
        result.hata_tipi = error_type
        logger.error("%s: kaynak='%s' hata=%s", error_type, name, result.hata)
        return result

    if getattr(feed, "bozo", False):
        logger.warning(
            "PARSE: kaynak='%s' feed kismen bozuk ama %d entry islenecek (hata=%s)",
            name,
            len(feed.entries),
            getattr(feed, "bozo_exception", ""),
        )

    try:
        yeni, tekrar = process_entries(conn, name, feed.entries, dry_run=dry_run)
        result.yeni = yeni
        result.tekrar = tekrar
    except Exception as exc:  # noqa: BLE001
        error_type = classify_error(exc)
        result.hata = str(exc)
        result.hata_tipi = error_type
        logger.error("%s: kaynak='%s' hata=%s", error_type, name, exc)

    return result


def write_run_log(
    conn: sqlite3.Connection,
    baslangic_utc: str,
    bitis_utc: str,
    results: list[SourceResult],
) -> None:
    """Calisma ozetini calisma_loglari tablosuna yazar.

    Args:
        conn: Acik sqlite3 baglantisi.
        baslangic_utc: Calismanin baslangic zamani (ISO 8601, UTC).
        bitis_utc: Calismanin bitis zamani (ISO 8601, UTC).
        results: Her kaynak icin SourceResult listesi.
    """
    import json

    kaynak_sayisi = len(results)
    yeni_haber = sum(r.yeni for r in results)
    tekrar_haber = sum(r.tekrar for r in results)
    hatali_kaynak = sum(1 for r in results if r.hata is not None)

    ozet_json = json.dumps(
        [
            {
                "kaynak": r.name,
                "yeni": r.yeni,
                "tekrar": r.tekrar,
                "hata": r.hata,
                "hata_tipi": r.hata_tipi,
            }
            for r in results
        ],
        ensure_ascii=False,
    )

    conn.execute(
        """
        INSERT INTO calisma_loglari (
            baslangic_utc, bitis_utc, kaynak_sayisi,
            yeni_haber, tekrar_haber, hatali_kaynak, ozet
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            baslangic_utc,
            bitis_utc,
            kaynak_sayisi,
            yeni_haber,
            tekrar_haber,
            hatali_kaynak,
            ozet_json,
        ),
    )
    conn.commit()


def print_summary(results: list[SourceResult], dry_run: bool) -> None:
    """Calisma sonucunun insan-okur ozetini stdout'a basar.

    Args:
        results: Her kaynak icin SourceResult listesi.
        dry_run: True ise ozet basliginda dry-run belirtilir.
    """
    toplam_yeni = sum(r.yeni for r in results)
    toplam_tekrar = sum(r.tekrar for r in results)
    hatalilar = [r for r in results if r.hata is not None]

    mode = " (DRY RUN)" if dry_run else ""
    print(f"\n=== Toplama Ozeti{mode} ===")
    print(f"Kaynak sayisi : {len(results)}")
    print(f"Yeni haber    : {toplam_yeni}")
    print(f"Tekrar haber  : {toplam_tekrar}")
    print(f"Hatali kaynak : {len(hatalilar)}")
    if hatalilar:
        print("\nHatalar:")
        for r in hatalilar:
            print(f"  - [{r.hata_tipi}] {r.name}: {r.hata}")
    print()


def show_stats(conn: sqlite3.Connection) -> None:
    """Veritabaninin genel durumunu (toplam, kaynak bazinda, tarih araligi) yazdirir.

    Args:
        conn: Acik sqlite3 baglantisi.
    """
    total = conn.execute("SELECT COUNT(*) FROM haberler").fetchone()[0]
    print(f"\n=== Veritabani Istatistikleri ===")
    print(f"Toplam haber: {total}")

    print("\nKaynak bazinda:")
    for kaynak, sayi in conn.execute(
        "SELECT kaynak, COUNT(*) AS sayi FROM haberler "
        "GROUP BY kaynak ORDER BY sayi DESC"
    ):
        print(f"  {kaynak}: {sayi}")

    date_range = conn.execute(
        "SELECT MIN(yayin_tarihi_tr), MAX(yayin_tarihi_tr) FROM haberler "
        "WHERE yayin_tarihi_tr IS NOT NULL"
    ).fetchone()
    if date_range and date_range[0]:
        print(f"\nTarih araligi (TR): {date_range[0]} -> {date_range[1]}")

    now_utc = datetime.now(timezone.utc)
    cutoff = (now_utc.timestamp() - 24 * 3600)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    son_24_saat = conn.execute(
        "SELECT COUNT(*) FROM haberler WHERE toplama_zamani_utc >= ?",
        (cutoff_iso,),
    ).fetchone()[0]
    print(f"Son 24 saatte toplanan: {son_24_saat}")
    print()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Komut satiri argumanlarini ayristirir.

    Args:
        argv: Test icin ozel argv listesi; None ise sys.argv kullanilir.

    Returns:
        Ayristirilmis Namespace.
    """
    parser = argparse.ArgumentParser(
        description="Turkce ekonomi haberi RSS toplayicisi."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Veritabanina hicbir sey yazmadan kac haber cekilebildigini raporla.",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Sadece belirtilen isimdeki tek kaynagi calistir (test icin).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Veritabaninin mevcut durumunu ozetle ve cik.",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Ana akis: kaynaklari sirayla cekip veritabanina yazar ve ozet raporlar."""
    setup_logging()
    args = parse_args()

    conn = init_db()

    if args.stats:
        show_stats(conn)
        conn.close()
        return

    sources = load_sources()
    if args.source:
        sources = [s for s in sources if s.get("name") == args.source]
        if not sources:
            logger.error("Kaynak bulunamadi: %r", args.source)
            conn.close()
            sys.exit(1)

    baslangic_utc = datetime.now(timezone.utc).isoformat()
    logger.info(
        "Toplama basladi: %d kaynak%s",
        len(sources),
        " (dry-run)" if args.dry_run else "",
    )

    results: list[SourceResult] = []
    for source in sources:
        result = collect_source(conn, source, dry_run=args.dry_run)
        results.append(result)
        if result.hata:
            logger.info(
                "Kaynak tamamlandi (hatali): %s", result.name
            )
        else:
            logger.info(
                "Kaynak tamamlandi: %s (yeni=%d, tekrar=%d)",
                result.name,
                result.yeni,
                result.tekrar,
            )

    bitis_utc = datetime.now(timezone.utc).isoformat()

    if not args.dry_run:
        write_run_log(conn, baslangic_utc, bitis_utc, results)

    print_summary(results, dry_run=args.dry_run)
    conn.close()


if __name__ == "__main__":
    main()
