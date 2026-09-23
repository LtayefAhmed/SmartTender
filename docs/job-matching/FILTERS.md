# Evidence-based CV filters

The job-matching worker now checks experience, languages, certifications and education locally, using `backend/app/services/cv_filter_evidence.py`. No external model or API call is introduced. This module is separate from the legacy, unused `cv_profile.py`, which references unavailable model/database components.

## User behavior

Only profiles that confirm every requested criterion are shown by default. The checkbox **Afficher aussi les profils à vérifier et non conformes** reveals other returned profiles and their reasons. Assessments happen for the entire eligible corpus before the result limit is applied. Results sort by confirmed, unverified, insufficient, then similarity score within each group. Totals cover the full eligible corpus, whereas only the requested top slice is returned.

| Filter | Rule |
|---|---|
| Minimum experience | An explicit stated duration must meet the threshold; otherwise use a conservative lower bound from employment dates |
| Languages | All requested languages need explicit supporting mentions |
| Certifications | All requested certifications need explicit supporting mentions |
| Education | At least one selected diploma must be explicitly supported; no automatic degree hierarchy or international equivalence |
| Technologies | Existing weighted similarity/coverage score remains unchanged |
| Age | Explicit stated age or complete birth date only; missing/conflicting information is unverified. Birth dates use day/month/year or ISO format. |

## Evidence and status

Each selected criterion returns `criterion`, `requested`, `status`, `evidence` (original CV lines) and `reason`. Experience also includes `observed` years and `method`. Each candidate returns `filter_status`, `filter_checks`, `filtered_out`, `filtered_reason`, and extracted `structured_profile` fields.

- `pass`: the required statement is supported by the extracted CV text.
- `unknown`: missing, ambiguous, conflicting, pending, expired or otherwise unconfirmed information. This is not proof the candidate lacks the qualification.
- `fail`: a clearly stated experience duration is below the minimum.

Aggregate status is `fail` if any check fails, otherwise `unknown` if any is unverified, otherwise `pass`. Both unknown and failed profiles are outside the confirmed shortlist but remain inspectable. With no requested criteria, candidates pass the filter stage as before.

`kept_total`, `unverified_total` and `filtered_total` count the three groups across all eligible CVs. `total_candidates` is the eligible corpus size. The similarity score is still a comparison index; filtering changes eligibility/order, not that numerical score.

## Extraction rules

The parser recognizes French/English section headings and qualification context. It uses word boundaries and a small alias dictionary, including French/English language names, PMP / Project Management Professional, Azure Fundamentals / AZ-900, and common diploma spellings. Unknown custom criteria require a matching phrase with appropriate context. It avoids treating a job title such as Scrum Master as proof of a Master degree or Scrum certification, and avoids treating a company certification as a personal credential.

Pending, training, negative and expired mentions do not establish a confirmed qualification. Rules are conservative rather than comprehensive: unusual layouts, OCR errors, unrecognized headings, Arabic-only statements, ambiguous credentials and unfamiliar synonyms may remain unverified. Evidence is a statement in a CV, not an independently verified certificate, diploma, fluency level or current credential validity. Language filters check the named language, not a proficiency threshold.

Experience first looks for explicit numeric French/English duration statements. Different stated durations are treated as conflicting. Statements such as `5+ years` are lower bounds: they prove a minimum of five years, but cannot establish that an eight-year requirement is unmet.

If no explicit duration is found, the parser reads date ranges inside recognized employment sections. It supports numeric month/year and common French/English month names, including current roles. Overlapping intervals are merged, education dates excluded, future-ending intervals ignored. Year-only ranges use December for the start and January for the end to avoid overstating tenure. Incomplete employment histories produce a lower bound; a lower bound below the threshold remains unknown rather than a definitive failure. Day-precise dates, job classification and full-time-equivalent experience are not calculated.

## Examples

```text
Profil : 7 ans d'expérience
Langues : Français, Anglais C1
Certifications
PMP
ITIL Foundation
Formation
Master en informatique
```

With minimum experience 5, languages French and English, certifications PMP and ITIL, and education Licence OR Master, every check passes. The UI displays the original line supporting each criterion.

`Certifications: PMP in progress` with requested PMP remains unverified. `2 years of experience` with minimum 5 fails. An absent education section remains unverified even if the document's similarity score is high.

## Tests

`test_cv_filter_evidence.py` covers extraction, aliases, evidence, unknown states, lower bounds and date overlap. `test_job_match_explanation.py` verifies that a confirmed lower-similarity candidate is retained before a high-similarity insufficient candidate when the limit is one. `test_job_match_api.py` validates experience and result-limit bounds.

The feature is applied on a fresh search. Existing completed Celery results keep their earlier payload. Changes to the parser/ranking require rebuilding the API image and recreating `worker-ai`; frontend changes require rebuilding/recreating `frontend`.
