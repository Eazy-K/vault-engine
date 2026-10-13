# vault-engine (Türkçe)

AI kodlama asistanları ([Claude Code](https://claude.com/claude-code) ve [Codex](https://openai.com/codex)) için bir "ikinci beyin". Notlarınız ağırlıklı bir graf oluşturur; asistan her görevde yalnızca o göreve uygun notları yükler.

Motor herkese açıktır ve içinde kişisel bilgi yoktur. Notlarınız sizin sahip olduğunuz ayrı bir **veri deposunda** durur.

*Tam referans (İngilizce): [README.md](README.md)*

## Hızlı kurulum

Kurulu ve giriş yapılmış bir AI kodlama asistanı gerekir. Ona şunu yapıştırın (İngilizce kalabilir; asistan sizinle Türkçe konuşmaya devam eder):

> Install vault-engine for me: read docs/agent-setup.md in https://github.com/Eazy-K/vault-engine and follow it step by step, asking me before each change, and talk to me in Turkish.

Asistan bilgisayarınızda neyin kurulu olduğunu kontrol eder; bir şey kurmadan veya hesap açmadan önce sorar. İki klasör oluşturur (motor ve notlarınız), isterseniz notlar için **özel (private)** bir GitHub yedeği kurar ve nasıl çalışmayı sevdiğinizi öğrenmek için birkaç kısa soru sorar: dil, ayrıntı düzeyi, hangi durumlarda önce onay alacağı. Her adımı önce açıklar. Siz yalnızca evet veya hayır dersiniz.

## Günlük kullanım nasıl işler

Normalde hiçbir şey yazmazsınız. Bir göreve başlarken asistan:

1. veri deposundaki son notları çeker,
2. göreve uygun notları yükler (notlar arasındaki bağlantıları izler ve hangilerinin işe yaradığını öğrenir),
3. işi yapar ve öğrendiklerini not eder,
4. işe yarayan notları işaretler; böylece bir sonraki sefer daha üstte çıkar,
5. notları commit'ler ve gönderir.

Kullanılmayan bağlantılar yavaş yavaş zayıflar. Notlarınız her commit öncesi denetlenir; gizli bilgi ve kişisel veri sızmaz.

## Güncelleme ve başka bilgisayar

Yeni sürüm çıkınca asistan size bir kez haber verir. Onaylarsınız; güncellemeyi o çalıştırır (notları taşır, asistan kurulumunu yeniler, sonucu denetler). İkinci bilgisayarda veri deponuzu çekip aynı güncellemeyi çalıştırın:

```
python <engine>/tools/graph.py update --all
```

Codex'te güncellemeden sonra değişen hook'ları `/hooks` ile yeniden onaylayın.

Motoru elle `git pull` ile güncellemeyin. 0.1.0 veya 0.2.0 kullanıyorsanız `update` komutu yoktur; asistana şunu yazın:

> Update vault-engine: my version has no `update` command, so follow "Updating from 0.1.0 or 0.2.0" in docs/agent-setup.md on GitHub (https://github.com/Eazy-K/vault-engine), asking me before each change, and talk to me in Turkish.

## Daha fazlası

- [Tüm komutlar](docs/commands.md) (İngilizce): kurulum, projeler, geri bildirim, CI, sürümler.
- [Asistan kurulum rehberi](docs/agent-setup.md): asistanın izlediği adımlar.
- [Katkı](docs/commands.md#contributing-to-the-engine): sızıntı denetimini açma, test ekleme.
- Lisans: MIT, bkz. LICENSE.
