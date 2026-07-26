import pathlib

from babel.crawler.parser import parse_article, parse_comments

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_extracts_every_comment():
    comments = parse_comments(load("article_with_comments.html"))
    assert len(comments) == 41


def test_ids_are_unique_and_positions_contiguous():
    comments = parse_comments(load("article_with_comments.html"))
    assert len({c.id for c in comments}) == len(comments)
    assert [c.position for c in comments] == list(range(len(comments)))


def test_depth_comes_from_indentation():
    comments = parse_comments(load("article_with_comments.html"))
    assert comments[0].depth == 0
    assert max(c.depth for c in comments) == 2
    assert all(c.depth >= 0 for c in comments)


def test_author_is_captured():
    comments = parse_comments(load("article_with_comments.html"))
    first = comments[0]
    assert first.author_id and first.author_id > 0
    assert first.author_name


def test_timestamps_are_game_local():
    comments = parse_comments(load("article_with_comments.html"))
    stamped = [c for c in comments if c.posted_at]
    assert stamped
    assert all(c.posted_at.tzinfo is not None for c in stamped)


def test_the_real_removed_comment_in_the_fixture_has_a_null_body():
    # This fixture contains exactly one genuinely removed comment. The synthetic
    # test below pins the parsing rule; this one pins it against real markup.
    comments = parse_comments(load("article_with_comments.html"))
    assert len([c for c in comments if c.body is None]) == 1


def test_an_article_with_a_single_comment_still_parses():
    comments = parse_comments(load("article_indonesia.html"))
    assert len(comments) == 1


def test_removed_comments_keep_their_slot_with_null_body():
    html = """
    <div id="comment1" class="commentWrapper"><div><div style="padding-left:0px;"><div>
      <div class="authorWrapper"><div class="details">
        <a title="ghost" href="/en/citizen/profile/42">ghost</a>
        <span>Day 6,819, 21:34</span>
        <p><i>[removed]</i></p>
      </div></div>
    </div></div></div>
    """
    comments = parse_comments(html)
    assert len(comments) == 1
    assert comments[0].body is None
    assert comments[0].author_id == 42


def test_parse_article_attaches_comments():
    article = parse_article(load("article_with_comments.html"), 2796950)
    assert len(article.comments) == 41
