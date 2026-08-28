import { Badge } from "./ui";
import { statusColor } from "../lib/format";

export interface ProposalFlowStep {
  key: string;
  label: string;
  description: string;
  icon: string;
  status: "pending" | "running" | "succeeded" | "failed";
}

/** The agent's pipeline, drawn as three connected stages.
 *
 *  Purely presentational — takes whatever `steps` it's given and renders
 *  their state. A future live wire-up swaps a demo array for polled data
 *  without touching this component at all. */
export function ProposalFlow({ steps }: { steps: ProposalFlowStep[] }) {
  return (
    <div className="proposal-flow">
      {steps.map((step, i) => (
        <div className="proposal-flow-item" key={step.key}>
          <div className={`proposal-flow-node ${step.status === "running" ? "pulse" : ""}`}>
            <div className="proposal-flow-icon" style={{ color: `var(--${statusColor(step.status)})` }}>
              {step.icon}
            </div>
            <div className="proposal-flow-label">{step.label}</div>
            <div className="tiny muted proposal-flow-desc">{step.description}</div>
            <Badge color={statusColor(step.status)}>{STATUS_LABEL[step.status]}</Badge>
          </div>
          {i < steps.length - 1 && (
            <div
              className="proposal-flow-connector"
              style={{
                borderColor:
                  step.status === "succeeded" ? "var(--teal)" : "var(--line)",
              }}
            />
          )}
        </div>
      ))}
    </div>
  );
}

const STATUS_LABEL: Record<ProposalFlowStep["status"], string> = {
  pending: "en attente",
  running: "en cours",
  succeeded: "terminé",
  failed: "échec",
};
