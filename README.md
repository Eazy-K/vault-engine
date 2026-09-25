# vault-engine

Model bağımsız "ikinci beyin" motoru. Notlar ağırlıklı bir graf olarak yüklenir; ajan her görevde sadece ilgili bağlamı alır. Motor kullanıcı bağımsızdır: senin notların ayrı bir **veri reposunda** durur, bu repoya hiçbir kullanıcı bilgisi girmez.

## Yapı
| Yol | İçerik |
|---|---|
| `tools/graph.py` | Arama, yayılım, öğrenme, guard, leakcheck. Sadece Python standart kütüphanesi. |
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

## Proje tanıma
`context` çalıştığı klasörden projeyi bulur ve `projects/<ad>/` notlarını başlangıç noktası yapar. Proje kökleri veri reposundaki `vault.config.json` dosyasından okunur (`{"project_roots": ["..."]}`); dosya yoksa motorun üst klasörü kullanılır. `--project <ad>` ya da `--no-project` ile elle belirlenir.

## Motora katkı
- Bu repoda `git config core.hooksPath .githooks` çalıştır. `leakcheck`; kullanıcı içeriğini (`profile/`, `projects/`, ...), ev dizini yolunu, git kimliğini ve `.git/info/vault-denylist` listesindeki terimleri commit'te durdurur.
- Yeni kod testli yazılır.
