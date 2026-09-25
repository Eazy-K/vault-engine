# AGENTS.md

Model bağımsız ikinci beyin. Bilgi ağırlıklı bir graf üzerinden yüklenir, dosyaları tek tek okuma. Komutlar vault kökünden çalışır. Motor aracı `$VAULT_ENGINE` ortam değişkeniyle çağrılır (Windows cmd'de `%VAULT_ENGINE%`). Veri klasörünün yolu `$VAULT_DATA` değişkenindedir (Windows cmd'de `%VAULT_DATA%`).

1. **Göreve başlarken:** Vault'ta `git pull --rebase` çalıştır, sonra `python "$VAULT_ENGINE/tools/graph.py" context "<görev özeti; TR + EN anahtar kelimeler>"`. Çıkan notlara uy.
   Python yoksa `profile/working-style.md`, `profile/language.md`, `standards/vault-notes.md` ve `standards/data-policy.md` dosyalarını oku.
2. **Görev `inbox/` klasöründen geldiyse:** `inbox/inbox.md` akışını izle (`python "$VAULT_ENGINE/tools/graph.py" tasks --status open`).
3. **Görev bitince:** `context` çıktısının son satırındaki komutu çalıştır: `reinforce --task <id>` ve ardından gerçekten işe yarayan notlar. Hiçbir not işe yaramadıysa not vermeden çalıştır.
4. **Kalıcı bir bilgi öğrenirsen:** `standards/vault-notes.md` kurallarına göre ilgili nota yaz. Sonra `python "$VAULT_ENGINE/tools/graph.py" lint` çalıştır, İngilizce commit at ve `git pull --rebase && git push` ile gönder. Guard hook, kişisel veri veya secret içeren commit'leri durdurur.
