# vault-engine

Model bağımsız "ikinci beyin" motoru. Notlar ağırlıklı bir graf olarak yüklenir; ajan her görevde sadece ilgili bağlamı alır. Motor kullanıcı bağımsızdır: senin notların ayrı bir **veri reposunda** durur, bu repoya hiçbir kullanıcı bilgisi girmez.

## Yapı
| Yol | İçerik |
|---|---|
| `tools/graph.py` | Arama, yayılım, öğrenme, guard, leakcheck. Sadece Python standart kütüphanesi. |
| `tools/feedback.py` | İsteğe bağlı, sızıntısız geri bildirim (bkz. aşağıda). |
| `tools/hooks/` | Veri repoları için guard hook'ları (`core.hooksPath` buraya işaret eder) |
| `tools/claude-agents/` | Claude Code alt ajan tanımları (`~/.claude/agents/` altına kopyalanır) |
| `defaults/` | Genel notlar. Veri reposunda aynı yoldaki not bunları ezer. |
| `templates/` | Yeni bir veri reposunun iskeleti |
| `.githooks/` | Bu reponun kendi hook'ları: `leakcheck` |
| `tests/` | `python -m unittest discover -s tests` |

## Kurulum
1. Motoru klonla. Python 3.10+ yeterli. İsteğe bağlı: Ollama + `ollama pull bge-m3` (yoksa sadece kelime eşleşmesi).
2. Veri reposu oluştur: `python tools/graph.py init <veri reposu yolu>`. `templates/` içeriğini kopyalar (var olan dosyaların üzerine yazmaz), git reposu başlatır, `core.hooksPath`'i bu motorun `tools/hooks` klasörüne ayarlar ve `vault.config.json` içine proje kökleri ile feedback tercihini yazar (feedback varsayılan olarak `off`'tur — opt-in). TTY'de ve `--yes` verilmediyse eksik değerler için sorar; `--feedback`, `--feedback-mode`, `--project-root` (tekrarlanabilir) ve `--yes` ile elle de verilebilir.
3. Motoru veri reposuna bağla: `python tools/graph.py setup [--data <veri reposu yolu>]`. Ortam değişkenlerini ayarlar (Windows'ta `setx` ile kalıcı; diğer platformlarda `export` satırlarını yazdırır), `tools/claude-agents/*.md` dosyalarını `~/.claude/agents/`'a kopyalar, ve her proje kökü için (git reposu değilse) `CLAUDE.md` içine `@<veri reposu yolu>/AGENTS.md` satırını yazar. İki kez çalıştırmak hiçbir şeyi değiştirmez. `--user-level` ile `~/.claude/CLAUDE.md` ve `~/.codex/AGENTS.md` dosyalarına da tek satırlık yönlendirme eklenir. `--no-env`, `--no-agents`, `--no-routing`, `--yes` adımları atlar/otomatikleştirir.
4. `profile/` altındaki notları doldur.
5. Kontrol: `python tools/graph.py doctor` — her satırda bir kontrol (`OK`/`WARN`/`FAIL`); herhangi bir `FAIL` çıkış kodunu 1 yapar.

## Proje keşfi
`tools/discovery.py`, motorun yanındaki (ya da `vault.config.json`'daki `project_roots` altındaki) git repolarını tarar ve hangi projelerin henüz not almadığını bulur. Sadece hafif metadata okunur (README'nin ilk satırı, dosya uzantısı sayımı, son commit tarihi, remote'un sadece host kısmı); kod hiçbir zaman indekslenmez ve hiçbir şey makineden dışarı çıkmaz. `vault.config.json`'daki `exclude` listesi (isim veya glob) ve bir repodaki `.vaultignore` dosyası taramadan hariç tutar. Sonuç `<veri reposu>/.graph/projects.json` içinde 1 saat önbelleğe alınır (bu dosya commit'lenmez). Kullanım: `python tools/graph.py projects` (tablo), `--json`, `--missing` (sadece notu olmayan projeler, sonunda öneri satırıyla) veya `--refresh` (önbelleği yok say).

## Proje tanıma
`context` çalıştığı klasörden projeyi bulur ve `projects/<ad>/` notlarını başlangıç noktası yapar. Proje kökleri veri reposundaki `vault.config.json` dosyasından okunur (`{"project_roots": ["..."]}`); dosya yoksa motorun üst klasörü kullanılır. `--project <ad>` ya da `--no-project` ile elle belirlenir.

## Geri bildirim
`tools/feedback.py` kurulumundan motorun geliştiricilerine isteğe bağlı geri bildirim gönderir (GitHub issue olarak). Varsayılan kapalıdır; hiçbir şey göndermeden önce açıkça açman gerekir.

Seviyeler (`vault.config.json` içinde `"feedback": {"level": ..., "mode": ..., "repo": "owner/name"}`):
- `off` (varsayılan): hiçbir şey gönderilmez.
- `metrics`: sadece sayılar ve sabit kategoriler gönderilir -- motor sürümü, işletim sistemi, Python sürümü, not sayısı aralığı (`<25`, `25-100`, ...), `context`/`reinforce` kullanım sayaçları, ajan başına dağılım. Not id'si, sorgu metni, dosya yolu veya makine adı **asla** gönderilmez. En sık 7 günde bir gönderilir.
- `reports`: yukarıdakine ek olarak, `feedback add --kind friction|bug|idea --command <komut> --summary "..." [--details "..."]` ile kuyruğa alınan kısa serbest metin notları da gönderilir (özet en fazla 200, detay en fazla 1000 karakter).

`mode`: `auto` (varsayılan; motor arka planda `reinforce` sonrasında günde en fazla bir kez gönderir) veya `ask` (önce önizleme gösterir, ajan kullanıcıya sorar, `feedback send --yes` ile onaylanır).

Hedef repo `feedback.repo` alanından, yoksa motorun `origin` remote'undan çıkarılır; hiçbiri yoksa gönderim devre dışıdır (hata vermez).

Serbest metin (özet/detay/komut) kuyruğa alınırken ve gönderilmeden hemen önce iki kez süzülür: notlardaki aynı guard kalıpları (TCKN, IBAN, kart no, telefon, e-posta, token, parola), ev dizini yolu, git kimliği, `.git/info/vault-denylist` terimleri ve veri reposundaki proje adları taranır. Bir eşleşme bulunursa metin **hiçbir zaman** gönderilmez veya ekrana yazılmaz; sadece eşleşmenin türü (ör. "TCKN") raporlanır ve içerik `.graph/feedback/quarantine/` altına taşınır.

Komutlar:
- `feedback status` -- etkin ayarlar, hedef repo, kuyruk/karantina sayıları, son gönderim zamanı.
- `feedback list` -- tam olarak ne gönderileceğini JSON olarak gösterir (gönderim yapmaz).
- `feedback send [--yes]` -- kuyruktakileri gönderir.
- `feedback set --level ... [--mode ...] [--repo ...] [--machine]` -- ayarları yazar. `--machine` ile hassas bir makinede sadece o makineye özel, gitignore'lı `.graph/machine.json` dosyasına yazılır (veri reposundaki ortak ayarı ezer).

Kapatmak için: `feedback set --level off` (veya belirli bir makinede `--machine` ile).

## CI (GitHub Actions)
- Motor reposu: her push ve PR'da testler (Ubuntu: Python 3.10 ve 3.13, Windows: 3.13) ve `leakcheck` (dosyalar + commit mesajları).
- Veri reposu: `init` ile gelen `.github/workflows/vault.yml`, motoru private bir composite action olarak kullanır (`uses: <sahip>/vault-engine@main`): guard, commit mesajları ve not lint'i. Böylece buluttan ya da telefondan atılan commit'ler de denetlenir.
- Motor private ise bir kez: motor reposunda Settings > Actions > General > Access → "Accessible from repositories owned by the user". Komutla: `gh api -X PUT repos/<sahip>/vault-engine/actions/permissions/access -f access_level=user`

## Motora katkı
- Bu repoda `git config core.hooksPath .githooks` çalıştır. `leakcheck`; kullanıcı içeriğini (`profile/`, `projects/`, ...), ev dizini yolunu, git kimliğini ve `.git/info/vault-denylist` listesindeki terimleri commit'te durdurur.
- Yeni kod testli yazılır.
