from saver.urls import classify, dedupe_key, is_git_author_url, normalize_url, parse_git_repo_url


def test_normalize_collapses_youtube_forms():
    assert normalize_url("https://youtu.be/abc123?si=x") == "youtube.com/watch?v=abc123"
    assert normalize_url("https://www.youtube.com/shorts/abc123") == "youtube.com/watch?v=abc123"
    assert normalize_url("https://www.example.com/a/b/") == "example.com/a/b"
    assert dedupe_key("https://example.com/x") == "url:example.com/x"


def test_git_urls():
    assert parse_git_repo_url("https://github.com/SebLague/Ray-Tracing") == ("github.com", "SebLague", "Ray-Tracing")
    assert parse_git_repo_url("https://git.example.net/me/tool.git") == ("git.example.net", "me", "tool")
    assert parse_git_repo_url("https://gist.github.com/0123456789abcdef0123") == (
        "gist.github.com", "anonymous", "0123456789abcdef0123")
    assert parse_git_repo_url("https://gist.github.com/someone") == (None, None, None)
    assert is_git_author_url("https://github.com/torvalds")


def test_classify():
    assert classify("https://www.youtube.com/@SebastianLague") == "youtube-channel"
    assert classify("https://youtu.be/abc") == "youtube"
    assert classify("https://media.ccc.de/v/39c3-talk") == "video"
    assert classify("https://github.com/torvalds") == "git-author"
    assert classify("https://github.com/torvalds/linux") == "git"
    assert classify("https://arxiv.org/x.pdf") == "pdf"
    assert classify("https://example.com/post") == "page"


def test_git_owner_and_repo_are_case_insensitive():
    assert normalize_url("https://github.com/SigNoz/signoz") == "github.com/signoz/signoz"
    assert dedupe_key("https://github.com/SigNoz/SigNoz.git") == dedupe_key("https://www.github.com/signoz/signoz/")
    assert normalize_url("https://codeberg.org/Forgejo/Forgejo") == "codeberg.org/forgejo/forgejo"
    # paths inside a repo stay case-sensitive, and other hosts are untouched
    assert normalize_url("https://github.com/Owner/Repo/blob/main/README.md") == "github.com/owner/repo/blob/main/README.md"
    assert normalize_url("https://example.com/Some/Page") == "example.com/Some/Page"
