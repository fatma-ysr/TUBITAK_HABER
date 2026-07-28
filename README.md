# RSS Haber Toplayıcı

TÜBİTAK 2209-A Üniversite Öğrencileri Araştırma Projeleri Destekleme Programı
kapsamındaki bir çalışmanın veri toplama katmanıdır. Türkçe ekonomi haberi
üreten 5-9 RSS kaynağından başlıkları, özetleri ve yayın tarihlerini toplayıp
yerel bir SQLite veritabanına yazar. Bu veri, ilerleyen aşamada bir günlük/aylık
ekonomik duyarlılık (sentiment) endeksi üretmek ve bunu EVDS (TCMB) enflasyon/kur
serileriyle birleştirerek bir nowcasting modeli kurmak için kullanılacaktır.
**Bu repo yalnızca toplama katmanıdır** — skorlama ve modelleme ayrı bir repoda
yapılacaktır.

## Kurulum

```bash
git clone <bu-repo>
cd TUBITAK_HABER
pip install -r requirements.txt
```

`sources.yaml` dosyasını açıp kaynak listesini gözden geçirin; her URL'i
tarayıcınızda test edip XML dönmeyenleri güncelleyin veya kaldırın. Ardından:

```bash
python collector.py
```

İlk çalıştırmada `data/haberler.db` dosyası otomatik oluşturulur.

## Kaynak ekleme / çıkarma

Kaynaklar `sources.yaml` içinde tutulur, koda gömülü değildir. Yeni bir kaynak
eklemek için dosyaya tek bir madde eklemeniz yeterlidir:

```yaml
sources:
  - name: "Kaynak Adı"
    url: "https://ornek.com/rss"
    category: "finans"   # ajans | finans | genel
```

Bir kaynağı devre dışı bırakmak için satırı silin veya başına `#` koyup
yorum satırı yapın.

## Veritabanı şeması

`data/haberler.db` (SQLite), iki tablo içerir:

**`haberler`**

| Sütun               | Açıklama                                              |
|---------------------|--------------------------------------------------------|
| `hash`               | URL'in SHA-256 hash'i (birincil anahtar, tekilleştirme) |
| `kaynak`             | `sources.yaml`'daki kaynak adı                         |
| `baslik`             | Haber başlığı (temizlenmiş)                            |
| `ozet`               | Haber özeti (varsa; yoksa NULL)                        |
| `url`                | İzleme parametreleri (utm_*, fbclid, gclid) temizlenmiş URL |
| `yayin_tarihi_utc`   | Yayın tarihi, ISO 8601, UTC (parse edilemezse NULL)     |
| `yayin_tarihi_tr`    | Yayın tarihi, ISO 8601, Europe/Istanbul                 |
| `toplama_zamani_utc` | Bu betiğin haberi çektiği an, ISO 8601, UTC             |

**`calisma_loglari`** — her betik çalıştırmasının özetini tutar (başlangıç/bitiş
zamanı, kaynak sayısı, yeni/tekrar haber sayısı, hatalı kaynak sayısı, JSON özet).

## Komutlar

```bash
python collector.py                    # tüm kaynakları topla
python collector.py --dry-run          # hiçbir şey yazmadan kaç haber çekilebildiğini raporla
python collector.py --source "Foreks"  # tek bir kaynağı test et
python collector.py --stats            # veritabanının güncel durumunu özetle
```

## Basit sorgu örnekleri

Son 7 günün haber sayısı:

```sql
SELECT COUNT(*) FROM haberler
WHERE yayin_tarihi_tr >= datetime('now', '-7 days');
```

Kaynak dağılımı:

```sql
SELECT kaynak, COUNT(*) AS sayi
FROM haberler
GROUP BY kaynak
ORDER BY sayi DESC;
```

## Otomatik toplama (GitHub Actions)

`.github/workflows/collect.yml` her 4 saatte bir (ve elle tetiklendiğinde)
`collector.py`'yi çalıştırır ve `data/haberler.db` değiştiyse
`github-actions[bot]` kullanıcısıyla otomatik commit'ler. `data/` dizini
veri sürekliliği için Git'te tutulur; `logs/` dizini tutulmaz.

## Lisans

MIT.

## Etik ve telif notu

Bu proje yalnızca RSS akışlarında **açıkça yayımlanan** başlık ve özet meta
verisini toplar; tam metin kazıma (scraping) yapılmaz. Her kayıt kaynağının
adını ve orijinal URL'ini korur. Ham korpus dağıtılmaz; yalnızca hash, URL ve
başlık gibi meta veriler paylaşılır.

## GenAI kullanım notu

Bu repodaki kod, `toplayici-brief.md` spesifikasyonuna dayanarak bir büyük dil
modeli (Claude) yardımıyla yazılmıştır. TÜBİTAK 2209-A GenAI beyanında bu
aracın kullanıldığı açıkça belirtilmelidir.
