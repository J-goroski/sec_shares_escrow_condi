# INTERVIEW NOTES — BULLET FORMAT
## Research Integration Specialist, FTSE Russell

**Format:** Core bullets only. Read them, then speak naturally. Don't memorize scripts.

---

## SECTION 1: WHO AM I (Opening — ~90 seconds)

### Background Foundation
- Junior Data Scientist on FTSE Russell primary equity index data team (since Nov 2024)
- B.S. Data Science (CS track) + Economics minor, ASU 2024
- 2.5 years MSSQL database developer (contractor) before FTSE Russell

### Current Role (Index Data Team)
- Maintain accuracy/completeness of reference data across ~80k name global universe
- Built automated workflows (Python + MSSQL)
- Multi-vendor data validation & reconciliation
- Technical liaison between engineering, operations, research

### Why I'm Here
- Want to move from pure engineering → bridge between research and engineering
- Deliberately learning research methodology (FTSE tilt, attribution, backtesting)
- See this role as path toward quantitative researcher roles
- Internal knowledge = faster ramp on research side

---

## SECTION 2: WHAT I CURRENTLY DO AT FTSE RUSSELL (~60 seconds)

### Process Automation (Day-to-day)
- **Russell Monitored List** — rebuilt manual process into clean automated workflow
  - Eliminated manual steps, reduced error surface, runs reliably monthly
  - Python + MSSQL + validation gates
- **SEC Filing Extraction** — automated extraction pipeline
  - Fine-tuned LLM (Qwen2.5-7B), confidence scoring, vendor reconciliation
  - Zero data discrepancies across ~5,600 US names

### Infrastructure & Data Quality
- **Reference Data Automation** — ICB classification maintenance across 80k names
- **Multi-vendor Validation** — cross-validate prices, shares, identifiers
  - Mindset: "Reconcile, don't trust. Fail loudly."
- **PowerApps Workflows** — process automation on day job

### Collaboration
- Work across index methodology, engineering, operations teams daily
- Act as translator between technical and non-technical groups
- Shape solutions, not just execute (filing extraction example: prioritized, built visibility into trade-offs)

---

## SECTION 3: PORTFOLIO PROJECTS (Evidence you can build what the role needs)

### `fin_data` — Research Infrastructure Pipeline
- Medallion architecture (bronze/silver/gold)
- Multi-source ingestion + reconciliation (EDGAR, pricing, OpenFIGI)
- 39 unit tests, deterministic, lineage-tracked
- *Signal:* "I can build data infrastructure researchers depend on"

### `index_engine` — Reconstitution Engine + Backtesting
- Rules-based: eligibility, float-adjusted weighting, banding/buffer logic
- 27 unit tests, reconciled to original data
- Sensitivity analysis: 10% buffer reduces turnover ~25%
- *Signal:* "I understand methodology trade-offs (turnover vs representativeness)"
- *Connection:* Same logic behind 2026 semi-annual recon decision

### `momentum-factor-study` — Factor Analysis
- Raw signal (12-1 momentum) → Z-score → quantile sort
- IC (information coefficient), returns spread, quantile analysis
- Clean code, tested, documented
- *Signal:* "I can build factor analysis tools"

### `price-ml` — Returns Prediction (with caveats)
- Fine-tuned language model for feature engineering
- Chronological (not shuffled) train/test split (prevents look-ahead)
- Honest about overfitting: AUC ~0.55 (not the pretty equity curve)
- *Signal:* "I understand backtesting pitfalls and have healthy skepticism"

---

## SECTION 4: VALIDATION & QUALITY MINDSET

### Testing Discipline
- Avg 85–96% test coverage across projects
- Unit tests on pure functions (math layer separate from I/O)
- Golden-master regression testing (reconcile to reference)
- Validated index values to ~1e-16 agreement

### Validation Philosophy
- Reconcile against independent source (vendors, researcher prototype)
- Fail loudly (validation gates that break if data is bad)
- Point-in-time correctness (avoid look-ahead bias)
- Multi-vendor cross-checks (three-way disagreement = red flag)

### QA as First-Class Deliverable
- Documentation alongside code (CLAUDE.md pause-and-review protocol)
- Every project has README explaining methodology + choices
- Deterministic, reproducible runs (fixed seeds, pinned environments, network-free tests)

---

## SECTION 5: RESEARCH METHODOLOGY LEARNING (Closing your gap)

### What I've Studied
- FTSE Tilt-Tilt factor construction (Z-score → 0–1 S-score → tilt → neutralize)
- Russell 2026 semi-annual recon change (why: speed, concentration, large IPOs)
- Brinson attribution (allocation + selection + interaction)
- Backtesting rigor (look-ahead, survivorship, point-in-time)

### How I'm Learning
- Reading FTSE public methodology docs
- Working through Brinson example by hand
- Building small attribution module (portfolio piece + learning)
- Importing flashcards into Anki (daily review)

### Why This Matters
- Shows I'm not waiting to be taught; taking ownership
- Demonstrates genuine curiosity (not fake)
- Signals I can ramp quickly on research side (foundation already solid)

---

## SECTION 6: CROSS-TEAM COLLABORATION STORY

### Filing Extraction Project
- Researchers wanted 50+ fields extracted from 10-Ks
- Engineering concern: accuracy drops with complexity
- *What I did:*
  - Asked researchers which fields *actually* mattered
  - Prioritized 15 fields, planned phased rollout for others
  - Built confidence-scoring dashboard (visibility into trade-offs)
  - Got buy-in on validation standards
- *Signal:* "I shape solutions; I listen; I balance competing needs"

---

## SECTION 7: YOUR GAPS (Own them)

### Performance Attribution
- 🟡 Newly studied (not from project yet)
- Building small module to learn + get portfolio evidence
- Know frameworks: Brinson (sector) and factor-based
- Can walk through example by hand

### Pure Research Methodology Depth
- 🟡 Learning FTSE factor approach, backtesting rigor, IC/IR
- Have domain fluency (index data, identifiers, regulatory)
- Engineering rigor → research rigor is adjacent (just need methodology vocabulary)

### How to Frame It
- "My biggest gap is pure research methodology depth—attribution, factor theory, backtesting rigor. I'm not hiding that. I've been deliberately studying it (reading FTSE docs, building attribution module, reviewing flashcards). What I'm confident about: I learn fast, I'm meticulous about quality, I can code anything you need."

---

## SECTION 8: THE BRIDGE-BUILDER PITCH (Your unique angle)

### What Makes You Different
- Internal (already know data, org, Russell methodology operationally)
- Engineer who wants to *understand* research intent (not just code it)
- Clear progression path in mind (toward quant research)
- Deliberate learning (not hoping to figure it out on the job)

### One-Liner
- "I can be the person who translates research intent into production tools without losing the methodology. I already know the index data; I'm learning the research side deliberately; I can bridge both worlds."

---

## SECTION 9: QUESTIONS TO ASK THEM

### Research Direction
- "What's the team's biggest priority right now? Are you focused on Target Exposure vs Fixed Tilt factor indexes?"
- "Where does the research team feel the most friction going from prototype to production?"

### Role Specifics
- "What does success look like in the first 6 months? Building tools? Documenting methodology? Hardening research prototypes?"
- "How much is reactive (researchers come to me with 'implement this') vs proactive (I can push back on methodology)?"

### Career Path
- "You mentioned a progression path toward quant research. What does that actually look like? 2-year apprenticeship then apply for researcher role, or more gradual?"

### Team & Culture
- "How does the research team currently handle methodology validation? What's your biggest pain point there?"
- "Who would I be working with most closely on day-to-day projects?"

---

## SECTION 10: KEY PHRASES TO USE (Signals you understand the job)

### When Talking About Your Work
- "I turned X from manual to automated"
- "I built infrastructure that researchers depend on"
- "I reconcile against an independent source to catch bad data early"
- "I validated to 1e-16 agreement with the reference"
- "I caught that issue because of the three-way reconciliation check"
- "I'm meticulous about look-ahead bias in backtests"

### When Talking About Gaps
- "I'm learning the research vocabulary (FTSE tilt approach, attribution frameworks)"
- "I understand the engineering rigor; I'm learning the methodology rigor"
- "I'm deliberately closing that gap because I see this role as a bridge"

### When Talking About Collaboration
- "I don't just code what I'm told; I shape the approach with stakeholders"
- "I understand the trade-offs and make them visible"
- "I speak both languages (research and engineering)"

### About the Role Fit
- "This is exactly the bridge-building work I want to do"
- "I'm internal—I already know the data and org, so I can ramp fast on the research side"
- "I've done pieces of this already; this role lets me do it full-time"

---

## BEFORE THE CALL — QUICK CHECKLIST

- [ ] Read this bullet sheet once (internalize, not memorize)
- [ ] Do the Brinson example one more time (in your head, quick)
- [ ] Think about the filing extraction story (how you shaped it)
- [ ] Check your energy/tone (confident, curious, grounded—not anxious)
- [ ] Have the one-liner ready (about yourself, why this role)
- [ ] Smile when you talk (yes, they notice on Teams)

---

## TONE REMINDERS

✅ **Do:**
- Speak naturally (read bullet, then talk)
- Be specific (project names, numbers, outcomes)
- Own your gaps (honest > defensive)
- Show curiosity (ask good questions)
- Be conversational (this is a chat, not a presentation)

❌ **Don't:**
- Memorize scripts (sounds robotic)
- Over-explain (trust them to ask follow-ups)
- Apologize for gaps (own them, show you're closing them)
- Use all the jargon at once (just sounds like you're showing off)
- Be falsely humble (you have solid evidence; claim it)

---

## QUICK REFERENCE: Your Evidence Chain

**"I already do this work"**
- Automation → Monitored List rebuild
- Infrastructure → fin_data pipeline
- Factor tools → momentum study + index_engine
- Validation → multi-vendor reconciliation

**"I'm learning the research side"**
- Methodology docs → FTSE tilt approach
- Attribution → Brinson example + module
- Backtesting → understand pitfalls via price-ml

**"I bridge teams"**
- Day job → work across index/engineering/operations
- Filing extraction → shaped approach with researchers

---

## One Last Thing

**If they ask "Tell us about yourself":**

Use Section 1 + Section 2 + Section 3 (whichever projects fit).
~2 minutes. Then stop and let them ask follow-ups.

**If they ask "How do you fit this role":**

Section 4 (validation) + Section 6 (collaboration) + Section 8 (bridge-builder).
Show you've done *pieces* of it already.

**If they ask "What's your biggest gap":**

Section 5 + Section 7.
Own it, show you're closing it.

**If they ask technical questions:**

Grab the relevant bullet from Section 3 or 4, speak naturally, use examples.

You've got this. 🎯
