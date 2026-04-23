# BettingEdge — Baseball Betting Dashboard
## Product Overview & Architecture Document

---

## What Is This?

BettingEdge is a personal baseball betting research tool built by a single user for their own use. It combines a custom Monte Carlo simulation engine, real player statistics from FanGraphs, live odds from multiple sportsbooks, weather and umpire data, and a Kelly Criterion bet-sizing model into one dashboard.

The goal is not to automate betting — it is to surface edges the market has mispriced, tell the user exactly what to bet, how much to bet, and why. The user reads the recommendation and places the bet manually at their book.

It runs locally on a Mac as a Flask web app. No cloud deployment (yet). No mobile app. No external users.

---

## The Core Problem It Solves

Sharp bettors lose their edge not from bad analysis but from:
1. **Overconfident models** — simulators that produce 62% win probability when the real number is 55%
2. **Bad bet sizing** — betting too much when uncertain, not enough when confident
3. **No tracking** — no feedback loop to know if the model is actually working
4. **Manual data assembly** — spending an hour per game collecting odds, weather, lineups, umpires

BettingEdge tackles all four by connecting data automatically, calibrating its own probabilities against live results, sizing bets mathematically, and tracking every recommendation to grade model performance.

---

## How It Works — End-to-End Flow

### 1. Data Assembly (Automated)

Every day, starting at 8 PM for the next day's games:

- **Schedule** → pulled from MLB Stats API (free, no key). Imports game time, teams, starters, ballpark, game type.
- **Odds** → fetched from The Odds API every 2 hours. Stores lines from DraftKings, BetMGM, FanDuel, Caesars in the database.
- **Weather** → fetched from OpenWeatherMap (current for today's games, 5-day forecast for tomorrow's). Temperature, wind speed, wind direction, humidity, barometric pressure all stored per game.
- **Umpires** → home-plate umpire assignments fetched from MLB API at 10 AM, matched to a pre-seeded database of 80+ umpires with their 2024 tendencies (walk rate impact, strikeout rate impact, runs per game).
- **Lineups** → official batting orders posted 60–90 minutes before first pitch via MLB API. The quick-sim button on the games page auto-imports them.
- **Player stats** → manually exported from FanGraphs (premium subscriber). Includes full-season batting and pitching splits (vs LHP/RHP), pitch mix, FIP, xFIP, wRC+, BABIP, recent 15-day form for every player.

### 2. Simulation

Clicking the lightning bolt on any game runs **10,000 Monte Carlo simulations** of the full 9-inning game.

**Each simulation:**
- Steps through every plate appearance, half-inning by half-inning
- Draws outcomes (single, double, triple, HR, walk, strikeout, out) from a probability distribution built by blending:
  - Batter outcome rates (with LHP/RHP split)
  - Pitcher outcome rates allowed (with LHB/RHB split)
  - League average (as the "prior" in a log-5 formula)
- Adjusts for park factor (HR-friendly, hitter-friendly, neutral)
- Adjusts for weather (temperature raises HR rate ~3% per 10°F above 72°F; wind-out boosts HR and extra-base hits; wind-in suppresses them; humidity and pressure have minor effects)
- Adjusts for umpire tendencies (tight zone = fewer walks/Ks = more balls in play = more runs)
- Tracks pitch count; swaps starter for bullpen when pitch limit is reached
- Runs extra innings if tied after 9
- Snapshots the score after inning 5 for F5 markets

**Regression to the mean:** Small sample sizes are handled by regressing observed stats toward league average. For example, a pitcher with 30 IP is given only ~40% weight on his own stats; the rest comes from the league average. A full-season starter (~180 IP) gets ~85% weight. Different outcomes stabilize at different rates (strikeouts fast, triples slow).

**Output:** home win %, away win %, projected total, over/under %, F5 win %, F5 total, first-inning scoring %, run line cover %, and the full score distribution stored for charting.

### 3. Calibration

Raw simulation probabilities are overconfident. A model that says 62% wins 62% of the time in simulation but only ~56% of the time in real life — because simulation doesn't model the unknowns (bad warmup, unreported injury, starter pulled early, cold bat streaks).

BettingEdge uses **Platt scaling** — a logistic regression fit on 115 real bets — to compress the raw probabilities toward true frequencies. A raw 62% becomes ~55.6% calibrated. The threshold price displayed on the games page uses the calibrated number, not the raw one.

Without calibration, the app shows -114 as a "good bet" on a 62% raw game. With calibration (55.6%), -114 has only 2.3% edge — below the 6% threshold. That's the difference between a bet and a pass.

### 4. Edge Calculation & Bet Sizing

For each market (moneyline, total, F5, run line):

**Edge** = our calibrated probability − market's implied probability  
**EV%** = (our_prob × profit_per_unit) − (our_prob × 1)  
**Kelly bet** = `(edge / decimal_odds) × kelly_fraction × bankroll`

Kelly fraction is set to **0.25 (quarter Kelly)** by default — well-established as the conservative, variance-reducing approach. Hard capped at 5% of bankroll per bet regardless of Kelly output. Minimum bet is $5.

**Research-validated filters** (from 2024+2025 backtests):
- Favorites: minimum 60% raw confidence, maximum -225 (heavy juice kills ROI), 6% edge floor (8% for the 65–70% bucket)
- Underdogs: only the +131 to +200 range (+21–34% ROI in backtest; below +131 and above +200 are both losing)
- Totals: minimum 58% raw probability, 6% edge floor
- Run line: disabled (backtest showed -8.8% ROI over 77 bets — not a reliable model signal)

### 5. Presentation

**Games page:** Every game has a card showing:
- Matchup, time, starters
- **Threshold prices** — the minimum price the user needs to get at their book to have a positive-EV bet (e.g. "Must get -150 or better"). Displayed as the key metric, not raw odds.
- Live input fields for ML, total line, over/under price — the user types in the price they see at Don Best or their book, and the app instantly shows the Kelly bet size.
- Push adjustment — if the user enters a whole-number total line (Over 8 instead of 8.5), the probability automatically accounts for push probability (a push returns your stake, not a loss).
- Auto-generated recommendations with edge %, model probability, market probability, suggested bet size.

**Quick Entry page:** A spreadsheet-style view of all today's games. Type in lines from Don Best across all markets at once (away ML, home ML, total, over, under, F5 away, F5 home, F5 total), hit Analyze, and get back a table of every profitable bet with edges and Kelly sizing.

**Dashboard:** Top 6 bets for today, placed bet watchlist, last 5 settled results, win rate and ROI.

**Bets page:** Full history of every recommendation. Tracks CLV (closing line value — did the user get a better number than the closing line, the gold standard of bet quality).

### 6. Settlement & Feedback Loop

After games end, clicking "Settle Games" fetches final scores from the MLB API, grades every bet (win/lose/push), calculates P&L, logs it to the bankroll, and captures the closing odds for CLV calculation. The model's long-run accuracy is tracked in the dashboard stats bar.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Flask Web App                      │
│                   (app.py)                           │
│   ~50 routes: pages, APIs, webhooks                  │
└────────────────────┬────────────────────────────────┘
                     │
        ┌────────────┼────────────────┐
        ▼            ▼                ▼
┌──────────────┐ ┌──────────┐ ┌────────────────┐
│  Simulation  │ │  Kelly / │ │  Calibration   │
│  Engine      │ │  EV      │ │  (Platt        │
│  (Monte      │ │  Model   │ │  scaling)      │
│  Carlo,      │ │          │ │                │
│  10k sims)   │ └──────────┘ └────────────────┘
└──────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────┐
│                  SQLite Database                     │
│  Teams · Ballparks · Players · Stats · Games         │
│  Lineups · Simulations · Odds · Recommendations      │
│  Bankroll · Umpires · Weather · Historical Odds      │
└────────────────────┬────────────────────────────────┘
                     │
        ┌────────────┼───────────────────────────┐
        ▼            ▼            ▼               ▼
┌──────────────┐ ┌────────┐ ┌─────────┐ ┌──────────────┐
│  MLB Stats   │ │ Odds   │ │ Weather │ │  FanGraphs   │
│  API (free)  │ │ API    │ │ API     │ │  (CSV        │
│  Schedule,   │ │ (The   │ │ (Open   │ │  export,     │
│  lineups,    │ │ Odds   │ │ Weather │ │  manual      │
│  results     │ │ API)   │ │ Map)    │ │  import)     │
└──────────────┘ └────────┘ └─────────┘ └──────────────┘
```

**Tech stack:**
- Python 3.9, Flask, SQLite (SQLAlchemy ORM)
- Bootstrap 5 (dark theme), Plotly (histograms)
- APScheduler (background jobs)
- No JavaScript framework — vanilla JS with Bootstrap components
- Runs locally at `http://localhost:5001`

---

## What Makes It Different

Most publicly available MLB betting tools fall into three categories:

1. **Sharp consensus sites** (Bet Labs, Killer Sports) — show you historical trends ("home dogs off a loss") with no forward-looking simulation. No player-level modeling.

2. **General-purpose analytics** (Baseball Savant, FanGraphs) — deep stats, but no connection to lines or bet sizing. Tells you what happened, not what to do.

3. **Black-box picks services** — you pay $50/month for someone else's recommendations with no transparency into the model.

BettingEdge is different in four ways:

### 1. Full transparency
Every recommendation shows why: model probability, market probability, edge, EV, Kelly fraction, the calibrated number vs the raw simulation number. You can see the full score distribution in a histogram. You can trace any recommendation back to the exact player stats and weather inputs that drove it.

### 2. Calibration against live results
The model validates itself. Every game simulated and graded becomes a data point for refitting the Platt scaling parameters. The calibration curve is based on actual bets at real closing lines, not synthetic backtests.

### 3. Real inputs, not team-level averages
Most models use team-level run-environment stats. This one uses individual batter and pitcher outcome rates with LHP/RHP splits, regressed to mean based on sample size, blended via log-5. Umpire tendencies and weather are included at the physics level (temperature → carry → HR rate), not as flat multipliers.

### 4. Integrated workflow
From "import today's games" to "here is your bet and how much to put on it" takes 2–3 minutes once lineups are posted. Everything is in one place: the data, the model, the odds, the bet history, the bankroll, the results. No spreadsheet, no browser tab switching.

---

## Current State & Limitations

### What works well
- Simulation engine producing reasonable outputs (totals avg close to market lines)
- Calibration significantly reduces overconfident favorites
- Backtest-validated bet filters (ML favorites 60%+, underdogs +131–+200, totals 58%+)
- Fully automated data pipeline (schedule, odds, weather, umpires)
- Kelly sizing with hard caps, live threshold display
- CLV tracking (gold standard of bet quality)
- Quick Entry for Don Best manual entry
- F5 markets supported (moneyline, totals, run line)

### Known gaps / open questions

**Data**
- FanGraphs import is manual. Player stats are as current as the last export (typically weekly or before a big game).
- No in-game stats (exit velocity, spin rate, shift usage, pitch sequencing). The model treats every plate appearance identically regardless of game situation.
- No injury/lineup-change alerting. A starter being scratched 90 minutes before first pitch is a manual check.
- No pitcher rest days modeled. A starter coming off 3 days rest vs normal 5 is treated the same.

**Model**
- Bullpen is aggregated — one "bullpen pool" per team rather than a specific reliever by inning. High-leverage bullpen matchups (e.g. closer for the final 2 outs vs a weak 7th-inning arm) are not captured.
- Base-running is MLB average for all batters. Fast runners and slow ones get the same advancement rules.
- No platoon advantage beyond the split (e.g. doesn't model a team being particularly aggressive or passive against certain pitch types).
- No stolen base modeling (adds ~0.7 runs/game in aggregate, currently offset by the 1.05 calibration multiplier — a blunt fix).
- Calibration has only 115 data points (moneyline). Totals calibration is the identity function (no calibration) because insufficient live data exists.

**Workflow**
- Simulation must be run manually per game (or via the quick lightning bolt). No automated "simulate all today's games at 11 AM" job yet.
- Lineup import requires the official lineup to be posted first. Before lineups are official, simulation quality depends on the user's best guess.
- Historical F5 odds only cover ~2 months (March–May 2025) — quota ran out. F5 backtest is directionally interesting but underpowered.
- No live in-game betting support.
- No mobile view (desktop only).

**Infrastructure**
- SQLite will bottleneck at scale (fine for 30 games/day, not fine if multiple users or historical import of 5 years of games).
- No authentication — completely open if exposed to the network.
- Runs as a process with no process manager (no auto-restart on crash).

---

## Data Flow: A Single Bet from Start to Finish

1. **8 PM** — Scheduler imports tomorrow's games from MLB API. Database now has: `SEA @ SD, 2025-04-17, 7:10 PM PDT, George Kirby vs Dylan Cease`.

2. **Next morning** — Weather job runs. Petco Park: 68°F, wind 8 mph blowing in from center field. Stored.

3. **10 AM** — Umpire job runs. Home plate: Marty Foster (neutral zone, +0.02 runs/game).

4. **2 hours before game** — User opens the Games tab. Official lineups are posted. Clicks lightning bolt on SEA @ SD. App:
   - Imports official batting orders
   - Runs 10,000 simulations
   - Applies wind-in adjustment (suppresses HRs, extra-base hits slightly)
   - Applies park factor (Petco Park is pitcher-friendly, park factor 0.92)
   - Applies Marty Foster adjustment
   - Returns: SEA 41% / SD 59%, projected total 7.9, F5: SD 57% / SEA 43%

5. **Calibration** — SD's 59% raw → calibrated 52.8%. With -180 at the book, that's only 4.0% edge. Below the 6% floor → no SD moneyline bet. The threshold display shows: "Need -163 or better" to have a 6% edge on SD.

6. **Odds at Don Best** — User sees SD -155. Below the threshold (-163 minimum). No bet.

7. **Total line** — User types `7.5` into the total line field. App calls `/api/game/42/ou-prob?line=7.5`. Returns: Over 46% / Under 54%. Under price at -118. Edge = 54% − 52.3% = +1.7%. Below threshold. No bet.

8. **User checks another game** — PHI -152 (Phillies are 67% in simulation, calibrated 57.1%, threshold is -140). -152 is better than -140. Edge = 57.1% − 60.3% = ... wait, implied probability at -152 is 60.3%. 57.1% − 60.3% = −3.2%. Negative edge on the favorite despite high confidence. Pass.

9. **Quick Entry** — User enters lines from Don Best for all 10 games. Hits Analyze. Three bets surface: NYM +165 (underdog sweet spot, 52% model, +18% edge), BOS Under 8.5 at -108 (61% model, +12% edge), MIA +180 (51% model, +14% edge).

10. **Bet placed** — User bets $45 on NYM (Kelly sizing: 25% × bankroll × 18% edge ÷ 1.65 = $45, capped at $50 max). Logs it in BettingEdge as placed.

11. **7 PM** — NYM wins 5-3. "Settle Games" fetches the final score. Bet graded won. +$74.25 logged to bankroll. Closing line was NYM +148 — user got +165, CLV = +17 cents (strong).

---

## Backtesting Summary

Two seasons of backtests have been run to validate and tune the model. Key findings:

| Filter | WR | ROI | Notes |
|---|---|---|---|
| ML Favorites 60–65% raw | 70% | +34.3% | Core bucket — reliable signal |
| ML Favorites 65–70% raw | 55% | −7.4% | Overconfident — edge floor raised to 8% |
| ML Favorites 70–75% raw | 86% | +25.2% | Small sample, strong signal |
| ML Underdogs +131–+200 | varies | +21–34% | Sweet spot for underdog value |
| ML Underdogs +100–+130 | varies | −3 to −29% | Model too noisy near even money |
| ML Underdogs +200+ | varies | −10.5% | Model can't reliably predict long shots |
| Totals 58–64% raw | 54–63% | +6–24% | Main totals signal |
| Run Line | 50% | −8.8% | Disabled — model not reliable here |

F5 backtesting is incomplete (only 2 months of data), but directionally shows +7% F5 ML ROI. Full-season data needed.

---

## Appendix: Settings Reference

| Setting | Default | Range | Effect |
|---|---|---|---|
| Min edge % | 6.0% | 0.5–25% | Below this edge, no recommendation is generated |
| Kelly fraction | 0.25 | 0.05–1.0 | Multiplier on full Kelly. 0.25 = quarter Kelly. |

Hard-coded limits:
- Max bet: 5% of bankroll
- Min bet: $5
- Max favorite odds to recommend: −225
- Underdog range: +131 to +200 only
- Min raw ML confidence: 60% (favorites), 50% (underdogs)
- Min raw totals confidence: 58%
