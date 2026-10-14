# Module 3 — Génération documentaire

> Document de référence destiné à la rédaction du rapport.
> Matière brute : inventaires, tableaux, signatures, mesures.
> Couvre uniquement ce qui est implémenté et vérifié.

---

## 1. Analyse de besoin

### 1.1 Problème métier

Répondre à un appel d'offres public impose de produire un **dossier de réponse** : un CV par expert proposé, au format imposé par le bailleur de fonds, une matrice de conformité, une lettre d'accompagnement. Ce travail est manuel, répétitif, et sa qualité conditionne la recevabilité de l'offre.

Deux contraintes structurent tout le module :

| Contrainte | Conséquence technique |
|---|---|
| Le **format** est imposé par le bailleur (BEI, BAD, bureau d'études) | Les gabarits doivent être des **données**, extensibles sans redéploiement |
| Le document est une **pièce contractuelle** — l'expert peut être auditionné dessus | Aucune compétence ne peut être inventée ; tout fait doit être traçable |

### 1.2 Besoins fonctionnels

| # | Besoin | Réalisation |
|---|---|---|
| BF-1 | Figer un classement de profils pour le rendre opposable | `Shortlist` + `ShortlistEntry` |
| BF-2 | Permettre à un humain de retenir / écarter chaque profil | `PATCH /shortlists/{id}/entries/{id}` |
| BF-3 | Verrouiller la sélection avant toute production | `POST /shortlists/{id}/validate` |
| BF-4 | Ajouter un format de document sans modifier le programme | Upload `.docx` → `DocumentTemplate` |
| BF-5 | Produire un CV adapté au format du bailleur | `generate_cv()` + `TemplateKind.CV` |
| BF-6 | Produire une fiche expert, une matrice de conformité, une lettre | 3 autres `TemplateKind` |
| BF-7 | Reformuler les textes dans le vocabulaire de l'AO | `adapt_experiences()` |
| BF-8 | Contrôler automatiquement le document produit | `review()` + `repair_context()` |
| BF-9 | Soumettre chaque document à une approbation humaine tracée | `POST /documents/{id}/approve` |
| BF-10 | Capitaliser les dossiers approuvés | Collection Qdrant `past_responses` |
| BF-11 | Exporter le dossier final avec sa traçabilité | `POST /shortlists/{id}/export` |
| BF-12 | Restituer le journal d'audit de bout en bout | `GET /shortlists/{id}/audit` |

### 1.3 Besoins non fonctionnels

| # | Besoin | Réalisation mesurée |
|---|---|---|
| BNF-1 | Génération d'un CV adapté en moins de 15 s | **289 ms** en déterministe ; ~5 s/profil avec LLM |
| BNF-2 | Le LLM n'invente jamais une compétence | 4 garde-fous mécaniques (§ 6.3) |
| BNF-3 | Le service reste utilisable sans LLM | Repli déterministe systématique |
| BNF-4 | Isolation multi-organisation | `tenant_id` sur les 3 tables, filtré dans la requête |
| BNF-5 | Traçabilité append-only | `ExecutionLog` + colonnes de décision |
| BNF-6 | Souveraineté des données personnelles | Caviardage avant tout appel externe |
| BNF-7 | Reproductibilité d'un document | Versionnage des templates et des documents |

### 1.4 Acteurs

| Acteur | Rôle dans le module |
|---|---|
| **Chargé d'affaires** | Fige la sélection, décide profil par profil, génère, exporte |
| **Validateur** | Approuve ou renvoie un document ; son identité est enregistrée |
| **Bureau d'Études / Administrateur** | Dépose et retire les gabarits `.docx` |
| **Système (Celery `worker-ai`)** | Exécute la passe de reformulation LLM + contrôle sémantique |
| **Mistral API** (externe) | Reformule les missions, rédige les sections libres |

---

## 2. Architecture du code

### 2.1 Volumétrie

| Couche | Fichiers | Lignes |
|---|---|---|
| Services métier | 9 | ~3 100 |
| Routers API | 5 | ~1 400 |
| Modèles ORM | 3 | ~450 |
| Worker Celery | 1 | ~75 |
| Scripts de gabarits | 4 | ~600 |
| **Backend total** | **22** | **~5 600** |
| Composants React | 4 | 1 187 |
| Tests | 10 fichiers | **168 tests** |
| Migrations Alembic | 7 | — |

### 2.2 Arborescence

```
backend/app/
├── core/
│   └── enums.py                 ShortlistStatus · ShortlistDecision
│                                TemplateKind · GeneratedDocumentStatus
├── db/models/
│   ├── shortlist.py             Shortlist · ShortlistEntry
│   ├── template.py              DocumentTemplate
│   └── document.py              GeneratedDocument
├── services/
│   ├── shortlist.py             fige et verrouille la sélection
│   ├── templates.py             lit, valide et remplit un .docx
│   ├── cv_structure.py          chronologie datée extraite d'un CV
│   ├── generation.py            assemble le contexte, rend, contrôle
│   ├── adaptation.py            reformulation LLM + anti-invention
│   ├── compliance.py            matrice de conformité
│   ├── cover_letter.py          lettre d'accompagnement
│   ├── document_qa.py           4 contrôles + reprise ciblée
│   └── memory.py                référentiel RAG des réponses validées
├── api/routers/
│   ├── shortlists.py            lecture, décision, validation
│   ├── templates.py             bibliothèque de gabarits
│   ├── documents.py             production des documents
│   ├── approval.py              approbation / refus
│   └── export.py                archive ZIP + journal d'audit
└── workers/tasks/
    └── generation.py            adapt_cv_experiences (queue `ai`)

backend/scripts/
├── build_default_template.py    gabarit CV
├── build_expert_template.py     gabarit fiche expert
├── build_matrix_template.py     gabarit matrice de conformité
└── build_letter_template.py     gabarit lettre

frontend/src/
├── components/
│   ├── ShortlistPanel.tsx       décisions sur une sélection figée
│   └── DossierPanel.tsx         génération, QA, approbation, export
└── pages/
    ├── Dossiers.tsx             liste des sélections et de leurs pièces
    └── Templates.tsx            bibliothèque de gabarits
```

### 2.3 Principes d'architecture appliqués

| Principe | Mise en œuvre |
|---|---|
| **Services purs** | `shortlist.py`, `compliance.py`, `document_qa.py` n'ouvrent aucune session ; le router possède la transaction. Testables sans base. |
| **Injection de dépendance** | La fonction de similarité est passée en paramètre à `adapt_experiences()`, jamais importée — le service reste utilisable sans modèle. |
| **Séparation par profil de ressource** | L'encodeur (470 Mo résidents) vit sur `worker-ai` uniquement ; l'API l'interroge par tâche Celery. |
| **Dégradation gracieuse** | Tout échec LLM, Qdrant ou encodeur retombe sur le chemin déterministe. |
| **Données plutôt que code** | Les gabarits sont des fichiers uploadés, pas des classes. |

---

## 3. Modèle de données

### 3.1 `shortlists` — la sélection figée

| Attribut | Type | Null | Rôle |
|---|---|---|---|
| `id` | UUID | non | PK |
| `tenant_id` | String(64) | non | Isolation organisation |
| `tender_id` | UUID | **oui** | FK `tenders` · `ON DELETE SET NULL` |
| `tender_title` | String(1024) | oui | Copié — survit à la purge de l'AO |
| `label` | String(255) | oui | Nom donné par l'utilisateur |
| `status` | String(16) | non | `open` \| `validated` \| `archived` |
| `requirements` | JSONB | non | Passages d'exigences lus dans le DCE |
| `required_technologies` | String[] | non | Technologies exigées |
| `structured_requirements` | JSONB | oui | Exigences lues par le LLM |
| `weights` | JSONB | non | Poids de scoring en vigueur |
| `kept_total` | Integer | non | Profils au-dessus du verrou |
| `vetoed_total` | Integer | non | Profils écartés par le verrou |
| `created_by` | String(128) | oui | Qui a figé |
| `validated_by` | String(128) | oui | Qui a validé |
| `validated_at` | DateTime(tz) | oui | Quand |
| `note` | Text | oui | Commentaire libre |
| `created_at` / `updated_at` | DateTime(tz) | non | `TimestampMixin` |

**Index** : `(tenant_id, tender_id)` · `(tenant_id, status)` · `tender_id` · `created_by`

### 3.2 `shortlist_entries` — un candidat dans la sélection

| Attribut | Type | Null | Rôle |
|---|---|---|---|
| `id` | UUID | non | PK |
| `shortlist_id` | UUID | non | FK `shortlists` · `ON DELETE CASCADE` |
| `cv_id` | UUID | **oui** | FK `cvs` · `ON DELETE SET NULL` |
| `rank` | Integer | non | Rang 1-based, figé |
| `label` | String(512) | non | Nom affiché **copié** au moment de la décision |
| `filename` | String(512) | oui | Fichier CV source |
| `score` | Float | non | Score global |
| `similarity` | Float | non | Composante sémantique |
| `coverage` | Float | non | Composante couverture |
| `technology_ratio` | Float | non | Composante technologique |
| `matched_technologies` | String[] | non | Technologies attestées |
| `missing_technologies` | String[] | non | Technologies exigées non attestées |
| `vetoed` | Boolean | non | Écarté par le verrou lexical |
| `veto_reason` | String(512) | oui | Motif du veto |
| `evidence` | JSONB | non | Passages probants `[{requirement, score, passage}]` |
| `decision` | String(16) | non | `pending` \| `retained` \| `rejected` |
| `decided_by` | String(128) | oui | Qui a décidé |
| `decided_at` | DateTime(tz) | oui | Quand |
| `decision_note` | Text | oui | Motif humain |

**Index** : `(shortlist_id, rank)` · `(shortlist_id, decision)` · `cv_id`

### 3.3 `document_templates` — la bibliothèque de gabarits

| Attribut | Type | Null | Rôle |
|---|---|---|---|
| `id` | UUID | non | PK |
| `tenant_id` | String(64) | non | Isolation |
| `key` | String(128) | non | Slug stable entre versions |
| `label` | String(255) | non | Libellé lisible |
| `kind` | String(32) | non | `TemplateKind` |
| `funder` | String(64) | oui | `BEI`, `BAD`… `NULL` = gabarit maison |
| `version` | Integer | non | Incrémenté au redépôt |
| `is_active` | Boolean | non | Une seule version active par clé |
| `original_filename` | String(512) | non | Nom déposé |
| `storage_bucket` / `storage_key` | String | non | Emplacement MinIO |
| `content_type` | String(128) | non | MIME |
| `size_bytes` | Integer | non | Taille |
| `sha256` | String(64) | oui | Empreinte |
| `variables` | String[] | non | Placeholders découverts au dépôt |
| `uploaded_by` | String(128) | oui | Qui a déposé |
| `notes` | Text | oui | Commentaire |

**Contrainte d'unicité** : `(tenant_id, key, version)`

### 3.4 `generated_documents` — la pièce produite

| Attribut | Type | Null | Rôle |
|---|---|---|---|
| `id` | UUID | non | PK |
| `tenant_id` | String(64) | non | Isolation |
| `shortlist_id` | UUID | oui | FK · `SET NULL` |
| `entry_id` | UUID | oui | FK · `SET NULL` |
| `cv_id` | UUID | oui | FK · `SET NULL` |
| `tender_id` | UUID | oui | FK · `SET NULL` |
| `template_id` | UUID | oui | FK · `SET NULL` |
| `template_key` | String(128) | oui | **Copié** — survit au retrait du gabarit |
| `template_version` | Integer | oui | **Copié** |
| `kind` | String(32) | non | `TemplateKind` |
| `label` | String(512) | non | Personne ou objet du document |
| `filename` | String(512) | non | Nom du fichier produit |
| `storage_bucket` / `storage_key` | String | non | Emplacement MinIO |
| `content_type` | String(128) | non | MIME `.docx` |
| `size_bytes` | Integer | non | Taille |
| `version` | Integer | non | v1, v2… |
| `status` | String(16) | non | `draft` \| `approved` \| `superseded` |
| `watermarked` | Boolean | non | Filigrane présent dans le fichier |
| `empty_fields` | String[] | non | Rubriques non renseignées |
| `adaptation` | JSONB | non | Rapport de la passe LLM |
| `qa` | JSONB | non | Rapport du contrôle automatique |
| `generated_by` | String(128) | oui | Qui a produit |
| `approved_by` | String(128) | oui | Qui a approuvé |
| `approved_at` | DateTime(tz) | oui | Quand |
| `decision_note` | Text | oui | Motif de refus |

**Index** : `(shortlist_id, created_at)` · `(tenant_id, status)` · `cv_id` · `tender_id`

### 3.5 Relations

```
Tender  1 ──── 0..n  Shortlist          SET NULL
Shortlist 1 ──── 1..n  ShortlistEntry   CASCADE
CV      1 ──── 0..n  ShortlistEntry     SET NULL
Shortlist 1 ──── 0..n  GeneratedDocument SET NULL
ShortlistEntry 1 ─── 0..n GeneratedDocument SET NULL
DocumentTemplate 1 ── 0..n GeneratedDocument SET NULL
CV      1 ──── 0..n  GeneratedDocument  SET NULL
```

**Règle de conception** : toutes les clés étrangères sont `SET NULL`, jamais `CASCADE` (sauf `Shortlist → ShortlistEntry`). Un AO purgé par rétention ou un CV supprimé à la demande d'un candidat ne doit pas effacer la trace qu'une décision humaine a été prise.

### 3.6 Énumérations

| Énumération | Valeurs |
|---|---|
| `ShortlistStatus` | `open` · `validated` · `archived` |
| `ShortlistDecision` | `pending` · `retained` · `rejected` |
| `TemplateKind` | `cv` · `fiche_expert` · `lettre` · `matrice_conformite` · `formulaire_tech` · `formulaire_fin` |
| `GeneratedDocumentStatus` | `draft` · `approved` · `superseded` |

---

## 4. Technologies

### 4.1 Pile du module

| Technologie | Version | Usage dans le module 3 |
|---|---|---|
| **docxtpl** | ≥ 0.16 | Remplissage de gabarits `.docx` |
| **Jinja2** | 3.1 | Moteur de template, en `SandboxedEnvironment` |
| **python-docx** | ≥ 1.1 | Construction des gabarits par script, relecture des documents |
| **Mistral API** | `mistral-small-latest` | Reformulation, rédaction des sections libres |
| **ONNX Runtime** | CPU | Exécution de l'encodeur multilingue |
| `paraphrase-multilingual-MiniLM-L12-v2` | 384 dim | Contrôle de fidélité sémantique, référentiel RAG |
| **Qdrant** | 1.19 | Collection `past_responses` (cosinus) |
| **MinIO** | S3 | Stockage des gabarits, documents et archives |
| **Celery + Redis** | — | Tâche `adapt_cv_experiences` sur la queue `ai` |
| **SQLAlchemy 2.0** | async + sync | ORM |
| **Alembic** | — | 7 migrations |
| **FastAPI** | — | 18 endpoints |
| **React 18 + TypeScript** | — | 4 composants |

### 4.2 Sécurité du moteur de template

Un `.docx` déposé contient du **Jinja2 exécutable**. Le rendu s'exécute donc dans un `SandboxedEnvironment`.

| Environnement | `{{ ''.__class__.__mro__[1].__subclasses__() }}` |
|---|---|
| `jinja2.Environment` | Retourne la chaîne de classes → exécution de code possible |
| `jinja2.sandbox.SandboxedEnvironment` | `SecurityError: access to attribute '__class__' of 'str' object is unsafe` |

S'y ajoute le `UploadValidator` existant : un `.docx` contenant `word/vbaProject.bin` est refusé au dépôt.

---

## 5. Fonctionnalités implémentées

### 5.1 Sélection figée

| Fonction | Signature | Comportement |
|---|---|---|
| `build_shortlist` | `(outcome, *, tenant, created_by, label) -> Shortlist` | Fige le résultat d'un matching : scores **et** exigences, technologies, poids |
| `apply_decision` | `(entry, *, decision, actor, note, now) -> ShortlistEntry` | Enregistre la décision ; lève `ShortlistLocked` si validée |
| `validate_shortlist` | `(shortlist, *, actor, now) -> Shortlist` | Verrouille ; **ne convertit jamais** un `pending` en `rejected` |
| `retained_entries` | `(shortlist) -> list[ShortlistEntry]` | Seuls les `retained` explicites |
| `summarise` | `(shortlist, *, include_entries) -> dict` | Forme API |

**Règles métier**

| Règle | Justification |
|---|---|
| La capture conserve aussi les profils **refusés** par le verrou | Une sélection qui ne montre que ses gagnants n'est pas contestable |
| La validation ne décide rien à la place de l'humain | `pending` = *non sélectionné et non examiné* ≠ *examiné et refusé* |
| Une sélection **vide** peut être validée | « Personne ne convient » est une conclusion valable |
| Un profil écarté par le verrou **peut être retenu**, et garde sa marque | L'arbitrage humain prime, mais l'override reste visible |
| Les labels sont **copiés**, pas joints | Un CV réimporté ne doit pas réécrire ce que le validateur a vu |

### 5.2 Gabarits comme données

| Fonction | Signature | Comportement |
|---|---|---|
| `discover_variables` | `(content: bytes) -> set[str]` | Lit les placeholders du `.docx` |
| `check_variables` | `(found, *, kind) -> (unknown, unavailable)` | Confronte au catalogue |
| `render_template` | `(content, context) -> bytes` | Rend en bac à sable |
| `variables_for` | `(kind) -> tuple[Variable, ...]` | Catalogue par type |

**Catalogue de variables par type de document**

| `TemplateKind` | Nombre de variables |
|---|---|
| `cv` | 19 |
| `fiche_expert` | 19 |
| `matrice_conformite` | 13 |
| `lettre` | 10 |
| `formulaire_tech` / `formulaire_fin` | 7 (jeu commun) |

**Variables à plat et non imbriquées** (`{{ nom }}` plutôt que `{{ identite.nom }}`) : Jinja ne rapporte que les noms de premier niveau ; un contexte imbriqué ne serait validable que jusqu'à `identite`, et toute faute de frappe interne survivrait au dépôt.

**Trois refus au dépôt**, vérifiés sur l'API :

| Cas | Réponse |
|---|---|
| Variable inconnue | `422` — « demande des variables que la plateforme ne sait pas fournir : *salaire_souhaite* » |
| Jinja malformé | `422` — « Expected an expression, got 'end of statement block' » |
| Macro VBA | `422` — « Document contains a macro project and was refused. » |

**Versionnage** : redéposer la même clé crée une v2 et désactive la précédente, qui reste lisible. Le retrait désactive sans supprimer.

### 5.3 Chronologie de carrière

`extract_structure(text) -> CvStructure` produit `experiences[]` et `formations[]`.

**Méthode** : la **plage de dates sert d'ancre**, les champs sont lus dans les lignes voisines. L'extraction est fragmentée par le pipeline OCR (un jeton par ligne), ce qui interdit toute règle attendant une date et son libellé sur la même ligne.

**Résultats mesurés sur 462 CV**

| Indicateur | Valeur |
|---|---|
| CV avec ≥ 1 expérience | 422 (**91 %**) |
| CV avec ≥ 1 formation | 329 (**71 %**) |
| Entrées d'expérience totales | 1 870 |
| — avec un intitulé de poste | 1 675 (89 %) |
| — avec des missions | 1 821 (97 %) |
| Durée de l'extraction sur tout le corpus | **6,5 s** |

**Règles de prudence**

| Règle | Effet mesuré |
|---|---|
| L'intitulé est identifié par un **lexique de métiers**, pas par sa position | Supprime le classement d'un poste en « employeur » |
| Un candidat hors lexique doit avoir la **forme** d'un titre (majuscule initiale, ≤ 8 mots, pas de mot de liaison final) | Fragments pris pour des intitulés : 29 → **2** |
| La même règle s'applique au champ **employeur** | Empêche le fragment de se déplacer d'un champ à l'autre |
| Les diplômes ne sont lus que **sous un en-tête Formation** | `M.S` est un master sous « Education » et une partie de « MS Office » ailleurs |
| Au-delà de **25 entrées**, le CV est déclaré non compris | Éviter d'inventer une frontière |

### 5.4 Les quatre documents produits

| Document | Portée | Moteur | Mesure |
|---|---|---|---|
| **CV adapté** | 1 par profil retenu | Déterministe (+ LLM optionnel) | **289 ms** |
| **Fiche expert** | 1 par profil retenu | Déterministe | même moteur, gabarit différent |
| **Matrice de conformité** | 1 par dossier | **100 % déterministe** | **194 ms** |
| **Lettre d'accompagnement** | 1 par dossier | LLM + repli déterministe | — |

#### Matrice de conformité

`build_matrix(shortlist, entries) -> ComplianceMatrix`

| Section | Statut possible | Source |
|---|---|---|
| Technologies exigées | `couverte` \| `non_couverte` | `required_technologies` × `matched_technologies` |
| Exigences du dossier | `a_traiter` **uniquement** | `structured_requirements.exigences` |

**Règle d'intégrité** : le taux de couverture s'écrit

```
taux = couvertes / (couvertes + non_couvertes)
```

Les exigences non évaluables depuis un CV (méthode, prix, engagement de présence) sont **exclues du dénominateur** et comptées séparément. Sans cette exclusion, un dossier afficherait « 92 % de couverture » en ayant sauté « Décrire la méthode envisagée » et « Estimer le coût global ».

**Résultat sur un AO réel** (Solution Oktotis, GIP OKANTIS) : 11 couvertes · 15 non couvertes · **42 %** · 17 à rédiger.

Un extrait n'est cité que s'il **contient** la technologie de la ligne ; sinon la cellule reste vide avec la mention « attesté dans le CV, hors des passages retenus par le classement ».

#### Lettre d'accompagnement

`compose_letter(brief: LetterBrief) -> CoverLetter`

Le `LetterBrief` ne contient que ce que la lettre a le droit d'affirmer : acheteur, référence, intitulé, échéance, équipe, **technologies attestées**. Les lacunes sont **comptées et retenues** — jamais transmises au modèle.

| Statut | Signification |
|---|---|
| `written` | Rédigée par le modèle, contrôle passé |
| `fallback` | Brouillon refusé → lettre déterministe produite |
| `unavailable` | LLM indisponible → lettre déterministe produite |

**Refus entier** et non partiel : une lettre est un seul argument ; il n'existe pas de fragment honnête à conserver.

### 5.5 Contrôle automatique

`review(text, *, context, kind) -> QaReport` inspecte **le document rendu**, pas le contexte.

| Contrôle | Sévérité | Détecte |
|---|---|---|
| `format` | bloquant | Marqueurs `{{ }}` non remplacés |
| `prompt` | bloquant | Mots du modèle dans la prose (` ``` `, « Voici la reformulation », `"pertinence"`) |
| `completude` | bloquant / avertissement | Nom absent du fichier ; rubriques vides |
| `coherence` | bloquant / avertissement | Fin avant début, date future, année < 1950 |

Chaque constat **nomme sa section** (`experiences[2]`), ce qui permet la reprise ciblée.

`repair_context(context, report, source) -> (context, reverted)` remet les paragraphes en échec au texte du CV source.

**Propriété de terminaison** : la boucle fait au plus **deux passes**, car le texte source passe ces contrôles par construction. Le repli retrouve le passage source par ses **dates et son intitulé**, pas par sa position — l'adaptation réordonne par pertinence.

### 5.6 Validation humaine

| Endpoint | Effet |
|---|---|
| `POST /documents/{id}/approve` | Statut `approved`, `approved_by` + `approved_at` enregistrés, **document re-rendu sans filigrane**, texte indexé au référentiel |
| `POST /documents/{id}/reject` | Statut `superseded`, motif conservé |

**Point technique important** : le filigrane est **rendu dans le fichier `.docx`**, pas attaché à la ligne. L'approbation re-rend donc le document — même gabarit, même contexte, mêmes lignes stockées — avec `filigrane=""`. Le re-rendu s'effectue **sans le modèle**, même si le document avait été adapté : relancer le LLM produirait une formulation différente de celle qu'un humain vient de signer.

Vérification sur fichier réel après approbation :

```
colonne watermarked = False
« EN ATTENTE DE VALIDATION » présent dans le .docx : False
```

### 5.7 Référentiel RAG des réponses validées

Troisième collection Qdrant : `past_responses` (384 dimensions, cosinus), à côté de `tender_passages` et `cv_passages`.

| Fonction | Signature |
|---|---|
| `index_response` | `(*, document_id, text, tenant, kind, tender_title, label) -> int` |
| `recall_responses` | `(query, *, tenant, limit=3) -> list[RetrievedResponse]` |

| Règle | Justification |
|---|---|
| Seuls les documents **approuvés** y entrent | Un brouillon est un avis ; un dossier approuvé est signé |
| Les passages rappelés sont du **style**, jamais des faits | Une réponse passée décrit le travail d'autrui sur une autre mission |
| Plancher de similarité **0,35** | Un exemple hors sujet est pire qu'aucun exemple |
| Limite de **3 passages** | Laisser la place au CV, seule source vérifiable |
| Identifiants de points **déterministes** (`uuid5`) | Réindexer écrase au lieu de dupliquer |

**Boucle fermée, mesurée** :

```
avant approbation   collection past_responses : inexistante
approbation         4 passages indexés
génération suivante memory.recalled → 3 passages rappelés
```

### 5.8 Export et audit

| Endpoint | Contenu |
|---|---|
| `POST /shortlists/{id}/export` | Archive ZIP + `MANIFESTE.txt`, lien signé à durée limitée |
| `GET /shortlists/{id}/audit` | Sélection (poids, exigences, décisions) + documents + journal |

**Seuls les documents approuvés sont empaquetés.** Refus explicite sinon :

> `422` — « Aucun document approuvé pour ce dossier. *N* brouillon(s) en attente : un dossier de brouillons porte le filigrane "en attente de validation" sur chaque page. »

Le manifeste voyage **dans** l'archive et porte, par pièce : type, gabarit et version, approbateur, mode de rédaction (`déterministe` ou `modèle + contrôle automatique`).

---

## 6. La passe LLM et ses garde-fous

### 6.1 Positionnement

| Étape | Moteur |
|---|---|
| Lecture de la chronologie du CV | Expressions régulières |
| Remplissage du gabarit | docxtpl / Jinja |
| **Reformulation des missions** | **Mistral** |
| **Rédaction de la lettre** | **Mistral** |
| Matrice de conformité | Déterministe — aucun modèle |
| Contrôle de fidélité | Encodeur ONNX |

### 6.2 Périmètre autorisé

| Opération | Autorisée | Raison |
|---|---|---|
| **Reformuler** — « dev Java/Spring » → « conception d'applications transactionnelles J2EE » | ✅ | La compétence existe, seul le vocabulaire change |
| **Sélectionner** — remonter 4 missions pertinentes sur 15 | ✅ | Rien d'ajouté, l'ordre change |
| **Compléter** — « a fait du Spring Boot » → « maîtrise les microservices » | ❌ | Déduction : indéfendable devant un bailleur |

### 6.3 Les quatre garde-fous

| Garde-fou | Mécanisme | Portée |
|---|---|---|
| **Technologies** | Lexique de 87 technologies : tout terme absent de la source est refusé | Rejet |
| **Acronymes** | Tout sigle majuscule absent de la source est refusé (`RGAA`, `TMA`, `HDS`) | Rejet |
| **Années** | Toute année absente de la source est refusée | Rejet |
| **Appui sémantique** | Similarité par phrase contre le meilleur appui de la source | `< 0,22` rejet · `0,22–0,45` signalement |

Les trois premiers sont **déterministes et invariants à la langue** — ce qui est indispensable ici : le corpus est anglophone et la sortie francophone, donc toute comparaison lexicale de prose ordinaire est défaite par la traduction.

**Calibrage du garde-fou sémantique** (mesures sur paires réelles) :

| Cas | Appui par phrase |
|---|---|
| Invention « e-commerce » | 0,176 |
| Hors sujet total | 0,193 |
| Compression fidèle | 0,257 |
| Vidé de sa substance | 0,342 |
| Reformulation fidèle | 0,418 |

L'écart de 0,064 entre une invention et une compression fidèle n'autorise pas un seuil de fidélité. Le contrôle est donc scindé : plancher d'absurdité à 0,22 (rejet) et signalement entre 0,22 et 0,45 (conservé, nommé au relecteur).

### 6.4 Verrous de souveraineté

| Verrou | Mise en œuvre |
|---|---|
| **Portée** | `kind` ∈ {`tender`, `cv`} vérifié à chaque appel ; échec fermé sur un type inconnu |
| **Caviardage** | Noms, e-mails, téléphones retirés **avant** l'envoi (`redaction.py`, 28 tests) |
| **Plafond** | 12 000 caractères en entrée, jetons bornés en sortie |
| **Traçabilité** | `tokens_in`, `tokens_out`, `model` lus de la réponse et journalisés |

Le nom réel n'est jamais transmis au modèle : le rendu déterministe le réinjecte après coup.

---

## 7. API — 18 endpoints

| Méthode | Chemin | Rôle |
|---|---|---|
| `POST` | `/tenders/{id}/shortlist` | Fige le classement en sélection |
| `GET` | `/shortlists` | Liste les sélections (+ nombre de pièces) |
| `GET` | `/shortlists/{id}` | Une sélection avec ses candidats |
| `PATCH` | `/shortlists/{id}/entries/{id}` | Retenir / écarter un candidat |
| `POST` | `/shortlists/{id}/validate` | Verrouille la sélection |
| `POST` | `/shortlists/{id}/documents` | Produit les documents |
| `GET` | `/shortlists/{id}/documents` | Documents d'une sélection |
| `POST` | `/shortlists/{id}/export` | Archive ZIP + lien signé |
| `GET` | `/shortlists/{id}/audit` | Journal de bout en bout |
| `POST` | `/documents/{id}/approve` | Approuve, retire le filigrane, indexe |
| `POST` | `/documents/{id}/reject` | Renvoie avec motif |
| `GET` | `/documents/{id}/download` | Lien signé |
| `GET` | `/templates` | Bibliothèque |
| `POST` | `/templates` | Dépôt d'un `.docx` |
| `GET` | `/templates/variables` | Catalogue par type |
| `GET` | `/templates/{id}/download` | Lien signé |
| `DELETE` | `/templates/{id}` | Retire une version |
| `GET` | `/stats/journey` | Compteurs des 4 phases |

**Codes de refus** : `409` sélection non validée ou document déjà approuvé · `422` aucun profil retenu, gabarit absent, variable inconnue · `404` ressource d'une autre organisation (volontairement indistinct d'une ressource inexistante).

---

## 8. Enchaînements principaux

### 8.1 Figer une sélection

```
Chargé d'affaires → POST /tenders/{id}/shortlist
  API → tâche Celery rank_tender_candidates (queue ai, priorité interactive)
    worker-ai → encodeur ONNX + Qdrant → classement
  API ← résultat
  API → build_shortlist(outcome) → Shortlist + N ShortlistEntry
  API → session.flush()
  API ← 201 summarise(shortlist)
```

Le classement est **rejoué côté serveur** plutôt que repris du navigateur : une capture est un enregistrement d'audit, et un enregistrement fourni par le client n'en est pas un.

### 8.2 Générer un CV adapté

```
POST /shortlists/{id}/documents {kind: cv, adapt: true}
  vérifie status == validated           sinon 409
  vérifie retained non vide             sinon 422
  charge le template actif depuis MinIO
  pour chaque entry retenue :
    generate_cv(template, cv, entry, shortlist, tender, adapter)
      build_cv_context()
        adapter() → tâche Celery adapt_cv_experiences (queue ai)
          recall_responses()        référentiel RAG
          adapt_experiences()       Mistral + 4 garde-fous
        ← experiences adaptées + rapport
      render_template()             docxtpl, bac à sable
      review()                      4 contrôles
      si bloquant : repair_context() + render_template() + review()
    storage.put_bytes()
    GeneratedDocument(status=draft, watermarked=True)
  ← 201 {produced, refused, template}
```

### 8.3 Approuver et capitaliser

```
POST /documents/{id}/approve
  vérifie status != approved / superseded   sinon 409
  recharge template + cv + entry + shortlist + tender
  generate_cv(..., approved=True)           sans adapter
  storage.put_bytes(même clé)               écrase le filigrané
  status=approved, approved_by, approved_at, watermarked=False
  _plain_text(contenu) → index_response()   → Qdrant past_responses
  ← 200 {status, watermarked, indexed_passages}
```

### 8.4 Cycle de vie d'un document

```
          generate            approve
  (néant) ────────▶ draft ─────────────▶ approved
                      │                      │
                      │ reject               │ regenerate
                      ▼                      ▼
                  superseded ◀──────── (nouvelle version)
```

---

## 9. Mesures et validation

### 9.1 Performance

| Opération | Mesure |
|---|---|
| Lecture base | 87 ms |
| Gabarit depuis MinIO | 147 ms |
| Rendu `.docx` | 55 ms |
| **CV déterministe — total** | **289 ms** (objectif : 15 000 ms) |
| Matrice de conformité | 194 ms |
| 2 CV avec adaptation LLM | ~10 s |
| Export ZIP d'un dossier | < 1 s |
| Extraction de chronologie, 462 CV | 6,5 s |

### 9.2 Couverture de tests — 168 tests

| Fichier | Tests | Objet |
|---|---|---|
| `test_cv_structure.py` | 30 | Ancrage sur les dates, forme des intitulés, anonymisation |
| `test_adaptation.py` | 26 | Garde-fous anti-invention, sélection, repli |
| `test_shortlist.py` | 19 | Capture, décisions, verrou |
| `test_document_qa.py` | 16 | 4 contrôles, terminaison de la boucle |
| `test_templates.py` | 15 | Bac à sable, catalogue, gabarit par défaut |
| `test_cover_letter.py` | 15 | Périmètre des affirmations, repli |
| `test_compliance.py` | 14 | Couverture, preuves, exclusion du dénominateur |
| `test_generation.py` | 13 | Refus, filigrane, provenance |
| `test_memory.py` | 11 | Référentiel : entrée, rappel, état vide |
| `test_documents.py` | 9 | Versionnage, survie de la trace |

**Suite complète du projet : 854 tests.**

### 9.3 Corpus de validation

| Donnée | Volume |
|---|---|
| Appels d'offres | 532 |
| CV | 462 |
| Passages vectorisés (AO + CV) | ~6 800 |
| Sélections figées | 6 |
| Documents produits | 30+ |

---

## 10. Interface utilisateur

| Composant | Lignes | Rôle |
|---|---|---|
| `ShortlistPanel.tsx` | 291 | Sélection figée : décisions, validation, verrou |
| `DossierPanel.tsx` | 398 | Génération, rapports QA et LLM, approbation, export |
| `Dossiers.tsx` | 150 | Liste des sélections et de leurs pièces |
| `Templates.tsx` | 348 | Bibliothèque de gabarits, catalogue de variables |

**Éléments d'interface notables**

| Élément | Information portée |
|---|---|
| Sélecteur de type | CV / fiche expert / matrice / lettre, avec la portée (par profil ou par dossier) |
| Case « Adapter les textes » | Décochée par défaut ; absente sur la matrice |
| Rapport d'adaptation | *N* reformulés, *N* rejetés, termes refusés |
| Phrases signalées | Citées intégralement, appui faible dans la source |
| Rapport QA | Avertissements et paragraphes restaurés |
| Badge de statut | `brouillon` / `approuvé` / `matrice` / `lettre` / `fiche expert` |
| Sections non renseignées | Listées par document |

---

## 11. Annexe — constantes et seuils

| Constante | Valeur | Fichier | Rôle |
|---|---|---|---|
| `_MAX_EXPERIENCES` | 8 | `generation.py` | Expériences dans le contexte de rendu |
| `_MAX_FORMATIONS` | 6 | `generation.py` | Formations dans le contexte |
| `WATERMARK` | « GÉNÉRÉ — EN ATTENTE DE VALIDATION » | `generation.py` | Mention d'état |
| `_MAX_SUBMITTED` | 10 | `adaptation.py` | Rôles soumis au modèle |
| `_MAX_GROWTH` | 1,6 | `adaptation.py` | Croissance maximale d'une reformulation |
| `_UNSUPPORTED` | 0,22 | `adaptation.py` | Plancher d'absurdité (rejet) |
| `_THIN` | 0,45 | `adaptation.py` | Seuil de signalement |
| `_MIN_SENTENCE` | 25 | `adaptation.py` | Longueur minimale d'une phrase comparable |
| `_MAX_ENTRIES` | 25 | `cv_structure.py` | Au-delà, le CV est déclaré non compris |
| `_MISSION_CHARS` | 600 | `cv_structure.py` | Taille du bloc missions |
| `_MAX_EVIDENCE` | 2 | `compliance.py` | Citations par ligne de matrice |
| `_EVIDENCE_CHARS` | 220 | `compliance.py` | Longueur d'une citation |
| `_PARAGRAPHS` | 3 | `cover_letter.py` | Paragraphes de la lettre |
| `_MAX_PARAGRAPH_CHARS` | 700 | `cover_letter.py` | Longueur d'un paragraphe |
| `_IMPLAUSIBLE_BEFORE` | 1950 | `document_qa.py` | Année en deçà de laquelle une date est douteuse |
| `_RECALL_LIMIT` | 3 | `memory.py` | Passages rappelés du référentiel |
| `_MIN_SCORE` | 0,35 | `memory.py` | Plancher de pertinence du rappel |

---

## 12. Annexe — migrations Alembic

| Révision | Objet |
|---|---|
| `0f60718293a4` | `shortlists` + `shortlist_entries` |
| `1071829304b5` | `document_templates` |
| `11829304b5c6` | Colonne `cvs.structure` (chronologie) |
| `1229304b5c6d` | `generated_documents` |
| `1329304b5c6e` | Colonne `generated_documents.adaptation` |
| `1429304b5c6f` | Colonnes d'approbation (`approved_by`, `approved_at`, `decision_note`) |
| `1529304b5c70` | Colonne `generated_documents.qa` |
