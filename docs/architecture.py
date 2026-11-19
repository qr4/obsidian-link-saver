"""Generate docs/architecture-{light,dark}.svg: python3 docs/architecture.py docs"""
import sys
from html import escape
from pathlib import Path

THEMES = {
    "light": dict(text="#1f2328", muted="#59636e", panel="#f6f8fa", panel_stroke="#d1d9e0",
                  box="#ffffff", box_stroke="#afb8c1", sync="#0f7b6c", save="#0969da",
                  store="#9a6700", ext="#8250df", sync_bg="#e6f4f1", save_bg="#ddf4ff",
                  store_bg="#fff8c5", ext_bg="#fbefff"),
    "dark": dict(text="#e6edf3", muted="#9198a1", panel="#151b23", panel_stroke="#3d444d",
                 box="#0d1117", box_stroke="#656c76", sync="#3fb9a6", save="#4493f8",
                 store="#d29922", ext="#ab7df8", sync_bg="#0f2a26", save_bg="#0c2d6b",
                 store_bg="#3b2e0a", ext_bg="#2a1a4a"),
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
W, H = 1120, 660


def svg(theme):
    c = THEMES[theme]
    out = []
    add = out.append

    def text(x, y, s, size=13, color=None, weight="normal", anchor="middle", italic=False):
        style = ' font-style="italic"' if italic else ""
        add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color or c["text"]}" '
            f'text-anchor="{anchor}"{style}>{escape(s)}</text>')

    def panel(x, y, w, h, title, dashed=False):
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{c["panel"]}" '
            f'stroke="{c["panel_stroke"]}" stroke-width="1.5"{dash}/>')
        text(x + 16, y + 26, title, size=13, color=c["muted"], weight="600", anchor="start")

    def box(x, y, w, h, title, sub=(), accent=None, fill=None, dashed=False):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill or c["box"]}" '
            f'stroke="{accent or c["box_stroke"]}" stroke-width="{2 if accent else 1.2}"{dash}/>')
        lines = 1 + len(sub)
        top = y + h / 2 - (lines - 1) * 8.5 + 5
        text(x + w / 2, top, title, size=15, weight="600")
        for i, s in enumerate(sub):
            text(x + w / 2, top + 19 + i * 16, s, size=12, color=c["muted"])

    def cylinder(x, y, w, h, title, sub=()):
        e = 9  # ellipse half-height
        col, bg = c["store"], c["store_bg"]
        add(f'<path d="M{x},{y + e} v{h - 2 * e} a{w / 2},{e} 0 0 0 {w},0 v-{h - 2 * e}" '
            f'fill="{bg}" stroke="{col}" stroke-width="2"/>')
        add(f'<ellipse cx="{x + w / 2}" cy="{y + e}" rx="{w / 2}" ry="{e}" fill="{bg}" stroke="{col}" stroke-width="2"/>')
        lines = 1 + len(sub)
        top = y + h / 2 - (lines - 1) * 8.5 + 9
        text(x + w / 2, top, title, size=15, weight="600")
        for i, s in enumerate(sub):
            text(x + w / 2, top + 19 + i * 16, s, size=12, color=c["muted"])

    def pill(x, y, w, h, label, accent):
        add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h / 2}" fill="{c["box"]}" '
            f'stroke="{accent}" stroke-width="2"/>')
        text(x + w / 2, y + h / 2 + 4.5, label, size=12, weight="600")

    def edge(points, kind, both=False, dashed=False, label=None, label_at=None, label_anchor="middle", arrow=True):
        col = c[kind]
        d = "M" + " L".join(f"{px},{py}" for px, py in points)
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        start = f' marker-start="url(#a-{kind})"' if both else ""
        end = f' marker-end="url(#a-{kind})"' if arrow else ""
        add(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2"{dash}{start}{end}/>')
        if label:
            lx, ly = label_at
            for i, part in enumerate(label.split("\n")):
                text(lx, ly + i * 15, part, size=12, color=col, weight="600", anchor=label_anchor)

    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'font-family="{FONT}" role="img" aria-labelledby="t d">')
    add('<title id="t">How obsidian-link-saver fits together</title>')
    add('<desc id="d">Obsidian on your devices syncs through the LiveSync plugin with CouchDB behind a reverse '
        'proxy. The livesync-bridge keeps CouchDB and the vault copy on the server in sync. The saver service, '
        'reached through the proxy by the web UI, bookmarklet and obsidian-save client, '
        'writes notes into the vault copy, stores videos and git clones in the blob store, fetches content '
        'from the web, and asks Ollama to tag, summarize and file each link. SingleFile in the browser, with its '
        'Companion, saves pages into the desktop vault\'s inbox folder; LiveSync carries them to the vault copy, '
        'where the saver watches the inbox and imports them. '
        'Claude Code edits the notes in the '
        'desktop vault and calls the saver API for the registry, vault lock and folder indexes.</desc>')
    add("<defs>")
    for kind in ("sync", "save", "store", "ext"):
        add(f'<marker id="a-{kind}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c[kind]}"/></marker>')
    add("</defs>")

    # --- panels ---
    panel(20, 60, 220, 560, "YOUR DEVICES")
    panel(262, 60, 136, 560, "REVERSE PROXY", dashed=True)
    panel(420, 60, 470, 560, "DOCKER COMPOSE STACK")
    text(W / 2, 34, "How obsidian-link-saver fits together", size=18, weight="700")

    # --- devices ---
    for y, device in ((100, "laptop"), (160, "phone"), (220, "desktop")):
        box(40, y, 180, 50, "Obsidian", (f"{device} · LiveSync plugin",), accent=c["sync"], fill=c["sync_bg"])
    box(40, 292, 180, 54, "Claude Code", ("vault-maintenance skill",), dashed=True)
    box(40, 362, 180, 66, "Browser", ("web UI · bookmarklet", "SingleFile (+ Companion)"),
        accent=c["save"], fill=c["save_bg"])
    box(40, 445, 180, 50, "obsidian-save", ("CLI & keybinding",))
    for i, line in enumerate(("Claude Code edits the desktop",
                              "vault and uses the saver's API.",
                              "SingleFile with its Companion",
                              "saves pages into the vault's",
                              "inbox folder for the saver to",
                              "import; without it, drop the",
                              "file onto the web UI.")):
        text(40, 522 + i * 15, line, size=11, color=c["muted"], anchor="start")

    # --- proxy ---
    pill(274, 132, 112, 36, "sync.example", c["sync"])
    pill(274, 372, 112, 36, "saver.example", c["save"])
    text(330, 190, "HTTPS", size=11, color=c["muted"])
    text(330, 205, "end-to-end encrypted", size=11, color=c["sync"], weight="600")
    text(330, 436, "HTTPS", size=11, color=c["muted"])

    # --- stack ---
    box(440, 105, 160, 90, "CouchDB", ("LiveSync database",), accent=c["sync"], fill=c["sync_bg"])
    box(700, 105, 170, 90, "livesync-bridge", ("files ⇄ CouchDB", "documents"), accent=c["sync"], fill=c["sync_bg"])
    box(440, 320, 160, 120, "saver", ("web UI · API · workers", "Whisper · SQLite", "imports the inbox",
                                       "serves /watch, /blobs"),
        accent=c["save"], fill=c["save_bg"])
    cylinder(700, 310, 170, 100, "Vault copy", ("Areas/ · Dumps/",))
    cylinder(700, 450, 170, 95, "Blob store", ("videos · git clones",))

    # --- external ---
    box(930, 395, 170, 70, "Ollama", ("tag · summarize · file",), accent=c["ext"], fill=c["ext_bg"])
    box(930, 545, 170, 70, "The web", ("pages · repos · videos",), accent=c["ext"], fill=c["ext_bg"])

    # --- edges: sync ---
    for y in (125, 185, 245):  # shared line from every device to the sync endpoint
        edge([(250, y), (220, y)], "sync")
    add(f'<path d="M250,125 L250,245" stroke="{c["sync"]}" stroke-width="2" fill="none"/>')
    edge([(250, 150), (274, 150)], "sync")
    edge([(386, 150), (440, 150)], "sync", both=True)
    edge([(600, 150), (700, 150)], "sync", both=True)
    edge([(785, 195), (785, 310)], "sync", both=True, label="keeps in sync", label_at=(795, 257), label_anchor="start")

    # --- edges: saving ---
    edge([(220, 390), (274, 390)], "save")
    edge([(220, 470), (250, 470), (250, 390)], "save", arrow=False)
    # SingleFile Companion: page captures land in the desktop vault's inbox
    edge([(40, 410), (30, 410), (30, 245), (40, 245)], "store", dashed=True)
    edge([(220, 319), (250, 319), (250, 390)], "save", dashed=True, arrow=False)
    edge([(386, 390), (440, 390)], "save")
    edge([(130, 292), (130, 270)], "store", label="edits notes", label_at=(140, 286), label_anchor="start")

    # --- edges: storage ---
    edge([(600, 360), (700, 360)], "store", label="notes,\ntranscripts", label_at=(650, 336))
    # the inbox watcher: SingleFile captures synced into Dumps/singlefile_inbox
    edge([(700, 392), (600, 392)], "store", dashed=True, label="watches inbox", label_at=(650, 410))
    edge([(580, 440), (580, 497), (700, 497)], "store", label="bulky files", label_at=(592, 489),
         label_anchor="start")

    # --- edges: external ---
    edge([(600, 430), (930, 430)], "ext", label="LLM", label_at=(910, 422))
    edge([(480, 440), (480, 580), (930, 580)], "ext", label="SingleFile · git · yt-dlp", label_at=(494, 600),
         label_anchor="start")

    add("</svg>")
    return "\n".join(out) + "\n"


dest = Path(sys.argv[1])
for theme in THEMES:
    (dest / f"architecture-{theme}.svg").write_text(svg(theme), encoding="utf-8")
