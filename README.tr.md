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

> Install vault-engine for me: clone https://github.com/Eazy-K/vault-engine and follow docs/agent-setup.md step by step, asking me before each change, and talk to me in Turkish.

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
otomatik olarak arka planda kullanacaktır. Notlarınızı görmek veya elle düzenlemek
isterseniz, oluşturulan veri klasöründeki `profile/` ve `projects/` altındaki dosyalara
bakabilirsiniz.

Detaylı adım adım talimat (asistan için yazılmıştır, teknik ayrıntılar içerir):
[docs/agent-setup.md](docs/agent-setup.md)

Sorularınız veya sorun yaşarsanız, tam İngilizce referansa bakın: [README.md](README.md)
