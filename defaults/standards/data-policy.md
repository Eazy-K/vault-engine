---
core: true
keywords: [kvkk, kişisel veri, personal data, pii, gizlilik, privacy, maskeleme, masking, secret, parola, token, guard]
links:
  - "[[vault-notes]]"
weights:
  vault-notes: 0.5
---

# Data Rule

- Forbidden: personal data under KVKK (Turkish personal data protection law) / GDPR-style rules (full name + ID/contact info, national ID number, phone, email, address, IBAN/card, health data, customer records).
- Forbidden: secrets (passwords, tokens, keys, credentials embedded in connection strings).
- Mask before writing to logs, sample data, or error output: `***`, `example@example.com`, a fake name.
- If the guard hook blocks a commit, mask the data; if it's a false positive, add `guard:ignore` to the line.
- Vault files are never committed to unrelated repos (team, company, open source); use `.git/info/exclude` if needed.
