---
keywords: [inbox, görev, görevler, task, tasks, kuyruk, queue, iş ver, delegate, iş ajanı]
links:
  - "[[data-policy]]"
weights:
  data-policy: 0.6
---

# Görev Kuyruğu

Bir oturumda ya da cihazda (ör. telefon) yazılan görev, başka bir oturumdaki ajan tarafından yapılır.

## Görev yazma
- Dosya: `inbox/<proje>/NNNN-<kisa-ad>.md`. Numara proje içinde artar.
- Şablon: `inbox/_template.md`. Frontmatter'daki `type: task`, `project` ve `status` alanları zorunlu.
- Görevde kişisel veri olmaz (bkz. [[data-policy]]).

## Görevi yapan ajan
1. `git pull --rebase`, ardından `python tools/graph.py tasks --status open`.
2. Görevin durumunu `in-progress` yap, commit edip push et. Böylece diğer makine görevin alındığını görür.
3. İşi yap. Proje reposunda kendi kurallarıyla: branch, PR, test.
4. Görevin `## Log` bölümünü doldur (tarih, yapılanlar, PR linki, test sonucu, sorunlar). Durumu `done` ya da `blocked` yap.
5. Projenin status notunu güncelle, sonra commit edip push et.

## Durumlar
`open` → `in-progress` → `done` / `blocked`. `done` durumundaki görevler geçmiş olarak kalır, ama `context` çıktısına artık yüklenmez.
