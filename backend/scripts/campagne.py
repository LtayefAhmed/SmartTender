"""Campagne de tests REELS contre les deux portails.

    python scripts/campagne.py            tous les scenarios
    python scripts/campagne.py pays       ceux dont le nom contient "pays"

Complement de la suite unitaire, pas un remplacement : celle-ci tourne sans
reseau et verifie la logique, celle-la interroge les vrais portails et verifie
que le contrat n'a pas bouge. A lancer apres un changement de connecteur, et
avant une demonstration.

Chaque scenario est lance par l'API, suivi jusqu'a son terme, puis JUGE :
le script dit lui-meme si le resultat est coherent, plutot que d'afficher des
chiffres qu'il faudrait interpreter a la main.
"""
import json
import sys
import time
import urllib.request

API = "http://localhost:8000"
HEAD = {"X-API-Key": "dev-local-key", "Content-Type": "application/json"}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(API + path, data=data, headers=HEAD, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


SCENARIOS = [
    # (libelle, sources, filtres, attente)
    ("1 mot-cle, OU", ["j360"],
     {"keywords": ["ERP"], "keywords_any": True}, "pousse au portail"),
    ("1 mot-cle, ET", ["j360"],
     {"keywords": ["ERP"], "keywords_any": False}, "pousse au portail"),
    ("2 mots-cles, OU", ["j360"],
     {"keywords": ["ERP", "SIRH"], "keywords_any": True}, "recherche par terme"),
    ("2 mots-cles, ET", ["j360"],
     {"keywords": ["cloud", "securite"], "keywords_any": False}, "pousse au portail"),
    ("pays minuscule", ["j360"],
     {"keywords": ["logiciel"], "countries": ["tunisie"]}, "pays au serveur"),
    ("pays majuscule", ["j360"],
     {"keywords": ["logiciel"], "countries": ["TUNISIE"]}, "pays au serveur"),
    ("pays sans accent", ["j360"],
     {"keywords": ["logiciel"], "countries": ["algerie"]}, "pays au serveur"),
    ("pays hors catalogue", ["j360"],
     {"keywords": ["logiciel"], "countries": ["Atlantide"]}, "repli local"),
    ("pays lointain reconnu", ["j360"],
     {"keywords": ["logiciel"], "countries": ["Japon"]}, "pays au serveur"),
    ("2 pays", ["j360"],
     {"keywords": ["cloud"], "countries": ["Tunisie", "Mauritanie"]}, "pays au serveur"),
    ("zone Afrique", ["j360"],
     {"keywords": ["logiciel"], "countries": ["Afrique"]}, "pays au serveur"),
    ("zone + pays", ["j360"],
     {"keywords": ["logiciel"], "countries": ["Afrique", "France"]}, "pays au serveur"),
    ("marche Inetum (TN+MR)", ["j360"],
     {"keywords": ["informatique", "logiciel", "developpement"], "keywords_any": True,
      "countries": ["Tunisie", "Mauritanie"]}, "pays au serveur"),
    ("Maroc", ["j360"],
     {"keywords": ["informatique"], "countries": ["Maroc"]}, "pays au serveur"),
    ("France", ["j360"],
     {"keywords": ["maintenance applicative"], "countries": ["France"]}, "pays au serveur"),
    ("accents dans le mot-cle", ["j360"],
     {"keywords": ["securite"]}, "trouve malgre l'accent manquant"),
    ("mot-cle introuvable", ["j360"],
     {"keywords": ["zzzintrouvable"]}, "zero, source epuisee"),
    ("aucun filtre", ["j360"], {}, "beaucoup de resultats"),
    ("TUNEPS 1 mot-cle", ["tuneps"],
     {"keywords": ["logiciel"], "keywords_any": True}, "pousse au formulaire"),
    ("TUNEPS 2 mots-cles OU", ["tuneps"],
     {"keywords": ["logiciel", "informatique"], "keywords_any": True}, "recherche par terme"),
]


def juger(nom, attente, run):
    """Verdict automatique : ce resultat est-il coherent ?"""
    extra = run.get("extra") or {}
    app_ = extra.get("filter_application") or {}
    serveur = app_.get("server_side") or []
    lues = extra.get("records_parsed") or 0
    ecartees = extra.get("items_filtered_out") or 0
    trouvees = run.get("items_found") or 0
    arret = extra.get("stop_reason")

    alertes = []
    if "introuvable" in nom and trouvees == 0 and arret == "source_exhausted":
        pass  # attendu
    elif lues and trouvees == 0:
        alertes.append("rien retenu alors que des lignes ont ete lues")
    if lues and ecartees / max(lues, 1) > 0.5:
        alertes.append(f"{100 * ecartees // max(lues, 1)}% jetes localement")
    if "pousse" in attente and "keywords" not in serveur:
        alertes.append("mot-cle NON pousse au portail")
    if "pays au serveur" in attente and "countries" not in serveur:
        alertes.append("pays NON pousse au portail")
    if "repli local" in attente and "countries" not in (app_.get("client_side") or []):
        alertes.append("pays inconnu non signale en local")
    if arret == "page_cap" and trouvees == 0:
        alertes.append("plafond atteint sans resultat")
    return alertes, (lues, ecartees, trouvees, arret, serveur)


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"{'scenario':26} {'lues':>5} {'ecart':>6} {'trouv':>6}  {'arret':16} serveur")
    print("-" * 104)

    total_alertes = 0
    for nom, sources, filtres, attente in SCENARIOS:
        if only and only not in nom:
            continue
        body = {"connectors": sources,
                "filters": {**filtres, "max_results_per_source": 10}}
        try:
            job = call("POST", "/scrape", body)
        except Exception as exc:
            print(f"  {nom:26} ECHEC AU LANCEMENT : {str(exc)[:40]}")
            total_alertes += 1
            continue

        job_id = job["job_id"]
        for _ in range(60):
            time.sleep(4)
            detail = call("GET", f"/scrape/jobs/{job_id}")
            if detail["status"] in ("succeeded", "failed", "partial", "timed_out"):
                break

        if not detail.get("runs"):
            print(f"  {nom:26} aucune execution")
            total_alertes += 1
            continue

        for run in detail["runs"]:
            alertes, (lues, ecart, trouv, arret, serveur) = juger(nom, attente, run)
            marque = "!!" if alertes else "ok"
            print(f"{marque} {nom:26} {lues:5} {ecart:6} {trouv:6}  {str(arret):16} {serveur}")
            for a in alertes:
                print(f"      -> {a}")
            total_alertes += len(alertes)

    print("-" * 104)
    print(f"  {total_alertes} anomalie(s)")


main()
