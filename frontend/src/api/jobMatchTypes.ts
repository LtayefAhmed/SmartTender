/** Types for the job-match endpoint. Kept separate from `types.ts` so that
 *  file stays untouched. */

export interface CandidateEvidence {
  passage: string;
  document?: string | null;
  score?: number | null;
}

export interface CandidateMatch {
  cv_id: string;
  label: string;
  headline: string | null;
  score: number;
  retrieval_score?: number | null;
  matched_technologies: string[];
  missing_technologies: string[];
  evidence: CandidateEvidence[];
  vetoed: boolean;
  veto_reason: string | null;
}

export interface StructuredCvProfile {
  age: number | null;
  experience_years: number | null;
  education: string | null;
  certifications: string[];
  languages: string[];
  skills: string[];
}

/** A CandidateMatch plus whether it passed the recruiter's own filters.
 *  `filtered_out`/`filtered_reason`/`structured_profile` are all null for a
 *  vetoed candidate shown for transparency — filters were never evaluated for
 *  it, which must read differently from "evaluated and passed". */
export interface JobMatchCandidate extends CandidateMatch {
  filtered_out: boolean | null;
  filtered_reason: string | null;
  structured_profile: StructuredCvProfile | null;
}

export interface JobMatchFiltersApplied {
  age_min: number | null;
  age_max: number | null;
  min_experience_years: number | null;
  certifications: string[];
  education: string[];
  languages: string[];
  technologies: string[];
}

export interface JobMatchResult {
  status: "ok" | "no_text";
  message?: string;
  requirements: { position: number; document: string | null; text: string }[];
  required_technologies: string[];
  kept_total: number;
  vetoed_total: number;
  filtered_total: number;
  filters_applied: JobMatchFiltersApplied;
  structured_requirements: {
    technologies: string[];
    certifications: string[];
    experience_min_annees: number | null;
    langues: string[];
    profils: string[];
    exigences: string[];
  } | null;
  weights: Record<string, number | string>;
  candidates: JobMatchCandidate[];
}
