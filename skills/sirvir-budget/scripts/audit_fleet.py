#!/usr/bin/env python3
"""Fleet audit: discover all Hermes profiles, their models, and token usage.

Run this first when setting up sirvir-budget. It will:
1. Find all profiles under ~/.hermes/profiles/
2. Read each profile's config.yaml to see what models they're using
3. Read each profile's state.db to aggregate token usage by model
4. Print a fleet-wide summary with cache rates and cost estimates
5. Identify which profiles are premium, default, or cheap tier
"""

import sqlite3
import os
import glob
import sys
from pathlib import Path

PROFILE_ROOT = os.path.expanduser("~/.hermes/profiles")

# ── Tier classification ────────────────────────────────────────
# Adjust these model lists to match your fleet's tier assignments.
# Models not listed here will be classified as "unknown" tier.
PREMIUM_MODELS = {"glm-5.2", "glm-5", "qwen-3.7-max", "qwen3.7-max", "claude", "gpt-5.5"}
DEFAULT_MODELS = {"deepseek-v4-pro", "deepseek-v4", "gpt-5.4", "gpt-5"}
CHEAP_MODELS = {"deepseek-v4-flash", "deepseek-v4-flash", "minimax-m3", "minimaxai/minimax-m3"}

def classify_tier(model_name: str) -> str:
    """Classify a model into premium/default/cheap tier."""
    m = model_name.lower().strip()
    for p in PREMIUM_MODELS:
        if p in m:
            return "premium"
    for d in DEFAULT_MODELS:
        if d in m:
            return "default"
    for c in CHEAP_MODELS:
        if c in m:
            return "cheap"
    return "unknown"

def read_config(profile_dir: str) -> dict:
    """Read a profile's config.yaml and extract model info."""
    config_path = os.path.join(profile_dir, "config.yaml")
    if not os.path.exists(config_path):
        return {}
    
    info = {}
    try:
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("provider:") and "model" not in info:
                    info["provider"] = line.split(":", 1)[1].strip()
                if line.startswith("default:") or (line.startswith("model:") and ":" not in line.split(":", 1)[1]):
                    val = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if val and val != "''" and val != '""':
                        info["main_model"] = val
                if "compression:" in line and "provider" not in line and "model" not in line:
                    # Next lines will have compression provider/model
                    pass
    except Exception:
        pass
    return info

def read_state_db(profile_dir: str) -> list:
    """Read a profile's state.db and return session aggregates."""
    db_path = os.path.join(profile_dir, "state.db")
    if not os.path.exists(db_path):
        return []
    
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        
        # Check if sessions table exists
        tables = [t[0] for t in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "sessions" not in tables:
            db.close()
            return []
        
        rows = db.execute("""
            SELECT COALESCE(model,'') AS model,
                   COUNT(*) AS sessions,
                   SUM(input_tokens) AS input_tok,
                   SUM(output_tokens) AS output_tok,
                   SUM(cache_read_tokens) AS cache_tok,
                   ROUND(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 2) AS cost_usd
            FROM sessions
            GROUP BY COALESCE(model,'')
            ORDER BY (input_tok + output_tok + cache_tok) DESC
        """).fetchall()
        db.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return []

def main():
    if not os.path.isdir(PROFILE_ROOT):
        print(f"ERROR: Profile root not found: {PROFILE_ROOT}")
        print("Set PROFILE_ROOT in the script or ensure ~/.hermes/profiles/ exists.")
        sys.exit(1)
    
    profiles = sorted([
        d for d in os.listdir(PROFILE_ROOT)
        if os.path.isdir(os.path.join(PROFILE_ROOT, d)) and not d.startswith(".")
    ])
    
    if not profiles:
        print(f"No profiles found under {PROFILE_ROOT}")
        sys.exit(0)
    
    print("=" * 80)
    print("FLEET AUDIT")
    print("=" * 80)
    print(f"Profile root: {PROFILE_ROOT}")
    print(f"Profiles found: {len(profiles)}")
    print()
    
    fleet_total_in = 0
    fleet_total_out = 0
    fleet_total_cache = 0
    fleet_total_sessions = 0
    fleet_total_cost = 0.0
    
    for prof in profiles:
        prof_dir = os.path.join(PROFILE_ROOT, prof)
        config = read_config(prof_dir)
        sessions = read_state_db(prof_dir)
        
        provider = config.get("provider", "?")
        main_model = config.get("main_model", "?")
        
        prof_in = sum(r["input_tok"] or 0 for r in sessions)
        prof_out = sum(r["output_tok"] or 0 for r in sessions)
        prof_cache = sum(r["cache_tok"] or 0 for r in sessions)
        prof_sessions = sum(r["sessions"] or 0 for r in sessions)
        prof_cost = sum(r["cost_usd"] or 0 for r in sessions)
        
        fleet_total_in += prof_in
        fleet_total_out += prof_out
        fleet_total_cache += prof_cache
        fleet_total_sessions += prof_sessions
        fleet_total_cost += prof_cost
        
        # Determine tier from the dominant model
        top_models = sorted(sessions, key=lambda r: (r["input_tok"] or 0) + (r["output_tok"] or 0) + (r["cache_tok"] or 0), reverse=True)
        dominant_model = top_models[0]["model"] if top_models else ""
        tier = classify_tier(dominant_model) if dominant_model else classify_tier(main_model)
        
        cache_rate = 100.0 * prof_cache / (prof_in + prof_cache) if (prof_in + prof_cache) > 0 else 0
        
        print(f"── {prof} ──")
        print(f"  Config:  provider={provider}  main={main_model}")
        print(f"  Tier:    {tier} (dominant model: {dominant_model})")
        print(f"  Usage:   {prof_sessions} sessions, {prof_in + prof_out + prof_cache:,} tokens")
        print(f"  Cache:   {cache_rate:.1f}% ({prof_cache:,} cache / {prof_in + prof_cache:,} input+cache)")
        print(f"  Cost:    ${prof_cost:.2f}")
        
        if sessions:
            print(f"  Models:")
            for r in sessions[:5]:  # top 5
                m_tier = classify_tier(r["model"])
                m_cache = 100.0 * (r["cache_tok"] or 0) / ((r["input_tok"] or 0) + (r["cache_tok"] or 0)) if ((r["input_tok"] or 0) + (r["cache_tok"] or 0)) > 0 else 0
                print(f"    {r['model']:30s} [{m_tier:8s}]  {r['sessions']:3d} sessions  in={r['input_tok'] or 0:>10,}  out={r['output_tok'] or 0:>10,}  cache={r['cache_tok'] or 0:>10,}  rate={m_cache:>5.1f}%  ${r['cost_usd'] or 0:>8.2f}")
        print()
    
    fleet_cache_rate = 100.0 * fleet_total_cache / (fleet_total_in + fleet_total_cache) if (fleet_total_in + fleet_total_cache) > 0 else 0
    fleet_total_tokens = fleet_total_in + fleet_total_out + fleet_total_cache
    
    print("=" * 80)
    print("FLEET SUMMARY")
    print("=" * 80)
    print(f"  Profiles:     {len(profiles)}")
    print(f"  Sessions:     {fleet_total_sessions}")
    print(f"  Total tokens: {fleet_total_tokens:,}")
    print(f"  Input:        {fleet_total_in:,}")
    print(f"  Output:       {fleet_total_out:,}")
    print(f"  Cache read:   {fleet_total_cache:,}")
    print(f"  Cache rate:   {fleet_cache_rate:.1f}%")
    print(f"  Total cost:   ${fleet_total_cost:.2f}")
    print()
    
    # Tier breakdown
    tier_counts = {"premium": 0, "default": 0, "cheap": 0, "unknown": 0}
    for prof in profiles:
        prof_dir = os.path.join(PROFILE_ROOT, prof)
        config = read_config(prof_dir)
        sessions = read_state_db(prof_dir)
        top_models = sorted(sessions, key=lambda r: (r["input_tok"] or 0) + (r["output_tok"] or 0) + (r["cache_tok"] or 0), reverse=True)
        dominant = top_models[0]["model"] if top_models else ""
        tier = classify_tier(dominant) if dominant else classify_tier(config.get("main_model", ""))
        tier_counts[tier] += 1
    
    print("Tier distribution:")
    for tier, count in tier_counts.items():
        print(f"  {tier:10s}: {count} profiles")
    
    print()
    print("Next steps:")
    print("  1. Edit references/budget-config.yaml to set your monthly budget")
    print("  2. Adjust PREMIUM_MODELS / DEFAULT_MODELS / CHEAP_MODELS in this script")
    print("     to match your fleet's actual model assignments")
    print("  3. Run scripts/verify_budget_docs.py to confirm everything is wired")
    print("  4. Set up the daily budget cron (see sirvir-research skill)")

if __name__ == "__main__":
    main()
