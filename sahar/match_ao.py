"""
Match an AO (appel d'offres / job tender) JSON against the résumé pool
stored in Qdrant, and produce a fit analysis + grade -- per required profile,
not just one blended score. An AO is a PROJECT: it usually needs several
different profiles (e.g. 2 backend devs + 1 DevOps + 1 PM), each of which
should be matched and graded independently, then rolled up.

Usage:
    python match_ao.py ao.json
    python match_ao.py ao.json --top 8 --topk 10

The AO JSON schema is NOT assumed:
  - We look for a list of profile objects anywhere in the JSON (any key whose
    value is a list of dicts, e.g. "profiles", "roles", "positions" -- the
    key name doesn't matter). The largest such list found is treated as the
    project's required profiles.
  - If no such list exists, the whole AO is treated as a single profile
    (backwards compatible with a plain single-role AO).
  - Within each profile, we look for common title/count field names
    ("role", "title", "count", "headcount", ...) and fall back to flattening
    everything if nothing matches. This is a generic placeholder -- once you
    show me your real AO schema we can target its exact fields.
"""

import argparse
import json
import os
import statistics
import sys
import webbrowser

sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer, CrossEncoder

from dashboard import render_dashboard
from qdrant_utils import (
    QDRANT_PATH,
    COLLECTION_NAME,
    MODEL_NAME,
    RERANK_MODEL_NAME,
    RERANK_SHORTLIST_SIZE,
    search_resumes,
    rerank,
)

# Grading thresholds are z-scores: how many standard deviations the top-K
# average sits above this profile's own similarity distribution across the
# whole résumé pool. Self-calibrated per profile, since raw cosine values
# from MiniLM cluster in a narrow band and aren't comparable across queries.
GRADE_BANDS = [
    (2.5, "A", "Excellent fit"),
    (1.5, "B", "Strong fit"),
    (0.75, "C", "Moderate fit"),
    (0.0, "D", "Weak fit"),
    (float("-inf"), "F", "No fit / mismatch"),
]
GRADE_RANK = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}
GRADE_WORD = {letter: word for _, letter, word in GRADE_BANDS}

# A z-score alone can be fooled: if the ENTIRE pool is unrelated to a role
# (e.g. asking a résumé pool with no vets for a "Veterinary Surgeon"), the
# corpus mean/std shrink together, and even a mediocre top match becomes a
# large z-score simply because it's "less bad" than everything else -- not
# because it's a genuine fit. corpus_mean measures something different and
# harder to fake: how similar is a RANDOM résumé in the pool to this role's
# language, on average. A low value means the pool doesn't discuss this
# domain at all, independent of any single résumé's rank. When that happens
# we cap the grade regardless of z-score. 0.25 is a heuristic threshold
# calibrated from a handful of test AOs (well-covered roles landed
# corpus_mean 0.32-0.36; an uncovered role landed 0.215) -- revisit if it
# starts mis-flagging real roles.
COVERAGE_MEAN_FLOOR = 0.25
COVERAGE_CAP = "D"

# The z-score/coverage checks above are still bi-encoder (cosine similarity)
# based -- fast, whole-pool, but easily fooled by generic résumé-vocabulary
# overlap (see qdrant_utils.rerank). The cross-encoder's relevance score for
# the actual candidates we'd propose is a second, more trustworthy opinion,
# so it gets its own cap: average cross-encoder relevance of the top `needed`
# reranked candidates (not an arbitrary top-10 -- if a role only needs 1
# person, one great candidate should be enough; needing 2 means we check 2).
# Thresholds are the cross-encoder's own sigmoid probability, so 0.7/0.4/0.2
# read literally as "70%+/40%+/20%+ confident this is a relevant match."
RELEVANCE_CAP_BANDS = [
    (0.7, None),
    (0.4, "B"),
    (0.2, "C"),
    (float("-inf"), "D"),
]


def relevance_cap_for(relevance_mean):
    for threshold, cap in RELEVANCE_CAP_BANDS:
        if relevance_mean >= threshold:
            return cap
    return "D"

PROFILE_LIST_KEYS_HINT = ("profiles", "roles", "positions", "requirements")
TITLE_KEYS = ("role", "title", "job_title", "position", "name", "profile")
COUNT_KEYS = ("count", "quantity", "headcount", "num_positions", "number", "nb", "positions_needed")
PROJECT_TITLE_KEYS = ("project", "title", "name", "client")


def flatten_json_to_text(obj, prefix=""):
    """Recursively turn arbitrary JSON into a list of 'field: value' text lines."""
    lines = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            lines.extend(flatten_json_to_text(value, prefix=key))
    elif isinstance(obj, list):
        for item in obj:
            lines.extend(flatten_json_to_text(item, prefix=prefix))
    else:
        if obj is not None and str(obj).strip():
            lines.append(f"{prefix}: {obj}" if prefix else str(obj))
    return lines


def grade_for(z_score):
    for threshold, letter, word in GRADE_BANDS:
        if z_score >= threshold:
            return letter, word
    letter, word = GRADE_BANDS[-1][1], GRADE_BANDS[-1][2]
    return letter, word


def find_profiles(ao):
    """Find the project's list of required profiles anywhere in the AO JSON.
    Falls back to treating the whole AO as a single profile if none found."""

    def walk(obj):
        found = []
        if isinstance(obj, dict):
            for v in obj.values():
                found.extend(walk(v))
        elif isinstance(obj, list):
            if obj and all(isinstance(item, dict) for item in obj):
                found.append(obj)
            for item in obj:
                found.extend(walk(item))
        return found

    candidates = walk(ao)
    if candidates:
        return max(candidates, key=len)
    return [ao] if isinstance(ao, dict) else []


def profile_title(profile, idx):
    for key in TITLE_KEYS:
        if isinstance(profile, dict) and profile.get(key):
            return str(profile[key])
    return f"Profile {idx + 1}"


def profile_count(profile):
    for key in COUNT_KEYS:
        if isinstance(profile, dict) and isinstance(profile.get(key), (int, float)):
            return int(profile[key])
    return 1


def score_profile_against_pool(client, model, text, topk_n):
    """Embed `text` and score it against every résumé in the pool (one
    best-scoring chunk per résumé -- see qdrant_utils.search_resumes)."""
    results = search_resumes(client, model, text)

    all_scores = [hit.score for hit in results]
    corpus_mean = statistics.mean(all_scores)
    corpus_std = statistics.pstdev(all_scores) or 1e-9

    topk_scores = all_scores[:topk_n]
    topk_mean = statistics.mean(topk_scores)
    z_score = (topk_mean - corpus_mean) / corpus_std
    grade_letter, grade_word = grade_for(z_score)

    coverage_warning = corpus_mean < COVERAGE_MEAN_FLOOR
    if coverage_warning and GRADE_RANK[grade_letter] > GRADE_RANK[COVERAGE_CAP]:
        grade_letter, grade_word = COVERAGE_CAP, GRADE_WORD[COVERAGE_CAP]

    category_scores = {}
    for hit in results:
        cat = hit.payload["category"]
        category_scores.setdefault(cat, []).append(hit.score)
    category_avg = sorted(
        ((cat, statistics.mean(scores), len(scores)) for cat, scores in category_scores.items()),
        key=lambda x: x[1],
        reverse=True,
    )

    return {
        "results": results,
        "pool_size": len(results),
        "all_scores": all_scores,
        "corpus_mean": corpus_mean,
        "corpus_std": corpus_std,
        "topk_mean": topk_mean,
        "z_score": z_score,
        "grade_letter": grade_letter,
        "grade_word": grade_word,
        "coverage_warning": coverage_warning,
        "category_avg": category_avg,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ao_file", help="path to the AO JSON file")
    parser.add_argument("--top", type=int, default=8, help="how many individual matches to list per profile")
    parser.add_argument("--topk", type=int, default=10, help="how many top matches to average for grading")
    parser.add_argument("--dashboard", default="dashboard.html", help="output path for the HTML dashboard")
    parser.add_argument("--no-open", action="store_true", help="don't auto-open the dashboard in a browser")
    args = parser.parse_args()

    with open(args.ao_file, "r", encoding="utf-8") as f:
        ao = json.load(f)

    profiles_raw = find_profiles(ao)
    if not profiles_raw:
        sys.exit("No usable content found in the AO JSON.")

    project_title = next(
        (ao[k] for k in PROJECT_TITLE_KEYS if isinstance(ao, dict) and ao.get(k)),
        "AO Analysis",
    )

    print("=" * 70)
    print(f"PROJECT: {project_title}")
    print(f"Required profiles: {len(profiles_raw)}")
    print("=" * 70)

    model = SentenceTransformer(MODEL_NAME)
    cross_encoder = CrossEncoder(RERANK_MODEL_NAME)
    client = QdrantClient(path=QDRANT_PATH)

    profiles_out = []
    for idx, profile in enumerate(profiles_raw):
        title = profile_title(profile, idx)
        needed = profile_count(profile)
        profile_lines = flatten_json_to_text(profile)
        profile_text = "\n".join(profile_lines) if profile_lines else title

        scored = score_profile_against_pool(client, model, profile_text, args.topk)
        results = scored["results"]
        total_count = scored["pool_size"]

        # The bi-encoder (`results`, cosine similarity) is fast enough to rank
        # the whole pool -- used above for corpus_mean/std/z-score/grade. But
        # it's easily fooled by generic résumé vocabulary overlap, which is
        # exactly why an unrelated résumé could rank #1. A cross-encoder reads
        # query and résumé together and is far more accurate at judging real
        # relevance, at the cost of being too slow for the whole pool -- so it
        # reranks just the top shortlist for the candidate list actually shown.
        shortlist_size = max(RERANK_SHORTLIST_SIZE, args.top, needed)
        shortlist = results[:shortlist_size]
        reranked_all = rerank(cross_encoder, profile_text, shortlist)  # full shortlist, sorted by relevance
        display_matches = reranked_all[: args.top]

        # Grade on relevance for exactly as many candidates as the role
        # needs -- a role needing 1 person only needs 1 great candidate; a
        # role needing 2 needs both to look good, not diluted by a long tail.
        grading_candidates = reranked_all[:needed]
        relevance_mean = (
            statistics.mean(h.score for h in grading_candidates) if grading_candidates else 0.0
        )
        relevance_cap = relevance_cap_for(relevance_mean)

        grade_letter, grade_word = scored["grade_letter"], scored["grade_word"]
        relevance_capped = relevance_cap is not None and GRADE_RANK[grade_letter] > GRADE_RANK[relevance_cap]
        if relevance_capped:
            grade_letter, grade_word = relevance_cap, GRADE_WORD[relevance_cap]

        print(f"\n--- Profile {idx + 1}/{len(profiles_raw)}: {title} (need {needed}) ---")
        print(f"  Grade: {grade_letter} - {grade_word}  (z={scored['z_score']:+.2f} vs. pool, "
              f"top-{needed} relevance avg={relevance_mean:.3f})")
        print(f"  Best-aligned category: {scored['category_avg'][0][0]} "
              f"(avg={scored['category_avg'][0][1]:.3f})")
        if scored["coverage_warning"]:
            print(f"  WARNING: pool coverage for this role is low (corpus mean "
                  f"{scored['corpus_mean']:.3f} < {COVERAGE_MEAN_FLOOR}) -- even the "
                  f"top match may not be a genuine fit; grade capped at {COVERAGE_CAP}.")
        if relevance_capped:
            print(f"  WARNING: cross-encoder relevance for the top {needed} candidate(s) is only "
                  f"{relevance_mean:.3f} -- grade capped at {relevance_cap}.")
        print(f"  Top {len(display_matches)} candidates (reranked for relevance):")
        for hit in display_matches:
            # Show the chunk that actually matched, not the résumé's generic
            # opening -- a long résumé's best-matching section can be a
            # completely different part of their history than the header.
            snippet = hit.payload["chunk_text"][:150].replace("\n", " ").strip()
            print(f"    #{hit.id}  relevance={hit.score:.3f}  (retrieval={hit.retrieval_score:.3f})  "
                  f"[{hit.payload['category']}]  {snippet}...")

        top_hit = reranked_all[0] if reranked_all else results[0]
        profiles_out.append({
            "title": title,
            "needed": needed,
            "profile_text": profile_text,
            "pool_size": total_count,
            "grade_letter": grade_letter,
            "grade_word": grade_word,
            "coverage_warning": scored["coverage_warning"],
            "relevance_warning": relevance_capped,
            "relevance_mean": relevance_mean,
            "z_score": scored["z_score"],
            "top_score": top_hit.score,
            "top_id": top_hit.id,
            "topk": args.topk,
            "topk_mean": scored["topk_mean"],
            "corpus_mean": scored["corpus_mean"],
            "corpus_std": scored["corpus_std"],
            "category_avg": scored["category_avg"],
            "top_matches": [
                {
                    "id": hit.id,
                    "score": hit.score,
                    "category": hit.payload["category"],
                    "snippet": hit.payload["chunk_text"][:180].replace("\n", " ").strip() + "...",
                }
                for hit in display_matches
            ],
        })

    # Project-level grade = weakest-link across all required profiles: a
    # project is only as staffable as its hardest-to-fill role.
    bottleneck = min(profiles_out, key=lambda p: GRADE_RANK[p["grade_letter"]])
    overall_letter, overall_word = bottleneck["grade_letter"], bottleneck["grade_word"]

    print("\n" + "=" * 70)
    print("OVERALL PROJECT FIT")
    print("=" * 70)
    print(f"  Overall grade:    {overall_letter} - {overall_word}")
    print(f"  Bottleneck role:  {bottleneck['title']}")
    print("  Note: candidates are ranked per profile independently -- the same")
    print("  résumé can appear as a top match for more than one role. This does")
    print("  not yet check whether enough DISTINCT people exist to staff every")
    print("  role at once.")

    dashboard_data = {
        "project_title": project_title,
        "pool_size": total_count,
        "overall_letter": overall_letter,
        "overall_word": overall_word,
        "bottleneck_title": bottleneck["title"],
        "profiles": profiles_out,
    }
    render_dashboard(dashboard_data, args.dashboard)
    dashboard_abspath = os.path.abspath(args.dashboard)
    print(f"\nDashboard written to: {dashboard_abspath}")

    if not args.no_open:
        webbrowser.open(f"file://{dashboard_abspath}")


if __name__ == "__main__":
    main()
