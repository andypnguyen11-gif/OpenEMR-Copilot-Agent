# Corpus source attribution

Every document under `corpus/sources/` is a *synthetic excerpt adapted
from public guidance* for the Week 2 demo. They are not the canonical
text and must not be used for clinical decisions — see each document's
`source_url` frontmatter for the authoritative version.

The Week 2 demo's permitted-source list is restricted to U.S.
public-health bodies whose guidance is published in the public domain
or under permissive licensing:

| Source | Basis |
|---|---|
| U.S. Preventive Services Task Force (USPSTF) | Federal works, public domain (17 U.S.C. § 105). |
| Centers for Disease Control and Prevention (CDC) | Federal works, public domain (17 U.S.C. § 105). |
| National Institutes of Health (NIH) / NHLBI | Federal works, public domain. |
| National Institutes of Health (NIH) / NIDDK | Federal works, public domain (17 U.S.C. § 105). |
| American Heart Association (AHA) — short adapted excerpts | Cited under fair-use for non-commercial educational demo; canonical text remains AHA's. |

The corpus is rebuilt from these markdown files via
`python -m clinical_copilot.corpus.index`. The build emits a
`manifest.json` with per-source URLs; the citation check uses that
manifest as the permitted-source allowlist.
