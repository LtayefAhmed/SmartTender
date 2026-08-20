"""
Renders the AO fit-analysis results as a single self-contained HTML dashboard.
No JS framework, no external assets, no network calls -- opens straight from
disk in any browser. Called by match_ao.py after it computes real results
per required profile; never fed placeholder data. UI text is in French;
underlying data (résumé categories, matched excerpts) stays in the language
the résumés themselves were written in (English, in this dataset).
"""

import html

# Status color per grade letter (from the shared palette's reserved status
# role -- never reused as a plain series color).
GRADE_STATUS = {
    "A": "good",
    "B": "good",
    "C": "warning",
    "D": "serious",
    "F": "critical",
}

GRADE_WORD_FR = {
    "Excellent fit": "Excellent profil",
    "Strong fit": "Bon profil",
    "Moderate fit": "Profil moyen",
    "Weak fit": "Profil faible",
    "No fit / mismatch": "Aucune correspondance",
}

CSS = """
:root {
  color-scheme: light;
  --page:      #f9f9f7;
  --surface:   #fcfcfb;
  --ink-1:     #0b0b0b;
  --ink-2:     #52514e;
  --ink-3:     #898781;
  --hairline:  #e1e0d9;
  --baseline:  #c3c2b7;
  --blue:      #2a78d6;
  --good:      #0ca30c;
  --warning:   #b8790c;
  --serious:   #c1552f;
  --critical:  #c1302f;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page:      #0d0d0d;
    --surface:   #1a1a19;
    --ink-1:     #ffffff;
    --ink-2:     #c3c2b7;
    --ink-3:     #898781;
    --hairline:  #2c2c2a;
    --baseline:  #383835;
    --blue:      #3987e5;
    --good:      #0ca30c;
    --warning:   #d99b1f;
    --serious:   #e17e56;
    --critical:  #e66767;
  }
}
* { box-sizing: border-box; }
html { font-size: 17px; }
body {
  margin: 0;
  background: var(--page);
  color: var(--ink-1);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
  line-height: 1.5;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 52px 28px 90px; }
header { margin-bottom: 36px; }
.eyebrow { font-size: 0.8rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-3); margin: 0 0 8px; }
h1 { font-size: 1.75rem; font-weight: 700; margin: 0; }
.meta { font-size: 1rem; color: var(--ink-3); margin-top: 8px; }

.project-hero {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 28px;
  align-items: center;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-left: 5px solid var(--status);
  padding: 28px;
  margin-bottom: 32px;
}
.project-grade { font-size: 3rem; font-weight: 700; line-height: 1; color: var(--status); }
.project-grade-word { font-size: 0.95rem; color: var(--ink-2); margin-top: 6px; }
.project-note { font-size: 1rem; color: var(--ink-2); line-height: 1.65; }
.project-note b { color: var(--ink-1); }

/* Overview: scan every required profile at a glance -- grade, and a real bar
   for relevance, not just a number -- then jump straight to its detail card. */
.overview { margin-bottom: 44px; border: 1px solid var(--hairline); }
.overview table { margin-top: 0; }
.overview tbody tr { cursor: pointer; }
.overview tbody tr:hover { background: var(--surface); }
.overview td.role { font-weight: 600; color: var(--ink-1); }
.overview td.role a { color: inherit; text-decoration: none; }
.overview td.role a:hover { text-decoration: underline; }
.overview .grade-chip { display: inline-flex; align-items: baseline; gap: 8px; font-weight: 700; color: var(--row-status); font-size: 1.05rem; }
.overview .grade-chip .word { font-weight: 400; font-size: 0.8rem; color: var(--ink-3); }
.overview .rel-cell { display: flex; align-items: center; gap: 10px; min-width: 160px; }
.overview .rel-track { flex: 1; height: 14px; background: var(--page); border: 1px solid var(--hairline); border-radius: 7px; overflow: hidden; }
.overview .rel-fill { height: 100%; border-radius: 7px; background: var(--row-status); }
.overview .rel-val { font-size: 0.85rem; font-variant-numeric: tabular-nums; color: var(--ink-2); width: 3.2em; text-align: right; }

.profile {
  border: 1px solid var(--hairline);
  border-left: 5px solid var(--status);
  margin-bottom: 32px;
  scroll-margin-top: 24px;
}
.profile-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  padding: 20px 24px;
  border-bottom: 1px solid var(--hairline);
  background: var(--surface);
}
.profile-title { font-size: 1.2rem; font-weight: 700; }
.profile-need { font-size: 0.85rem; color: var(--ink-3); margin-top: 3px; }

/* Grade+relevance donut: the ring sweep is the top candidate's relevance
   (0-100%), the letter in the center is the overall grade -- one glance
   carries both the pass/fail signal and how confident it is. */
.grade-donut-wrap { display: flex; align-items: center; gap: 14px; }
.grade-donut {
  --pct: 0;
  width: 76px;
  height: 76px;
  border-radius: 50%;
  background: conic-gradient(var(--status) calc(var(--pct) * 3.6deg), var(--hairline) 0deg);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.grade-donut-inner {
  width: 58px;
  height: 58px;
  border-radius: 50%;
  background: var(--surface);
  display: flex;
  align-items: center;
  justify-content: center;
}
.grade-donut-inner .letter { font-size: 1.6rem; font-weight: 700; color: var(--status); }
.grade-caption .word { font-size: 0.95rem; font-weight: 600; color: var(--ink-1); }
.grade-caption .pct { font-size: 0.8rem; color: var(--ink-3); margin-top: 2px; }

.profile-body { padding: 26px; }
.coverage-warning {
  display: flex;
  gap: 12px;
  font-size: 0.9rem;
  line-height: 1.55;
  color: var(--ink-1);
  background: var(--page);
  border: 1px solid var(--hairline);
  border-left: 3px solid var(--warning);
  padding: 14px 18px;
  margin-bottom: 24px;
}
.coverage-warning .icon { color: var(--warning); flex-shrink: 0; }

.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 22px; margin-bottom: 30px; }
.stat .label {
  font-size: 0.78rem;
  color: var(--ink-3);
  margin: 0 0 7px;
  text-decoration: underline dotted var(--baseline);
  text-underline-offset: 3px;
  cursor: help;
}
.stat .value { font-size: 1.35rem; font-weight: 700; font-variant-numeric: tabular-nums; margin: 0; }
.stat .sub { font-size: 0.78rem; color: var(--ink-3); margin-top: 3px; }

h2 { font-size: 0.85rem; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; color: var(--ink-2); margin: 30px 0 14px; }
h2:first-child { margin-top: 0; }

/* Category-fit bars: real chart weight (20px), rounded, value at the end of
   the fill so the number and the magnitude read as one shape, not two. */
.bar-row { display: grid; grid-template-columns: 190px 1fr 64px; align-items: center; gap: 14px; padding: 8px 0; }
.bar-cat { font-size: 0.9rem; color: var(--ink-1); }
.bar-track { height: 20px; background: var(--page); border: 1px solid var(--hairline); border-radius: 5px; overflow: hidden; }
.bar-fill { height: 100%; background: var(--blue); border-radius: 5px; }
.bar-val { font-size: 0.85rem; font-variant-numeric: tabular-nums; color: var(--ink-2); text-align: right; }

table { width: 100%; border-collapse: collapse; margin-top: 14px; }
th { text-align: left; font-size: 0.78rem; font-weight: 700; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.03em; padding: 0 14px 12px; border-bottom: 1px solid var(--baseline); }
td { padding: 14px; border-bottom: 1px solid var(--hairline); vertical-align: top; font-size: 0.9rem; line-height: 1.55; }
td.score { font-variant-numeric: tabular-nums; font-weight: 700; white-space: nowrap; }
td.id { color: var(--ink-3); font-variant-numeric: tabular-nums; white-space: nowrap; }
td.cat { color: var(--ink-2); white-space: nowrap; }
td.snippet { color: var(--ink-2); }
td.snippet .quote { color: var(--ink-3); }
tr:last-child td { border-bottom: none; }

.back-to-top { display: inline-block; font-size: 0.85rem; color: var(--ink-3); text-decoration: none; margin-top: 18px; }
.back-to-top:hover { color: var(--ink-1); text-decoration: underline; }

footer { font-size: 0.8rem; color: var(--ink-3); margin-top: 44px; border-top: 1px solid var(--hairline); padding-top: 18px; line-height: 1.6; }

@media (max-width: 640px) {
  html { font-size: 16px; }
  .stat-row { grid-template-columns: repeat(2, 1fr); }
  .bar-row { grid-template-columns: 120px 1fr 52px; }
  .project-hero { grid-template-columns: 1fr; }
  .profile-header { flex-direction: column; align-items: flex-start; }
}
"""


def esc(s):
    return html.escape(str(s))


def grade_word_fr(word):
    return GRADE_WORD_FR.get(word, word)


def render_bars(category_avg):
    max_cat = max(v for _, v, _ in category_avg) or 1.0
    return "\n".join(
        f"""
        <div class="bar-row">
          <div class="bar-cat">{esc(cat)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:{(avg / max_cat) * 100:.1f}%"></div></div>
          <div class="bar-val">{avg:.3f}</div>
        </div>"""
        for cat, avg, n in category_avg[:6]
    )


def render_matches(matches):
    return "\n".join(
        f"""
        <tr>
          <td class="id">#{esc(m['id'])}</td>
          <td class="score">{m['score']:.3f}</td>
          <td class="cat">{esc(m['category'])}</td>
          <td class="snippet"><span class="quote">«</span> {esc(m['snippet'])} <span class="quote">»</span></td>
        </tr>"""
        for m in matches
    )


def render_overview(profiles):
    rows = "\n".join(
        f"""
        <tr onclick="location.hash='#profile-{i}'">
          <td class="role"><a href="#profile-{i}">{esc(p['title'])}</a></td>
          <td>{p['needed']}</td>
          <td>
            <span class="grade-chip" style="--row-status: var(--{GRADE_STATUS.get(p['grade_letter'], 'warning')})">
              {esc(p['grade_letter'])} <span class="word">{esc(grade_word_fr(p['grade_word']))}</span>
            </span>
          </td>
          <td>
            <div class="rel-cell" style="--row-status: var(--{GRADE_STATUS.get(p['grade_letter'], 'warning')})">
              <div class="rel-track"><div class="rel-fill" style="width:{p['top_score'] * 100:.1f}%"></div></div>
              <div class="rel-val">{p['top_score'] * 100:.0f}%</div>
            </div>
          </td>
        </tr>"""
        for i, p in enumerate(profiles)
    )
    return f"""
  <div class="overview">
    <table>
      <thead><tr><th>Poste</th><th>Requis</th><th>Note</th><th>Pertinence max</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
"""


def render_profile(p, idx):
    status_role = GRADE_STATUS.get(p["grade_letter"], "warning")
    warnings = []
    if p.get("coverage_warning"):
        warnings.append(
            "Faible couverture du bassin pour ce poste — les CV de ce bassin abordent rarement "
            "ce domaine, donc même la meilleure correspondance n'est peut-être pas fiable. Note plafonnée."
        )
    if p.get("relevance_warning"):
        warnings.append(
            f"La pertinence (cross-encoder) des {p['needed']} meilleur(s) candidat(s) requis n'est que de "
            f"{p['relevance_mean']:.2f} (échelle 0–1) — le classement paraissait statistiquement "
            f"favorable, mais un examen des candidats réels montre une correspondance faible. Note plafonnée."
        )
    warning_html = "".join(
        f'<div class="coverage-warning"><span class="icon">&#9888;</span><span>{esc(w)}</span></div>'
        for w in warnings
    )
    pct = p["top_score"] * 100
    return f"""
  <div class="profile" id="profile-{idx}" style="--status: var(--{status_role})">
    <div class="profile-header">
      <div>
        <div class="profile-title">{esc(p['title'])}</div>
        <div class="profile-need">{p['needed']} poste(s) requis</div>
      </div>
      <div class="grade-donut-wrap">
        <div class="grade-donut" style="--pct: {pct:.1f}">
          <div class="grade-donut-inner"><span class="letter">{esc(p['grade_letter'])}</span></div>
        </div>
        <div class="grade-caption">
          <div class="word">{esc(grade_word_fr(p['grade_word']))}</div>
          <div class="pct">{pct:.0f}% pertinence max</div>
        </div>
      </div>
    </div>
    <div class="profile-body">
      {warning_html}
      <div class="stat-row">
        <div class="stat">
          <p class="label" title="Le CV le plus pertinent après reclassement par le cross-encoder.">Meilleure correspondance</p>
          <p class="value">{p['top_score']:.3f}</p>
          <p class="sub">CV n° {p['top_id']}</p>
        </div>
        <div class="stat">
          <p class="label" title="Moyenne des {p['topk']} meilleurs scores de récupération initiale (avant reclassement) — c'est ce qui sert à calculer le score z de la note.">Signal de récupération</p>
          <p class="value">{p['topk_mean']:.3f}</p>
          <p class="sub">z = {p['z_score']:+.2f} vs bassin</p>
        </div>
        <div class="stat">
          <p class="label" title="Score moyen d'un CV pris au hasard dans tout le bassin face à ce poste. Faible = le bassin n'aborde presque jamais ce domaine.">Référence du bassin</p>
          <p class="value">{p['corpus_mean']:.3f}</p>
          <p class="sub">± {p['corpus_std']:.3f}</p>
        </div>
        <div class="stat">
          <p class="label" title="La catégorie de métier dont les CV obtiennent, en moyenne, le meilleur score face à ce poste.">Meilleure catégorie</p>
          <p class="value" style="font-size:1.05rem">{esc(p['category_avg'][0][0])}</p>
          <p class="sub">moy. {p['category_avg'][0][1]:.3f}</p>
        </div>
      </div>

      <h2>Adéquation par catégorie</h2>
      {render_bars(p['category_avg'])}

      <h2>Meilleurs CV correspondants — reclassés par pertinence</h2>
      <table>
        <thead><tr><th>ID</th><th>Pertinence</th><th>Catégorie</th><th>Extrait correspondant</th></tr></thead>
        <tbody>{render_matches(p['top_matches'])}</tbody>
      </table>

      <a class="back-to-top" href="#top">&#8593; Retour à la vue d'ensemble</a>
    </div>
  </div>
"""


def render_dashboard(data, output_path):
    """
    data = {
      "project_title": str,
      "pool_size": int,
      "overall_letter": "A".."F",
      "overall_word": str,
      "bottleneck_title": str,
      "profiles": [ { title, needed, grade_letter, grade_word, z_score,
                      top_score, top_id, topk, topk_mean, corpus_mean,
                      corpus_std, category_avg: [(cat, avg, n), ...],
                      top_matches: [{id, score, category, snippet}, ...] }, ... ]
    }
    """
    overall_status = GRADE_STATUS.get(data["overall_letter"], "warning")
    overview_html = render_overview(data["profiles"]) if len(data["profiles"]) > 1 else ""
    profiles_html = "\n".join(render_profile(p, i) for i, p in enumerate(data["profiles"]))

    html_doc = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analyse d'adéquation AO</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap" id="top">

  <header>
    <p class="eyebrow">Analyse d'adéquation AO · Projet</p>
    <h1>{esc(data['project_title'])}</h1>
    <p class="meta">{len(data['profiles'])} profil(s) requis · évalués sur {data['pool_size']} CV</p>
  </header>

  <div class="project-hero" style="--status: var(--{overall_status})">
    <div>
      <div class="project-grade">{esc(data['overall_letter'])}</div>
      <div class="project-grade-word">{esc(grade_word_fr(data['overall_word']))}</div>
    </div>
    <div class="project-note">
      La note globale du projet est fixée par le poste le <b>plus difficile à pourvoir</b> :
      le profil le plus dur à satisfaire avec le bassin actuel plafonne la note de tout le projet.
      Ici, il s'agit de <b>{esc(data['bottleneck_title'])}</b>.
    </div>
  </div>

  {overview_html}

  {profiles_html}

  <footer>
    Généré localement à partir de la collection Qdrant · récupération all-MiniLM-L6-v2 · reclassement cross-encoder ·
    les candidats sont classés indépendamment pour chaque profil et peuvent se chevaucher entre postes.
  </footer>

</div>
</body>
</html>
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
