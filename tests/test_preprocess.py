from preprocess import clean_text, is_valid

def test_clean_text_html_unescape():
    assert clean_text("&lt;p&gt;hello&lt;/p&gt;") == "<p>hello</p>"
    assert clean_text("A &amp; B") == "A & B"

def test_clean_text_url_replacement():
    assert clean_text("Check https://google.com for info") == "Check http for info"
    assert clean_text("Visit www.example.org/path?query=1") == "Visit http"

def test_clean_text_username_replacement():
    assert clean_text("Hello @alice and @bob") == "Hello @user and @user"

def test_clean_text_whitespace_collapse():
    assert clean_text("   Too   many   spaces   ") == "Too many spaces"
    assert clean_text("Line1\nLine2\tTab") == "Line1 Line2 Tab"

def test_is_valid():
    assert is_valid("abc") is True
    assert is_valid("ab") is False
    assert is_valid("") is False
    assert is_valid("   ") is True  # spaces are counted before cleaning, though after cleaning it might be different. Let's make sure it checks length >= 3.
