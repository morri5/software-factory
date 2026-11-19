from factory.graph import fence_frontmatter


def test_frontmatter_becomes_a_yaml_block() -> None:
    doc = "---\ndate: 2026-11-19\nresearcher: factory\n---\n\n# Research\n"
    assert (
        fence_frontmatter(doc)
        == "```yaml\ndate: 2026-11-19\nresearcher: factory\n```\n\n# Research\n"
    )


def test_document_without_frontmatter_is_unchanged() -> None:
    assert fence_frontmatter("# Research\n---\n") == "# Research\n---\n"
