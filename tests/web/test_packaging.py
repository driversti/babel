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
