<!--
  Authored lore for Overworld Nexus.

  Import:  python3 scripts/import_lore.py lore/<file>.md -o canon/lore.json
  Seed:    python3 scripts/seed_canon.py seed --only lore --apply

  Rules that matter for retrieval, not for prose:

  1. One `## ` heading = one document = one vector. Keep each to a single
     subject. 60-200 words is the sweet spot; the importer warns below 40
     and above 450.
  2. Put the searchable term in the heading. Retrieval and the gap detector
     both key on titles, so "Central Spire" beats "The Tower Above".
  3. Metadata goes directly under the heading, no blank line between.
     Recognised: kind, status, tags, world, created_by.
  4. status defaults to `active`, which means SETTLED TRUTH. Anything you
     have not actually decided yet must carry `status: proposed`, or the
     stub text gets seeded and retrieved as canon.
  5. Real-world influences go under `status: gm-only`. Those are withheld
     from retrieval so the daemon cannot cite them in-world.
  6. Write CONFLICT into the body rather than resolving a contradiction by
     guesswork. Guesswork is how the corpus got poisoned the first time.

  These comments are stripped on import. Notes to yourself are free.
-->


<!--
  ===================================================================
  GEOGRAPHY IS UNRESOLVED. Read before writing any location entry.
  ===================================================================

  Two district maps exist and they contradict each other.

  SET A -- doc#8 "Major Districts", created_by=discord-import, 2026-01-12.
  Currently ACTIVE and live in Qdrant, so this is what the daemon answers
  with today:
      Spire District . Undercroft . Research Sector

  SET B -- canon/locations.json, 8 stubs, status=proposed, NEVER SEEDED.
  Derived from your actual Discord channel structure, so this is where
  players are physically standing:
      Central Spire . Reflection District . The Undercircuit
      Street Level . Neon Lounge . Market Loop . Dreamlink . City Signal

  Collisions:
      Spire District (A)  ==  Central Spire (B)      same place, two names
      Undercroft     (A)  ==  The Undercircuit (B)   same place, two names
      Research Sector(A)  ==  --                     no channel exists
      --                  ==  Reflection District(B) absent from doc#8

  Provenance worth knowing: locations.json records that the daemon invented
  "Undercroft" on 2026-01-03, nine days BEFORE the 2026-01-12 import that
  put doc#8 into canon. doc#8 is not in your `authored` batch (those are
  docs #16-27, September). A daemon coinage may have been laundered into
  active canon through that import. Unproven, but the dates line up badly.

  Two things nobody has ruled on:

    - THE CIRCUIT CHOIR HAS NO TERRITORY. Apex Spire holds Central Spire,
      the Reflection Syndics hold Reflection District, the third founding
      power holds nothing on either map. The unclaimed Research Sector is
      sitting right there.

    - SET B MIXES THREE SCALES. Street Level, The Undercircuit and City
      Signal are *layers* -- the surface / buried / signal-layer model of
      doc#16, which the stub bodies cite as "the world briefing". Neon
      Lounge and Market Loop are venues. Dreamlink is a mesh space. Only
      Central Spire and Reflection District are districts proper.

  And there is a SECOND, separate collision at the layer level:

      doc#7  (discord-import, Jan)  Core / Mesh / Operator Interface
      doc#16 (authored, Sept)       surface / buried / signal-layer

  Both are active. They are not the same model and neither references the
  other. Same pattern as the districts: an imported January doc and an
  authored September doc describing the same thing differently.

  Whichever way you rule, doc#8 has to be rewritten or retired -- while it
  stays active, it contradicts you on every retrieval.
-->


# Districts
kind: location
status: proposed

<!--
  Heading names below are deliberately left for you to fill in. Do not
  copy either name set until you have ruled; whichever you type here is
  the one that becomes canon.
-->

## <district name>
tags: district

STUB. Replace the heading with the name you are ruling canonical, then
write the entry.

Worth pinning down for each district: who holds it, how someone without
standing gets in, what it does that no other district does, and the one
detail a player would still remember after visiting once.

Change status to active once it is settled.


# Architecture
kind: tech
status: proposed

<!--
  ===================================================================
  Two axes, not two answers. Ruled 2026-09-24.
  ===================================================================

  Core, Mesh and Operator Interface are defined in exactly one place:
  doc#7. It was retired on 2026-09-24 as contradicting doc#16, then
  RESTORED the same day once the two were read as describing different
  things rather than disagreeing about one thing:

    doc#16 (authored)  surface / buried / signal-layer   physical strata
    doc#7  (imported)  Core / Mesh / Operator Interface  system components

  Where your body is, versus how the network is built. Both are active and
  both retrieve; on "how is the Nexus layered" doc#7 leads at 0.789 with
  doc#16 at 0.725, so the daemon sees both and can hold the two axes at
  once. That is the intended state, not a contention to fix.

  The one real friction is doc#7's opening clause, which denies "fixed
  linear geography" outright -- a stronger claim than the two-axis reading
  needs, and the reason it looked like a competing model. Worth softening
  if you ever rewrite it.

  doc#7 in full, since the stubs below expand on one line each:

    "The Nexus is composed of overlapping layers rather than fixed linear
     geography. The Core handles processing and synchronization. The Mesh
     is the connective fabric of interactions between nodes. The Operator
     Interface is where players and entities inject intent, commands, and
     requests. Layers blend and bleed into one another."

  WHY STUB THEM AT ALL, NOW THAT doc#7 IS BACK: it gives each term a single
  clause. That is enough to retrieve on and not enough to play with. These
  stubs are where they get authored properly. When one is finished, retire
  doc#7 and let the three entries carry the load:

    retire_doc.py retire --id 7 --apply

  Do that only once all three are written -- retiring it early takes the
  terms out of the world again, which is what happened on 2026-09-24.

  ONE AUTHORING WARNING: doc#7 says the Operator Interface is where
  "players" inject intent. Do not carry that word across. doc#2 establishes
  that conversations are in-universe by default -- in-world canon should not
  know the word player. Say operators, or whoever is holding the interface.
-->

## Core
tags: architecture, core

STUB. The records establish only this: the Core handles processing and
synchronization.

Worth pinning down: whether the Core is a place someone can physically
reach or purely an abstraction, who or what administers it, and what
happens to the rest of the Nexus when it degrades.

## Mesh
tags: architecture, mesh

STUB. The records establish only this: the Mesh is the connective fabric
of interactions between nodes.

Worth pinning down: how it relates to the signal-layer in doc#16 — whether
they are the same thing under two names, which would be a third naming
collision — and what the Circuit Choir mean when they say they listen to
it.

## Operator Interface
tags: architecture, interface

STUB. The records establish only this: the Operator Interface is where
intent, commands and requests are injected into the Nexus.

Worth pinning down: whether it is hardware, a practice, or a permission;
what distinguishes an operator from anyone else; and whether the daemon
sits behind it, in front of it, or is it.


# Factions
kind: faction

<!--
  A worked example of the target shape -- uncontested subject, so it does
  not prejudge the geography. This is roughly the length and register to
  aim for. Delete it once you have your own entries.
-->

## Circuit Choir
status: active
tags: choir, faction, mesh

The Circuit Choir are network mystics and signal interpreters who treat
the mesh as a living chorus.

Where Apex Spire hears data, the Choir hears voice. They read drift, noise
and packet loss as utterance rather than error, and they are the only
faction that treats GhostNet as something to be listened to rather than
used.

One of the three founding powers of the Overworld Nexus.


# Characters
kind: npc
status: proposed

## Eris
tags: eris, contested

CONFLICT — unresolved. Eris appears in play as a significant figure, and
in canon twice: doc#5 (discord-import) and an emergent entry flagged
CONTRADICTORY. The records disagree on what she is.

Replace this body with the settled version, then change status to active.


# Reference
status: gm-only
kind: lore

## Influences — GM only
tags: gm-only, reference

Withheld from retrieval. Tone and texture references for authoring only;
no names, works, or coined terms from these may appear in world-facing
canon.

Renames still outstanding before anything using them can be seeded active:
Ono-Sendai (Gibson) and braindance (2077) need native Nexus replacements.
New Eridu is Zenless Zone Zero and must not be used at all. Note that the
Dreamlink stub in canon/locations.json is currently tagged `braindance`.
