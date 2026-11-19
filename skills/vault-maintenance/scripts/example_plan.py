#!/usr/bin/env python3
"""Example plan: split the five oversized dirs diag.py flagged in one vault.

Copy it outside the skill directory and adapt it; it only applies to the
vault it was written for.

  hobbies/reading (34)  hobbies/board-games (24)  programming/clojure/web (18)
  electronics/projects (18)  security/pentesting (16)

All 23 new subfolder names were checked against every note basename in
Areas/ first - none collide, so none needs the clojure-editors/ style
prefix. Basenames don't change, so the [[wikilinks]] in index notes still
resolve by name.

mathematics/theorem-of-the-day (16) is deliberately left flat: its notes are
the source site's own subject taxonomy and carry 15-53 entries each, so it
did not grow by accident and there is no finer split the source supports.

security/pentesting keeps its four existing subfolders; the new ones sit
alongside them. "tools/" would have been the obvious name for the largest
group but the vault root already has Tools/ - hence pentesting-tools/.

`with Migration() as m` holds the vault lock across the whole read-plan-write
cycle and flushes on the way out; see Migration's docstring for why the read
has to be inside the lock too.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent  # in a copy elsewhere: Path("<skill dir>/scripts")
sys.path.insert(0, str(SCRIPTS))
import migrate
from migrate import Migration, log

RD = "hobbies/reading"
BG = "hobbies/board-games"
WEB = "programming/clojure/web"
PRJ = "electronics/projects"
PEN = "security/pentesting"


def plan(m):
    # ── hobbies/reading: by genre, with the author/criticism pieces split
    # off from the books they are about (douglas-adams and
    # neil-gaiman-allegations are about the writers, not about one book).
    m.move_many(RD, ["battletech-novels", "children-of-time", "cryptonomicon",
                     "daemon-daniel-suarez", "enders-game-series",
                     "final-architecture-series", "project-hail-mary",
                     "the-road", "war-with-the-newts"], f"{RD}/science-fiction")
    m.move_many(RD, ["broken-earth-series", "chronicles-of-amber",
                     "fabius-bile-series", "igra-haosa",
                     "iron-druid-chronicles", "malazan-book-of-the-fallen",
                     "necroscope-series", "perdido-street-station",
                     "stormlight-archive", "the-devils",
                     "the-library-at-mount-char", "the-locked-tomb-series",
                     "the-name-of-the-wind", "the-shadow-of-the-torturer"],
                f"{RD}/fantasy")
    m.move_many(RD, ["a-wizard-of-earthsea", "der-wal-der-immer-mehr-wollte",
                     "die-streithornchen", "wonderful-wizard-of-oz-volkov"],
                f"{RD}/childrens-books")
    m.move_many(RD, ["cains-jawbone", "professor-puzzles-puzzles"],
                f"{RD}/puzzle-books")
    m.move_many(RD, ["practical-doomsday", "simplicity-edward-de-bono"],
                f"{RD}/nonfiction")
    m.move_many(RD, ["camus-the-stranger", "douglas-adams",
                     "neil-gaiman-allegations"], f"{RD}/authors-and-criticism")

    # ── hobbies/board-games: the games by primary form, then the shops and
    # tools that are not games at all. fox-go-dataset (a Go corpus for game
    # AI) and top-10-board-games-2025 (a roundup, not one game) stay put.
    m.move_many(BG, ["3-chapters", "9-lives", "claim-stitch-game",
                     "coincidence-store", "fluxx", "love-letter", "nyet",
                     "ruins", "sail", "wizards-of-the-grimoire"],
                f"{BG}/card-games")
    m.move_many(BG, ["age-of-innovation", "arcs", "hadrians-wall", "wyrmspan"],
                f"{BG}/strategy-games")
    m.move_many(BG, ["bomb-busters", "earthborne-rangers",
                     "gloomhaven-jaws-of-the-lion", "spirit-island"],
                f"{BG}/cooperative-games")
    m.move_many(BG, ["brettspielpreise", "mpc-autofill", "spielradar",
                     "tabletopfinder"], f"{BG}/shops-and-tools")

    # ── clojure/web: the rendering stack, the no-build browser stack, the
    # compiler toolchain, the server side, then the worked examples.
    m.move_many(WEB, ["clojure-reactive-ui-signals", "electric-v3",
                      "hiccup-macro-performance", "re-frisk",
                      "reagent-katex-math-rendering", "reagent-react-interop",
                      "rum-clojurescript"], f"{WEB}/reagent-and-react")
    m.move_many(WEB, ["pyodide-scittle-clojurescript",
                      "scittle-reagent-presentations",
                      "scittle-weather-app-tutorial"], f"{WEB}/scittle")
    m.move_many(WEB, ["clojurescript-closure-compiler",
                      "clojurescript-import-maps"], f"{WEB}/clojurescript-build")
    m.move_many(WEB, ["clojure-http-router", "pedestal"],
                f"{WEB}/routing-and-servers")
    m.move_many(WEB, ["acha-acha-web-app", "allekinos",
                      "clojurescript-pdf-viewer", "clojurescript-qr-scanner"],
                f"{WEB}/example-apps")

    # ── electronics/projects: by what the build is made of. The three left
    # at the root (a CNC'd threat-level dial, a robot, glow sticks) share no
    # family with anything else here.
    m.move_many(PRJ, ["avr-audio-recording-toy", "avr-handheld-arcade-games",
                      "avr-handheld-cipher-device", "avr-sokoban-implementation",
                      "bob-the-cat-mcu-build", "sir-box-a-lot", "sketchy-sketch"],
                f"{PRJ}/avr-builds")
    m.move_many(PRJ, ["phosphorescence-detector", "radiation-detecting-rgb-lamp",
                      "radiation-monitoring-lnd-7121"],
                f"{PRJ}/radiation-and-sensing")
    m.move_many(PRJ, ["conways-game-of-life-hardware",
                      "relay-calculator-full-adder", "shannons-ultimate-machine"],
                f"{PRJ}/discrete-logic-machines")
    m.move_many(PRJ, ["voltmeter-calculator-build", "voltmeter-clock-build"],
                f"{PRJ}/analog-meter-builds")

    # ── security/pentesting: tools, the two protocol-level attacks, the two
    # write-ups of deliberately vulnerable targets. silence-on-the-wire is a
    # book review and stays at the root.
    m.move_many(PEN, ["cookiemonster", "cyberchef", "jitsi-meet-raid-tool",
                      "mitmproxy", "p0f", "pwntools", "rustscan", "ysoserial",
                      "zenmap"], f"{PEN}/pentesting-tools")
    m.move_many(PEN, ["gstreamer-flic-heap-exploit",
                      "sql-injection-visualization"], f"{PEN}/exploitation")
    m.move_many(PEN, ["tcp-ip-isn-predictability",
                      "tls-sni-censorship-circumvention"], f"{PEN}/network-attacks")
    m.move_many(PEN, ["breachlab", "pwnlab-init"], f"{PEN}/labs-and-walkthroughs")


def main():
    migrate.DRY = "--apply" not in sys.argv
    with Migration() as m:
        before_e = sum(len(m.content(r)) for r in m.notes)
        before_n = len([r for r in m.notes if m.content(r)])
        plan(m)
    after_e = sum(len(m.content(r)) for r in m.notes)
    after_n = len([r for r in m.notes if m.content(r)])
    for line in log:
        print(line)
    print(f"\n{len(log)} ops | notes {before_n} -> {after_n} | "
          f"entries {before_e} -> {after_e}"
          f"{'  (DRY RUN)' if migrate.DRY else '  APPLIED'}")
    if before_e != after_e:
        print("!! entry count changed - nothing here was meant to drop an entry")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
