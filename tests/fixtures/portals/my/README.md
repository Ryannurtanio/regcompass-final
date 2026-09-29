# Recorded Malaysian Portal answers

`fess_search_personal_data_protection_2026-09-16.json` is the verbatim response
body of ONE live request to the Malaysian Portal's search proxy
(`https://lom.agc.gov.my/fess-proxy.php`), made on 16 Sep 2026 under the
identified user agent, with the same parameters `discover_my` sends and the
query `"personal data protection"`. No PDF was downloaded and no second request
was made.

What it shows: the Portal now answers `{"encrypted": true, "data": "<base64>"}`.
The Solr envelope the adapter was written against (`response.docs`, each doc
carrying `DOC2DOWNLOADBI` / `..._GENERATEPDF`) is gone from the wire, which is
why the Malaysian search returns zero hits today. The ciphertext is 21,254 bytes,
not block-aligned and not OpenSSL-salted, so the payload cannot be read without
the key the Portal's own page script holds.

The adapter now RECOGNISES this envelope and says so, instead of reporting the
misleading "no portal hits ... (HTTP 200)". Reading the payload needs the page
script, which is a separate fetch and a separate decision.
