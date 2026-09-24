<!--
  Authored lore for Overworld Nexus.

  Import:  python scripts/import_lore.py lore/TEMPLATE.md -o canon/lore.json
  Seed:    python scripts/seed_canon.py seed --only lore --apply

  Rules that matter for retrieval, not for prose:

  1. One `## ` heading = one document = one vector. Keep each to a single
     subject. 60-200 words is the sweet spot; the importer warns below 40
     and above 450.
  2. Put the searchable term in the heading. Retrieval and the gap detector
     both key on titles, so "Spire District" beats "The Towers Above".
  3. Metadata goes directly under the heading, no blank line between.
     Recognised: kind, status, tags, world, created_by.
  4. Real-world influences go under `status: gm-only`. Those are withheld
     from retrieval so the daemon cannot cite them in-world.
  5. Write CONFLICT into the body rather than resolving a contradiction by
     guesswork. Guesswork is how the corpus got poisoned the first time.
-->

# Districts
kind: location

## Spire District
tags: spire, corporate, apex

STUB — needs authoring. What is actually up there besides altitude?

Known from canon: luxury, power, corporate towers. Apex Spire holds the
upper stacks and measures status by altitude.

Open questions worth answering here: how does someone without standing get
in, what does the district do that the others cannot, and what is the one
detail a player would remember after visiting once.

## Research Sector
tags: research, prototypes, forbidden-tech

STUB — needs authoring.

Known from canon: experimentation, prototypes, forbidden tech. District
borders are semi-fluid and shift.

## Undercroft
tags: undercroft, smugglers, market

STUB — needs authoring.

Known from canon: smugglers, scavengers, illicit markets.


# Technology
kind: tech

## Network Linking
tags: mobility, mesh

STUB — needs authoring. Currently the only source is a single line about
mobility being partly digital.

Worth pinning down: is this transit, telepresence, or identity projection,
and what does it cost the person doing it.


# Characters
kind: npc

## Eris
tags: eris, contested

CONFLICT — unresolved. Eris appears in play as a significant figure but
the records disagree on what she is. Needs a ruling before this becomes
active canon.

Replace this body with the settled version, then change status to active.


# Reference
status: gm-only
kind: lore

## Influences — GM only
tags: gm-only, reference

Withheld from retrieval. Tone and texture references for authoring only;
no names, works, or coined terms from these may appear in world-facing
canon.

Renames still outstanding: Ono-Sendai (Gibson) and braindance (2077) must
be replaced with native Nexus terms before either can be seeded active.
New Eridu is Zenless Zone Zero and must not be used at all.
