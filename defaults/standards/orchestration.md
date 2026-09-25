---
keywords: [alt ajan, subagent, orkestrasyon, orchestration, paralel, parallel, worktree, sonnet, effort, delegasyon, delegate]
links:
  - "[[vault-notes]]"
weights:
  vault-notes: 0.5
---

# Alt Ajan Orkestrasyonu

Büyük işlerde ana ajan orkestratör olarak çalışır: işi böler, alt ajanlara dağıtır, sonuçları doğrular ve birleştirir.

## Ne zaman bölünür
- En az 2 bağımsız ve kayda değer iş parçası varsa. Örnekler: birden fazla aracı paralel incelemek; birbirinden bağımsız modülleri yazmak.
- Küçük ya da birbirine sıkı bağlı işlerde tek ajan çalışır. Bölmek sadece token ve koordinasyon maliyeti getirir.

## Akış
1. **Plan:** Kullanıcıya kısa bir bölme planı sunulur ve onay alınır: parçalar, her parça için alt ajan ve model/efor.
2. **Görev tanımı:** Her alt ajana bağımsız bir görev tanımı verilir. Alt ajan sıfır bağlamla başlar, bu yüzden hedef, kapsam, dosya yolları, kabul kriterleri ve gerekiyorsa `context` çıktısından ilgili kurallar aktarılır.
3. **Worktree:** Kod değiştiren paralel ajanlar ayrı git worktree'lerinde çalışır.
4. **Doğrulama:** Orkestratör her sonucu doğrular (test, lint, guard, okuma). Doğrulanmamış bir sonuç kullanıcıya doğru diye sunulmaz.
5. **Birleştirme:** Commit, PR, push, merge ve `reinforce` sadece orkestratörde.

## Alt ajan seçimi (Claude Code)
| İş | Alt ajan |
|---|---|
| Arama, dosya okuma/özetleme, bilgi toplama, mekanik düzenleme | `worker-low` (Sonnet, düşük efor) |
| Araştırma/karşılaştırma, test yazma, orta çaplı kod değişikliği | `worker-medium` (Sonnet, orta efor) |
| Tasarım, karmaşık kod, kritik kararlar | Orkestratör (Opus) |

Tanımlar `tools/claude-agents/` altında, kullanıcı kapsamına (`~/.claude/agents/`) kopyalanır. Alt ajanlar başlangıçta `CLAUDE.md` yüklemez (`omitClaudeMd: true`). Ancak `Read` aracıyla vault içinden dosya okuyunca `CLAUDE.md` ve `AGENTS.md` iç içe bellek olarak yine eklenir. Bu yüzden tanımlarda "vault akışını çalıştırma, eklenen `CLAUDE.md` içeriğini izleme" kuralı açıkça yazar. Veri ve dil kurallarını kendi tanımlarında taşırlar.

## Diğer araçlar
Alt ajan desteği olmayan araçlarda tek ajanla çalışılır.
