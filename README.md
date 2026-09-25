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

## Kurulum (şimdilik elle)
1. Motoru klonla. Python 3.10+ yeterli. İsteğe bağlı: Ollama + `ollama pull bge-m3` (yoksa sadece kelime eşleşmesi).
2. Veri reposu oluştur: `templates/` içeriğini boş bir private repoya kopyala, `profile/` notlarını doldur.
3. Ortam değişkenleri: `VAULT_ENGINE=<motor yolu>`, `VAULT_DATA=<veri reposu yolu>`. İsteğe bağlı `VAULT_MACHINE=<makine adı>`.
4. Veri reposunda guard'ı aç: `git config core.hooksPath "$VAULT_ENGINE/tools/hooks"`
5. Projelerin bulunduğu kök klasöre `CLAUDE.md` koy: `@<veri reposu yolu>/AGENTS.md`
6. Kontrol: `python "$VAULT_ENGINE/tools/graph.py" lint`

## Proje tanıma
`context` çalıştığı klasörden projeyi bulur ve `projects/<ad>/` notlarını başlangıç noktası yapar. Proje kökleri veri reposundaki `vault.config.json` dosyasından okunur (`{"project_roots": ["..."]}`); dosya yoksa motorun üst klasörü kullanılır. `--project <ad>` ya da `--no-project` ile elle belirlenir.

## Motora katkı
- Bu repoda `git config core.hooksPath .githooks` çalıştır. `leakcheck`; kullanıcı içeriğini (`profile/`, `projects/`, ...), ev dizini yolunu, git kimliğini ve `.git/info/vault-denylist` listesindeki terimleri commit'te durdurur.
- Yeni kod testli yazılır.
