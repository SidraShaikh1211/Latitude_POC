from app.llm.citation_verify import normalize, verify_substring


def test_normalize_collapses_whitespace():
    assert normalize("a  b\n c\t d") == "a b c d"


def test_normalize_strips_zero_width():
    assert normalize("hello​world") == "helloworld"


def test_verify_simple_substring():
    check = verify_substring("hello world", "say hello world today")
    assert check.found
    assert check.start_offset == 4
    assert check.end_offset == 4 + len("hello world")


def test_verify_normalized_match():
    # quote has extra whitespace, source has line break — should normalize and match
    src = "the quick\nbrown   fox jumps"
    check = verify_substring("quick brown fox", src)
    assert check.found


def test_verify_handles_zero_width():
    src = "page header​content here"
    check = verify_substring("page headercontent here", src)
    assert check.found


def test_verify_missing_substring():
    check = verify_substring("never appears", "totally different text")
    assert not check.found
    assert "no substring overlap" in (check.diagnostic or "")


def test_verify_partial_match_diagnostic():
    long_quote = "the quick brown fox jumps over the lazy dog and runs to the river quickly"
    src = "the quick brown fox jumps over the lazy dog WALKED home"
    check = verify_substring(long_quote, src)
    assert not check.found
    assert check.diagnostic is not None
    assert "matched" in check.diagnostic


def test_empty_quote_is_not_found():
    check = verify_substring("", "anything")
    assert not check.found
