import { Badge, Card, Meter } from "./ui";

interface MockCandidate {
  label: string;
  headline: string;
  score: number;
  matched: string[];
}

interface MockSection {
  key: string;
  label: string;
  source: "llm" | "template";
  content: string;
}

const SECTIONS: MockSection[] = [
  {
    key: "company_presentation",
    label: "Présentation de la société",
    source: "template",
    content:
      "Inetum Tunisie accompagne ses clients du secteur public et privé dans leurs projets de transformation digitale depuis plus de 20 ans, avec une expertise reconnue en intégration de systèmes, développement sur mesure et infogérance.",
  },
  {
    key: "understanding",
    label: "Compréhension du besoin",
    source: "llm",
    content:
      "Le présent avis exprime le besoin de moderniser la plateforme de gestion documentaire du Ministère, avec un accent sur l'intégration continue, la sécurité applicative et la gestion des identités. Une expérience confirmée en transformation digitale du secteur public est exigée, ainsi qu'une maîtrise des architectures orientées microservices.",
  },
  {
    key: "methodology",
    label: "Méthodologie proposée",
    source: "llm",
    content:
      "Notre approche s'articule en quatre phases : cadrage et recueil des besoins détaillés, conception de l'architecture cible, développement itératif avec livraisons incrémentales, puis recette et accompagnement au changement. Chaque phase fait l'objet d'un comité de pilotage dédié.",
  },
  {
    key: "team",
    label: "Équipe proposée",
    source: "llm",
    content:
      "L'équipe mobilisée réunit des profils dont l'expérience recouvre directement les exigences techniques du dossier, sélectionnés automatiquement par rapprochement sémantique avec la base de CVs.",
  },
  {
    key: "timeline",
    label: "Planning indicatif",
    source: "template",
    content:
      "Phase de cadrage : 3 semaines · Conception : 4 semaines · Développement : 12 semaines · Recette et déploiement : 3 semaines. Planning à ajuster selon la date de notification et les contraintes du pouvoir adjudicateur.",
  },
  {
    key: "next_steps",
    label: "Prochaines étapes / Conclusion",
    source: "template",
    content:
      "Notre équipe reste à la disposition du pouvoir adjudicateur pour toute clarification complémentaire et se tient prête à démarrer la mission dès notification du marché.",
  },
];

const MOCK_CANDIDATES: MockCandidate[] = [
  {
    label: "Amine Ben Salah",
    headline: "Architecte logiciel senior",
    score: 0.89,
    matched: ["Symfony", "Docker", "Kubernetes"],
  },
  {
    label: "Farah Trabelsi",
    headline: "Ingénieure DevOps",
    score: 0.82,
    matched: ["Docker", "GitLab", "Terraform"],
  },
  {
    label: "Sahar Gharbi",
    headline: "Experte sécurité applicative",
    score: 0.77,
    matched: ["OAuth", "SSO", "SonarQube"],
  },
];

/** Static mockup of the eventual editable draft — placeholder content, no
 *  backend wiring. Source badge previews how a generated vs. templated
 *  section will read once the real agent exists. */
export function ProposalSections() {
  return (
    <div className="stack">
      {SECTIONS.map((section) => (
        <Card
          key={section.key}
          title={section.label}
          hint={
            <Badge color={section.source === "llm" ? "violet" : "gray"}>
              {section.source === "llm" ? "généré par l'IA" : "modèle"}
            </Badge>
          }
        >
          <p className="tiny" style={{ lineHeight: 1.6, margin: 0 }}>
            {section.content}
          </p>

          {section.key === "team" && (
            <div className="stack" style={{ gap: 8, marginTop: 12 }}>
              {MOCK_CANDIDATES.map((c) => (
                <div
                  key={c.label}
                  className="card"
                  style={{ padding: 10, borderLeft: "3px solid var(--teal)" }}
                >
                  <div className="row spread">
                    <span style={{ fontWeight: 600, fontSize: 13 }}>{c.label}</span>
                    <span className="tiny mono">{(c.score * 100).toFixed(0)}%</span>
                  </div>
                  <div className="tiny muted">{c.headline}</div>
                  <div className="row" style={{ gap: 8, marginTop: 4, alignItems: "center" }}>
                    <Meter value={c.score} color="var(--teal)" />
                  </div>
                  <div className="tiny mt">
                    <span className="muted">Compétences attestées : </span>
                    {c.matched.join(", ")}
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="row" style={{ justifyContent: "flex-end", marginTop: 10 }}>
            <button className="btn sm ghost" disabled title="Aperçu — non connecté au backend">
              Enregistrer
            </button>
          </div>
        </Card>
      ))}
    </div>
  );
}
