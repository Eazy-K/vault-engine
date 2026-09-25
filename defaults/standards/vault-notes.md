---
keywords: [vault, not, yazma, güncelleme, frontmatter, lint, hafıza, memory, note, update, kural]
links:
  - "[[data-policy]]"
weights:
  data-policy: 0.4
---

# Vault Not Yazma Kuralları

## Ne yazılır
- Kalıcı ve önemli bilgiler yazılır: tercihler, kararlar, proje bilgisi, öğrenilenler.
- Sadece o konuşmayı ilgilendiren geçici bilgiler yazılmaz.
- Önce mevcut notu güncellemeyi dene, kopya not oluşturma. Yanlış çıkan bilgiyi sil veya düzelt.

## Nasıl yazılır
- Bir not tek bir konuyu anlatır. Karışık konulu notlar aramada kötü eşleşir.
- Maddeler ve tablo satırları kendi başına anlaşılır olmalı, çünkü aramada her biri ayrı bir parça olarak değerlendirilir.
- Placeholder veya örnek satır yazma, bunlar yanlış eşleşmeye yol açar.
- Tarihler mutlak yazılır (YYYY-MM-DD).
- Dosya adları İngilizce ve kebab-case, içerik Türkçe olur.

## Frontmatter
```
---
keywords: [türkçe, english, eşanlamlılar, kısaltmalar]
links:
  - "[[ilgili-not]]"
weights:
  ilgili-not: 0.8
---
```
- `keywords` alanı kelime eşleşmesinde kullanılır. Kısaltmalar (db, api) ve İngilizce karşılıklar buraya eklenir.
- Ağırlık rehberi: 0.8–1.0 güçlü ilişki, 0.5–0.7 ilgili, 0.3 zayıf ilişki. Ağırlık yazılmazsa 0.7 kabul edilir.
- `core: true` sadece her görevde geçerli zorunlu kurallar için kullanılır. Core notlar her çağrıda yüklendiği için **15 satırı geçmemeli**.

## Klasörler
- `profile/`: kullanıcı ve tercihleri
- `projects/<proje>/`: proje bilgisi
- `standards/`: konvansiyonlar
- `notes/`: kişisel notlar
- `decisions/`: vault kararları (ADR)
- `inbox/`: makineler arası görev kuyruğu (bkz. [[inbox]])

## Araç
`python tools/graph.py <komut>`
- `context`, `query`: bağlam yükleme ve listeleme (`--seed`, `--threshold`, `--budget`, `--no-semantic`, `--no-log`)
- `show`: bir notun kenarlarını gösterir
- `reinforce --task <id>`, `decay`: öğrenme
- `stats`: `context` çağrılarının ne kadarının `reinforce` ile kapatıldığını ve hangi notların getirilip hiç kullanılmadığını gösterir
- `lint`: tutarlılık kontrolü
- `index`: embedding'leri önceden hesaplar
- `tasks`: inbox görevlerini listeler (`--status`, `--project`)
- `guard`: kişisel veri ve secret taraması (hook'lar otomatik çalıştırır)
