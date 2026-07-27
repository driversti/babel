import pathlib
import subprocess

from babel.web.app import STATIC_DIR, TEMPLATE_DIR

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_every_template_is_tracked_by_git():
    """.gitignore line 39 is `*.html`.

    Without an explicit negation this passes locally and fails only on the
    deploy host, because the tests read the working tree while the container
    gets what git actually shipped.
    """
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "src/babel/web/templates"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout.split()
    )
    on_disk = {
        str(p.relative_to(REPO)) for p in TEMPLATE_DIR.glob("*.html")
    }
    assert on_disk
    assert on_disk <= tracked


def test_static_assets_are_tracked_by_git():
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "src/babel/web/static"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout.split()
    )
    on_disk = {str(p.relative_to(REPO)) for p in STATIC_DIR.glob("*")}
    assert on_disk
    assert on_disk <= tracked


def test_no_template_uses_the_safe_filter():
    """render_body returns Markup, so nothing needs |safe — and body_raw is
    untrusted, so nothing may have it. Keeping the count at zero is cheaper to
    enforce than auditing each use."""
    for path in (pathlib.Path(__file__).parents[2]
                 / "src/babel/web/templates").rglob("*.html"):
        assert "|safe" not in path.read_text(), f"{path.name} uses |safe"
        assert "| safe" not in path.read_text(), f"{path.name} uses | safe"
