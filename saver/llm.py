"""Ollama calls and the tagging/summary/title/area prompts."""
import json
import re

import requests

from .config import settings
from .errors import ContentFreeError
from .jobctx import log
from .util import tree_to_string
from .vaultnotes import collect_existing_tags


def extract_json(text: str):
    """Pull the first valid JSON object out of a string that may contain
    extra text, <think> tags, or ```json code fences.

    Every prompt asks for a JSON *object*, but a small/fast model sometimes
    answers with just the bare value (["manuals", "reference"] for a tags
    prompt - happened on manualslib.com). Without the isinstance checks
    that bare list would sail through as a "successful" parse and every
    caller's parsed.get(...) would crash instead of hitting the normal
    malformed-response fallback."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    fenced = re.findall(r"```json(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced[0]

    brace_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if brace_match:
        try:
            parsed = json.loads(brace_match.group())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def call_raw(prompt: str) -> str:
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "think": settings.ollama_think,
        "options": {"temperature": settings.ollama_temperature},
    }
    response = requests.post(f"{settings.ollama_url}/api/generate", json=payload,
                             timeout=settings.ollama_timeout)
    response.raise_for_status()
    return response.json().get("response", "")


def call_json(prompt: str) -> dict:
    """call_raw() + extract_json(). Returns {} on a parse failure."""
    return extract_json(call_raw(prompt)) or {}


def call(prompt: str, key: str, default):
    result = call_raw(prompt)
    parsed = extract_json(result)
    if not parsed:
        log(f"WARNING: could not parse JSON response for '{key}': {result[:200]!r}")
        return default
    return parsed.get(key, default)


def extract_tags(text):
    existing_tags = ", ".join(collect_existing_tags()[:300])
    prompt = f"""You are a tagging assistant for a personal knowledge base (Obsidian). Read the TEXT_CONTENT below and produce tags that will be used to browse and filter saved pages later.

Rules:
- Return 3-5 tags; fewer is fine if the content doesn't warrant more.
- Tags must be broad, reusable categories (e.g. "rust", "algorithms", "self-hosting", "career"), not one-off phrases specific to only this page.
- Prefer tags likely to be reused across other saved pages over hyper-specific ones.
- Before writing a tag, check EXISTING_TAGS for one that already covers the same concept and reuse its exact spelling instead of creating a near-duplicate (e.g. don't add "ray-marching" if "raymarching" is already there, or "csharp" if "c-sharp" is already there). Only mint a tag absent from EXISTING_TAGS when none of them reasonably fit.
- All tags in English, lowercase; multi-word tags are separated by a dash (e.g. "machine-learning").
- Ignore boilerplate: cookie banners, privacy notices, navigation menus, ads.
- Do not use the page title or domain name verbatim as a tag.
- If nothing warrants a tag, return an empty array.

<EXISTING_TAGS>
{existing_tags}
</EXISTING_TAGS>

<TEXT_CONTENT>
{text}
</TEXT_CONTENT>

Respond with ONLY valid JSON: {{"tags": ["tag-one", "tag-two"]}}"""
    tags = call(prompt, "tags", [])
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def extract_summary(text, require_content: bool = True):
    """Raises ContentFreeError when the model reports there's nothing real
    to summarize (a bare image/file reference, a login wall, an error page,
    navigation furniture). Better to fail the save than file something
    admittedly content-free into Areas/.

    The listing rules draw a line a blunter "any page that is a list of
    links" wording got wrong: it dropped a Goodreads series page as a mere
    index. A tag archive is furniture because each post it links is worth
    saving on its own; a series/bibliography/DLC listing is the artifact
    itself.

    require_content=False (allow_thin) keeps a page judged content-free
    anyway - a site's front page bookmarked as an entry point. A model that
    returned has_content false often leaves "summary" empty too, so that
    path re-asks without the has_content framing."""
    prompt = f"""Write a summary of the TEXT_CONTENT below for someone deciding whether to revisit this saved page later.

Rules:
- 3-4 sentences, in English.
- State what the content actually covers - do not open with "This article..." or "This page discusses...".
- Ignore boilerplate: cookie banners, privacy notices, navigation menus, ads.
- If it's a tutorial/how-to, say what it teaches. If it's a tool/reference, say what problem it solves.
- Set "has_content" to false if TEXT_CONTENT has no real substance to summarize - e.g. it's just a bare image/file reference, a login wall, an error page, or empty/near-empty text. Set it true otherwise.
- Also set "has_content" to false when TEXT_CONTENT is a site's own navigation furniture rather than content: a tag or category archive, a "posts tagged X" listing, a blog or news archive index, a paginated feed of recent posts, a search results page - any listing generated to route readers to articles that each stand on their own as a page. The linked articles get saved separately; the listing that points at them is not worth its own entry. That holds even when the listing carries a per-post excerpt or date, and even when every post on it is by the same author.
- The exception is a listing whose subject is the collection itself: a catalogue of one specific named series, work, or product line, where the enumeration and its per-item details are the reason to keep the page - a book series' volume list, an author's bibliography, a game's DLC or expansion list, a curated directory of tools. The individual entries there are not pages of this site that would be saved on their own, so the list is the content: set "has_content" to true and summarize what the collection covers.

<TEXT_CONTENT>
{text}
</TEXT_CONTENT>

Respond with ONLY valid JSON: {{"summary": "...", "has_content": true/false}}"""
    parsed = call_json(prompt)
    if not parsed:
        log("WARNING: could not parse JSON response for summary")
        return ""
    if parsed.get("has_content") is False:
        if require_content:
            raise ContentFreeError("page has no real content to summarize "
                                   "(bare file/image reference, login wall, error page, etc.)")
        log("NOTE: model judged this page content-free - keeping it anyway (allow thin)")
        return parsed.get("summary") or summarize_plain(text)
    return parsed.get("summary", "")


def summarize_plain(text):
    """Fallback for allow_thin: summarize whatever is there, with no
    content-worthiness judgement and so no way to come back empty-handed."""
    prompt = f"""Write a summary of the TEXT_CONTENT below for someone deciding whether to revisit this saved page later.

Rules:
- 3-4 sentences, in English.
- State what the content actually covers - do not open with "This article..." or "This page discusses...".
- Ignore boilerplate: cookie banners, privacy notices, navigation menus, ads.
- If it's mainly a listing, index, or feed of links, say what kind of material it routes to and how it is organised.
- Summarize whatever is present; never refuse or return an empty string.

<TEXT_CONTENT>
{text}
</TEXT_CONTENT>

Respond with ONLY valid JSON: {{"summary": "..."}}"""
    return call(prompt, "summary", "")


def extract_title(text):
    """Used whenever the document's own metadata can't be trusted to give a
    distinctive per-page title: PDFs whose embedded /Title is blank (common
    for LaTeX output), and pages whose <title> is static and site-wide
    (iquilezles.org: every article would collide on one filename)."""
    prompt = f"""Identify the title of the document in TEXT_CONTENT below (it's the extracted text of a page or document - could be a PDF's first pages or a web article).

Rules:
- Return the document's actual title, as it appears on the page (e.g. a paper's title, a report's title, an article's headline).
- Do not include authors, dates, page headers/footers, journal/site names, or site-wide taglines.
- If no clear title is present, return an empty string.

<TEXT_CONTENT>
{text[:3000]}
</TEXT_CONTENT>

Respond with ONLY valid JSON: {{"title": "..."}}"""
    title = call(prompt, "title", "")
    return title if isinstance(title, str) else ""


def extract_area(tags, summary):
    current_tree = tree_to_string(settings.areas_dir)

    prompt = f"""You are organizing a saved page into an existing knowledge-base folder structure.

Given TAGS and SUMMARY for a document, and a TREE of existing categories, choose where it belongs.

Rules:
- A leaf file is meant to collect every save about the same specific subject over time, not just one page - identify the actual subject first, then check TREE for an existing leaf about that exact subject and reuse it if one exists. Only mint a new leaf when no existing one is even loosely about the same subject.
- The "subject" is the specific thing the page is actually about (a tool, a library, an engine's tutorials, a game, a technique) - not the broad parent category it happens to sit under. Two pages about different specific things (e.g. two different board games, two different self-hosted apps) each get their own leaf even if both would sit in the same directory; two pages about the *same* thing (e.g. two tutorials on the same game engine, two guides for the same tool) share one leaf.
- If the directory this leaf would naturally sit in already contains more than about 15 other leaf files (count entries at that level in TREE), don't drop another one straight into it - nest it under a narrower subfolder that groups related subjects within that directory instead (e.g. "graphics/raymarching/sdf-smooth-minimum.md" rather than a 148th file directly under "graphics/"). Check TREE for an existing subfolder that already covers this narrower grouping and reuse it, same reuse-first principle as for leaf files, before inventing a new one.
- Depth: 2-4 levels, where the final segment is a markdown file (ends in .md) and everything before it is a directory.
- The leaf filename must never be identical to its own containing directory's name (e.g. "productivity/productivity.md" or "hobbies/hobbies.md" are invalid - pick a name describing the actual subject instead, like "productivity/browser-reading-tools.md"). A vault plugin auto-manages a folder-note that happens to share its folder's name as a generated index, so a leaf reusing that exact name gets its content silently overwritten/mixed with that index.
- Respond with a path only (e.g. "programming/rust/error-handling.md"), never a tree drawing.

<TAGS>
{tags}
</TAGS>

<SUMMARY>
{summary}
</SUMMARY>

<TREE>
{current_tree}
</TREE>

Respond with ONLY valid JSON: {{"path": "..."}}"""
    path = call(prompt, "path", "")
    return path if isinstance(path, str) else ""


def detect_git_repo_prompt(url: str, title: str, text: str) -> dict:
    prompt = f"""Determine whether the page below is the homepage of a single git source code repository (as hosted by GitHub, GitLab, Gitea, Forgejo, sourcehut, or similar) - NOT a user/org profile listing multiple repos, a search results page, a blog post that merely links to a repo, or unrelated content.

<URL>
{url}
</URL>

<TITLE>
{title}
</TITLE>

<TEXT_CONTENT>
{text}
</TEXT_CONTENT>

If it is a single repo's homepage, identify its owner (user/organization) and repo name exactly as shown on the page.

Respond with ONLY valid JSON: {{"is_repo": true/false, "owner": "...", "repo": "..."}}"""
    return call_json(prompt)
