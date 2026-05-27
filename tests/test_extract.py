from gcos.sandbox.extract import extract_python


def test_python_fence_extracted():
    text = "Sure!\n\n```python\nprint('hi')\n```\n\nThat's it."
    assert extract_python(text) == "print('hi')"


def test_py_alias():
    text = "```py\nx = 1\nprint(x)\n```"
    assert extract_python(text) == "x = 1\nprint(x)"


def test_unlabeled_fence():
    text = "```\nprint(2)\n```"
    assert extract_python(text) == "print(2)"


def test_python_preferred_over_unlabeled():
    text = "```\nfirst\n```\n```python\nsecond\n```"
    assert extract_python(text) == "second"


def test_no_fence_returns_none():
    assert extract_python("just plain text") is None
    assert extract_python("") is None
    assert extract_python(None) is None


def test_handles_trailing_newlines():
    text = "```python\nprint(1)\n\n\n```"
    assert extract_python(text) == "print(1)"
