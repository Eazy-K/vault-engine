---
core: true
keywords: [kvkk, kişisel veri, personal data, pii, gizlilik, privacy, maskeleme, masking, secret, parola, token, guard]
links:
  - "[[vault-notes]]"
weights:
  vault-notes: 0.5
---

# Veri Kuralı

- Yasak: KVKK kişisel verisi (ad-soyad + kimlik/iletişim bilgisi, TCKN, telefon, e-posta, adres, IBAN/kart, sağlık, müşteri kaydı).
- Yasak: secret (parola, token, anahtar, bağlantı cümlesindeki kimlik bilgisi).
- Log, örnek veri, hata çıktısı yazmadan önce maskele: `***`, `ornek@example.com`, sahte ad.
- Guard hook commit'i durdurursa veriyi maskele; yanlış alarmsa satıra `guard:ignore` ekle.
- Vault dosyaları ilgisiz repolara (ekip, şirket, açık kaynak) commit edilmez; gerekirse `.git/info/exclude` kullanılır.
