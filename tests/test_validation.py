"""Anti-hallucination link validation + output cleaning — all offline."""
from llmstxt_generator.builder import (
    _drop_empty_sections,
    _strip_fences,
    _structure_stats,
    _validate_and_clean,
)


ALLOWED = {
    "https://x.com/",
    "https://x.com/pricing",
    "https://x.com/docs",
}


def test_drops_invented_url_in_strict_mode():
    text = (
        "# X\n> A company.\n\n## Product\n"
        "- [Pricing](https://x.com/pricing): plans.\n"
        "- [Made up](https://x.com/totally-invented): nope.\n"
    )
    cleaned, stats = _validate_and_clean(text, ALLOWED, "x.com", strict=True)
    assert "x.com/pricing" in cleaned
    assert "totally-invented" not in cleaned
    assert stats["dropped_invented_links"] == 1
    assert stats["link_count"] == 1


def test_loose_mode_allows_onhost_unknown():
    text = (
        "# X\n> A company.\n\n## Product\n"
        "- [Guess](https://x.com/some-real-page): on host.\n"
        "- [Offsite](https://evil.com/x): off host.\n"
    )
    cleaned, stats = _validate_and_clean(text, ALLOWED, "x.com", strict=False)
    assert "x.com/some-real-page" in cleaned     # on-host allowed when loose
    assert "evil.com" not in cleaned             # off-host always dropped
    assert stats["dropped_offhost_links"] == 1


def test_dedupes_repeated_urls():
    text = (
        "# X\n> A company.\n\n## A\n"
        "- [One](https://x.com/pricing): a.\n"
        "- [Two](https://x.com/pricing): dup.\n"
    )
    cleaned, stats = _validate_and_clean(text, ALLOWED, "x.com", strict=True)
    assert cleaned.count("x.com/pricing") == 1
    assert stats["dropped_duplicate_links"] == 1


def test_drops_empty_section():
    text = (
        "# X\n\n## Real\n- [P](https://x.com/pricing): a.\n\n"
        "## Empty\n- [Bad](https://evil.com/x): gone.\n"
    )
    cleaned, _ = _validate_and_clean(text, ALLOWED, "x.com", strict=True)
    assert "## Real" in cleaned
    assert "## Empty" not in cleaned


def test_strip_fences():
    assert _strip_fences("```markdown\n# X\n```").startswith("# X")
    assert _strip_fences("Here you go:\n\n# X\n> hi").startswith("# X")


def test_structure_stats():
    text = "# X\n> summary\n\n## Product\n- a\n\n## Optional\n- b\n"
    stats = _structure_stats(text)
    assert stats["has_h1"]
    assert stats["has_blockquote"]
    assert stats["has_optional_section"]
    assert stats["section_count"] == 2


def test_drop_empty_sections_keeps_populated():
    text = "## Keep\n- [x](https://x.com/docs): d\n"
    assert "## Keep" in _drop_empty_sections(text)
