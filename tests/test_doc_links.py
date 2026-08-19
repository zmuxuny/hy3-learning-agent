import runpy
from pathlib import Path


CHECKER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_doc_links.py"
check_markdown_links = runpy.run_path(str(CHECKER_PATH))["check_markdown_links"]


def test_markdown_link_checker_accepts_local_external_and_anchor_links(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "target file.md").write_text("# Target\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "\n".join(
            [
                "[local](docs/target%20file.md#target)",
                "[external](https://example.com/missing)",
                "[anchor](#section)",
                "`[code](missing.md)`",
                "```markdown",
                "[fenced](missing.md)",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    assert check_markdown_links(tmp_path) == []


def test_markdown_link_checker_reports_missing_and_escaping_targets(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "[missing](docs/missing.md)\n[escape](../outside.md)\n",
        encoding="utf-8",
    )

    broken = check_markdown_links(tmp_path)

    assert [(issue.line, issue.target, issue.reason) for issue in broken] == [
        (1, "docs/missing.md", "target does not exist"),
        (2, "../outside.md", "escapes repository"),
    ]


def test_repository_markdown_relative_links_are_valid():
    repository_root = Path(__file__).resolve().parents[1]

    assert check_markdown_links(repository_root) == []
