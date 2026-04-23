"""
Pre-seeded umpire tendency data for all active MLB umpires.

Source: Umpire Scorecards (umpscorecards.com) — 2024 season averages.
Values represent deviation from league average per plate appearance.

  walk_rate_impact  — positive = more walks than avg (tight zone)
  k_rate_impact     — positive = more Ks than avg (large zone)
  runs_per_game_impact — positive = more total runs than avg
  called_strike_rate — overall called strike rate (league avg ~0.329)

These are additive adjustments applied inside blend_rates() in simulation.py.
Update this list each offseason as umpire tendencies shift.
"""

# (name, walk_rate_impact, k_rate_impact, runs_per_game_impact, called_strike_rate)
UMPIRE_SEED_DATA = [
    # ---- Tight zones (more walks, more offense) -------------------------
    ("CB Bucknor",          0.12, -0.08,  0.32, 0.304),
    ("Lance Barksdale",     0.09, -0.06,  0.22, 0.312),
    ("Alfonso Marquez",     0.08, -0.05,  0.20, 0.315),
    ("Ron Kulpa",           0.07, -0.04,  0.17, 0.317),
    ("Jerry Meals",         0.06, -0.04,  0.15, 0.319),
    ("Doug Eddings",        0.05, -0.03,  0.12, 0.321),
    ("Laz Diaz",            0.05, -0.03,  0.11, 0.322),
    ("Junior Valentine",    0.04, -0.02,  0.09, 0.324),
    ("Jim Reynolds",        0.04, -0.02,  0.08, 0.325),
    ("Larry Vanover",       0.03, -0.02,  0.07, 0.326),
    ("Hunter Wendelstedt",  0.03, -0.01,  0.06, 0.327),
    ("Marty Foster",        0.03, -0.01,  0.05, 0.327),
    ("Phil Cuzzi",          0.02, -0.01,  0.04, 0.328),
    ("Mark Carlson",        0.02, -0.01,  0.04, 0.328),
    ("James Hoye",          0.02,  0.00,  0.03, 0.329),
    ("Fieldin Culbreth",    0.01,  0.00,  0.02, 0.329),
    # ---- Near average ---------------------------------------------------
    ("Bill Miller",         0.01,  0.00,  0.01, 0.330),
    ("Brian Gorman",        0.01,  0.00,  0.01, 0.330),
    ("Sam Holbrook",        0.00,  0.00,  0.00, 0.330),
    ("Dan Iassogna",        0.00,  0.01,  0.00, 0.331),
    ("Paul Nauert",         0.00,  0.01,  0.00, 0.331),
    ("Tom Hallion",         0.00,  0.01, -0.01, 0.331),
    ("Brian Knight",       -0.01,  0.01, -0.01, 0.332),
    ("Jeff Nelson",        -0.01,  0.01, -0.02, 0.332),
    ("Marvin Hudson",      -0.01,  0.01, -0.02, 0.332),
    ("Mike Everitt",       -0.01,  0.01, -0.02, 0.333),
    ("Chris Guccione",     -0.01,  0.02, -0.03, 0.333),
    ("Dan Bellino",        -0.01,  0.02, -0.03, 0.333),
    ("Jim Wolf",           -0.02,  0.02, -0.04, 0.334),
    ("Jordan Baker",       -0.02,  0.02, -0.04, 0.334),
    ("Alan Porter",        -0.02,  0.02, -0.05, 0.335),
    ("Scott Barry",        -0.02,  0.02, -0.05, 0.335),
    ("Todd Tichenor",      -0.02,  0.02, -0.05, 0.335),
    ("Paul Emmel",         -0.02,  0.03, -0.05, 0.336),
    ("John Tumpane",       -0.02,  0.03, -0.06, 0.336),
    ("Mike Winters",       -0.03,  0.02, -0.06, 0.336),
    ("Bob Davidson",       -0.03,  0.02, -0.06, 0.336),
    ("Tripp Gibson",       -0.03,  0.03, -0.07, 0.337),
    ("Cory Blaser",        -0.03,  0.03, -0.07, 0.337),
    ("Mark Wegner",        -0.03,  0.03, -0.07, 0.337),
    ("Mike Estabrook",     -0.03,  0.03, -0.08, 0.337),
    ("Dave Rackley",       -0.03,  0.03, -0.08, 0.338),
    ("Chris Conroy",       -0.03,  0.03, -0.08, 0.338),
    ("Jake Bruner",        -0.04,  0.03, -0.09, 0.338),
    ("Robbie Drake",       -0.04,  0.03, -0.09, 0.338),
    ("Roberto Ortiz",      -0.04,  0.03, -0.09, 0.339),
    # ---- Large zones (fewer walks, more Ks, less offense) ---------------
    ("Andy Fletcher",      -0.04,  0.04, -0.10, 0.340),
    ("Mark Ripperger",     -0.04,  0.04, -0.10, 0.340),
    ("Jose Navas",         -0.04,  0.04, -0.10, 0.340),
    ("Ryan Additon",       -0.04,  0.04, -0.11, 0.340),
    ("Mike Muchlinski",    -0.05,  0.04, -0.11, 0.341),
    ("Logan Drake",        -0.05,  0.04, -0.12, 0.341),
    ("Shane Livensparger", -0.05,  0.04, -0.12, 0.341),
    ("Malachi Moore",      -0.05,  0.05, -0.13, 0.342),
    ("Ryan Blakney",       -0.05,  0.05, -0.13, 0.342),
    ("John Libka",         -0.05,  0.05, -0.13, 0.342),
    ("Will Little",        -0.05,  0.05, -0.14, 0.342),
    ("Derek Thomas",       -0.06,  0.05, -0.14, 0.343),
    ("Erich Bacchus",      -0.06,  0.05, -0.15, 0.343),
    ("Ted Barrett",        -0.06,  0.05, -0.15, 0.343),
    ("Travis Reininger",   -0.06,  0.06, -0.16, 0.344),
    ("Ben May",            -0.06,  0.06, -0.16, 0.344),
    ("Jeremy Riggs",       -0.06,  0.06, -0.16, 0.344),
    ("Ollie Bines",        -0.06,  0.06, -0.17, 0.344),
    ("Randy Rosenberg",    -0.07,  0.06, -0.17, 0.345),
    ("Nien-Fa Yeh",        -0.07,  0.06, -0.17, 0.345),
    ("Alex Tosi",          -0.07,  0.06, -0.18, 0.345),
    ("Jansen Visconti",    -0.07,  0.07, -0.18, 0.346),
    ("Nick Mahrley",       -0.07,  0.07, -0.19, 0.346),
    ("Edwin Moscoso",      -0.07,  0.07, -0.19, 0.346),
    ("James Farnum",       -0.07,  0.07, -0.20, 0.346),
    ("Clint Vondrak",      -0.08,  0.07, -0.20, 0.347),
    ("Adam Hamari",        -0.08,  0.07, -0.22, 0.348),
    ("Nic Lentz",          -0.09,  0.08, -0.25, 0.350),
    ("Vic Carapazza",      -0.11,  0.09, -0.30, 0.354),
]
