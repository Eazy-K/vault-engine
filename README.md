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

## Kurulum (şimdilik elle)
1. Motoru klonla. Python 3.10+ yeterli. İsteğe bağlı: Ollama + `ollama pull bge-m3` (yoksa sadece kelime eşleşmesi).
2. Veri reposu oluştur: `templates/` içeriğini boş bir private repoya kopyala, `profile/` notlarını doldur.
3. Ortam değişkenleri: `VAULT_ENGINE=<motor yolu>`, `VAULT_DATA=<veri reposu yolu>`. İsteğe bağlı `VAULT_MACHINE=<makine adı>`.
4. Veri reposunda guard'ı aç: `git config core.hooksPath "$VAULT_ENGINE/tools/hooks"`
5. Projelerin bulunduğu kök klasöre `CLAUDE.md` koy: `@<veri reposu yolu>/AGENTS.md`
6. Kontrol: `python "$VAULT_ENGINE/tools/graph.py" lint`

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

## Motora katkı
- Bu repoda `git config core.hooksPath .githooks` çalıştır. `leakcheck`; kullanıcı içeriğini (`profile/`, `projects/`, ...), ev dizini yolunu, git kimliğini ve `.git/info/vault-denylist` listesindeki terimleri commit'te durdurur.
- Yeni kod testli yazılır.
