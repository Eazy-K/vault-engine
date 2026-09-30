# vault-engine (Türkçe hızlı başlangıç)

Bu, kısa bir Türkçe özettir. Tam referans (İngilizce) için: [README.md](README.md)

## Ne işe yarar?

vault-engine, AI kodlama asistanınız (Claude Code veya Codex) için kalıcı bir "ikinci
beyin" sağlar. Notlarınız kendi bilgisayarınızda, ayrı ve özel bir klasörde tutulur;
asistan her görevde sadece o göreve uygun notları otomatik olarak yükler. Kodun
bulunduğu bu depo (vault-engine) herkese açıktır ve içinde hiçbir kişisel bilgi
bulunmaz — sizin notlarınız tamamen ayrı bir yerde saklanır.

## Hızlı kurulum (teknik bilgi gerekmez)

Bilgisayarınızda kurulu ve giriş yapılmış bir AI kodlama asistanı gerekir — [Claude
Code](https://claude.com/claude-code) ya da [Codex](https://openai.com/codex). Sonra
asistana şunu yapıştırın (İngilizce bırakabilirsiniz, asistan sizinle Türkçe konuşmaya
devam edecektir):

> Install vault-engine for me: read docs/agent-setup.md in https://github.com/Eazy-K/vault-engine and follow it step by step, asking me before each change, and talk to me in Turkish.

Asistan şunları yapacak:
- Bilgisayarınızda neyin kurulu olduğunu kontrol eder, eksik bir şeyi kurmadan önce
  sizden onay ister.
- İki klasör oluşturur: motorun kendisi ve sizin kişisel notlarınız.
- İsterseniz notlarınızın GitHub üzerinde **özel (private)** bir yedeğini oluşturur —
  istemezseniz notlar sadece bilgisayarınızda kalır, bu da tamamen normaldir.
- Nasıl çalışmayı sevdiğinizi öğrenmek için birkaç kısa soru sorar (hangi dilde
  konuşacağı, ne kadar ayrıntılı yanıt istediğiniz, hangi durumlarda önce sizden onay
  alması gerektiği gibi).

Her adımdan önce ne yapacağını tek cümleyle açıklar ve onayınızı bekler; hiçbir şeyi
sormadan kurmaz veya değiştirmez.

Kurulumdan sonra normal şekilde asistanınızla çalışmaya devam edersiniz — notlarınızı
otomatik olarak arka planda kullanacaktır. Asistan öğrendiklerini zamanla not olarak
yazar (ör. bir proje için `projects/<proje>/` klasörü gerektiğinde oluşur). Tercihlerinizi
elle düzenlemek isterseniz veri klasöründeki `profile/` altındaki dosyalara bakabilirsiniz.

Claude Code delegasyon hook'u (`delegation-warn`), asistanın bir kullanıcı mesajında ana konuşmada kaç araç çağrısı yaptığını `.graph/usage.log` dosyasına yazar; `python tools/graph.py stats` bunu "orchestrator prompts" satırında özetler (ölçülen mesaj sayısı, eşiği aşanların yüzdesi, medyan çağrı sayısı). Bir oturumun son mesajı ölçülmez.

Kullanıcı düzeyi ayarlar (`user-config`): `defaults/config/` şablonları (veri klasöründe `config/` altında aynı yolda bir dosya varsa onun yerini alır) `$CODEX_HOME` (varsayılan `~/.codex`) ve `$CLAUDE_CONFIG_DIR` (varsayılan `~/.claude`) içine, kullanıcının kendi eklediklerini silmeden birleştirilir: Codex `developer_instructions` ve `rules/default.rules` içinde `# vault-engine:begin` / `# vault-engine:end` işaretli blok, Claude `settings.json` içinde derin birleştirme. Yer tutucular: `{VAULT_DATA}`, `{VAULT_ENGINE}`, `{WORKSPACE}`. `python tools/graph.py user-config` farkı bildirir, `--install` uygular (her dosyayı ilk değişiklikten önce `<ad>.bak-<TarihSaat>` olarak yedekler; ikinci çalıştırma hiçbir şey değiştirmez). Başka bilgisayarda: `git pull`, `doctor`, `user-config --install`.

Codex kurulumu için `python tools/graph.py codex-hooks --install` komutu bağlam ve
delegasyon hook'larını ve `agent-guard` (`PreToolUse`, `Agent|spawn_agent`; yalnızca `worker-*` profillerine ve kendi model/eforlarına izin verir) hook'unu ekler, eksik worker dosyalarını kurar. `doctor` ve `context`,
`~/.codex` varsa hook ve worker dosyalarının eksik veya eski olup olmadığını; ayrıca
`config.toml` içindeki model ve eforun açıkça ayarlanıp ayarlanmadığını denetler. Tercih
edilen model kullanıcıya özel olduğundan motor onun değişip değişmediğini belirlemez.
Dürüst not: Codex CLI 0.159.2 üzerindeki deney, PreToolUse deny kararının uygulandığını göstermedi; koruma, CLI bunu desteklediğinde çalışsın diye kurulur. Hook dosyasını değiştirmeden önce yedekler; kişiselleştirilmiş worker dosyalarını korur.

`python tools/graph.py stats --tokens`, model bazında çağrı, token ve tahmini maliyet paylarını gösterir. Codex için `stats --tokens --session <id|current>` ana oturum ile alt ajan kullanımını ayırır ve her alt ajan için ayrı satır verir. JSON çıktısı da çağrı, token ve maliyet paylarını ve ana oturum/alt ajan dökümünü içerir. Codex JSON'undaki `cost_pct`, fiyatı bilinen modellerin maliyet ara toplamındaki model payıdır. `cost_basis` değeri `partial_api_list_estimate` ve `cost_estimate_status` değeri `partial` olduğunda bu ara toplam fiyatsız modellerin kullanım maliyetini içermez. Tahmini USD, standart OpenAI API liste fiyatlarına göre hesaplanır; Codex veya ChatGPT abonelik ücreti değildir. Model ya da ajan ilişkilendirmesi belirsizse rapor bunu kısmi/bilinmiyor olarak işaretler ve ilgili alanlara tahmin yerine `null` yazar.

Öğrenilen bağlantılar kendiliğinden zayıflar (decay): `reinforce`, bu bilgisayardaki son
zayıflatmadan bu yana 7 günden fazla geçtiğini görürse öğrenilen bağlantıları önce %5
zayıflatır ve tek satır `auto-decay:` yazar; değişiklik aynı öğrenilen-bağlantılar
commit'ine girer. Zaman damgası veri klasöründeki, commit'lenmeyen `.graph/last-decay`
dosyasındadır; elle çalıştırılan `decay` de onu günceller. Güncellemeden sonraki ilk
`reinforce` yalnızca damgayı kaydeder, hemen zayıflatma yapmaz.

Detaylı adım adım talimat (asistan için yazılmıştır, teknik ayrıntılar içerir):
[docs/agent-setup.md](docs/agent-setup.md)

## Güncelleme

0.3.0 ve sonraki sürümlerde, yeni bir sürüm çıktığında asistanınız size haber verecek.
Güncellemek için asistanınıza sadece "vault-engine'i güncelle" demeniz yeterli — ne
değiştiğini size gösterecek ve geçmeden önce onayınızı isteyecek. Siz sormadan
kendiliğinden güncelleme yapmaz.

### 0.1.0 veya 0.2.0 kullanıyorsanız

Bu iki sürüm yeni sürümü haber vermez ve `update` komutunu bilmez (`invalid choice:
'update'` hatası verir). İlk güncelleme bir kereliğine elle yapılır, sonrasında `update`
çalışır. Asistanınıza şunu yazmanız yeterli:

> Update vault-engine: my version has no `update` command, so follow "Updating from 0.1.0 or 0.2.0" in docs/agent-setup.md on GitHub (https://github.com/Eazy-K/vault-engine), asking me before each change, and talk to me in Turkish.

Asistanın izleyeceği adımlar kısaca: motor klasöründe yerel değişiklik olmadığını
kontrol etmek, `git fetch --tags origin` ile sürümleri indirmek, en yeni etiketin
CHANGELOG'daki "Upgrade notes" bölümünü size özetlemek, onayınızla `git checkout <yeni
etiket>`, ardından `migrate --yes`, `setup --yes` ve `doctor` çalıştırmak, `doctor`'ın
uyarılarını düzeltmek ve veri reposunu göndermek.

### Birden fazla bilgisayar

Eğer notlarınızı birden fazla bilgisayardan kullanıyorsanız, hepsini güncellemeyi
unutmayın. 0.3.0 ve sonrası, veri düzeni kendisinden yeni bir vault'a yazmayı reddeder ve
sizden o bilgisayarda da güncellemenizi ister. 0.1.0 ve 0.2.0 bu kontrolü yapmaz; bu
yüzden bütün bilgisayarlar en az 0.3.0'a geçmeden, Upgrade notes'unda veri düzenini
(schema) yükselttiğini söyleyen bir sürüme geçmeyin.

Sorularınız veya sorun yaşarsanız, tam İngilizce referansa bakın: [README.md](README.md)
