"""Tests for :mod:`apps.common.markdown`.

Covers paragraph reflow behaviour (no ``nl2br`` — a single newline must
fold into a space within a paragraph, not a ``<br>``), GitHub-style
task-list rendering, and the bleach allowlist / ``_attr_filter``
boundary that keeps the markdown rendering XSS-safe.
"""

from apps.common.markdown import render_markdown


class TestParagraphReflow:
    """Single newlines must not produce ``<br>`` inside a paragraph."""

    def test_single_newline_folds_to_space(self):
        """A single newline mid-paragraph renders as plain reflow, not <br>.

        This is the explicit anti-regression for the removed ``nl2br``
        extension: editors wrap source text at ~80 cols and we must
        render it as one continuous paragraph, not a bunch of <br>
        breaks (which previously made descriptions visually narrow).
        """
        src = "This is a long sentence wrapped\nat eighty characters in the source."
        html = render_markdown(src)
        assert "<br" not in html
        assert "<p>This is a long sentence wrapped\nat eighty characters in the source.</p>" in html or (
            "<p>This is a long sentence wrapped at eighty characters in the source.</p>" in html
        )

    def test_double_newline_makes_new_paragraph(self):
        """A blank line separates paragraphs."""
        src = "First paragraph.\n\nSecond paragraph."
        html = render_markdown(src)
        assert "<p>First paragraph.</p>" in html
        assert "<p>Second paragraph.</p>" in html

    def test_two_trailing_spaces_force_hard_break(self):
        """CommonMark hard break (two spaces + newline) still renders as <br>."""
        src = "Line one.  \nLine two."
        html = render_markdown(src)
        assert "<br" in html


class TestHighlight:
    """``pymdownx.mark`` turns ``==text==`` into a ``<mark>`` element.

    Matches the markdown TipTap's Highlight extension serializes to,
    so a yellow-highlighted span in the editor survives the round-trip
    through the server render. Bleach's ALLOWED_TAGS must include
    ``mark`` or it'd be stripped silently.
    """

    def test_double_equals_renders_mark(self):
        html = render_markdown("normal ==yellow== normal")
        assert "<mark>yellow</mark>" in html

    def test_mark_text_content_survives_even_if_tag_stripped(self):
        # Defensive check: even if a future bleach config drops mark,
        # the inner text shouldn't disappear.
        html = render_markdown("normal ==still here== normal")
        assert "still here" in html


class TestTaskList:
    """``pymdownx.tasklist`` renders GitHub-style checkboxes."""

    def test_unchecked_task_renders_checkbox(self):
        html = render_markdown("- [ ] todo item")
        assert "<input" in html
        assert 'type="checkbox"' in html
        assert "checked" not in html

    def test_checked_task_renders_checked_checkbox(self):
        html = render_markdown("- [x] done item")
        assert "<input" in html
        assert 'type="checkbox"' in html
        assert "checked" in html


class TestSanitization:
    """Bleach + ``_attr_filter`` enforce the XSS allowlist."""

    def test_script_tag_is_stripped(self):
        html = render_markdown("hello <script>alert(1)</script> world")
        assert "<script" not in html
        assert "alert(1)" in html  # text content survives, only tag stripped

    def test_input_text_is_stripped_but_checkbox_kept(self):
        """Only ``<input type="checkbox">`` is allowed; other inputs go."""
        html = render_markdown('foo <input type="text" name="x"> bar\n\n- [ ] task')
        # the bogus <input type="text"> is stripped
        assert 'type="text"' not in html
        # the legit task-list checkbox survives
        assert 'type="checkbox"' in html

    def test_javascript_href_is_stripped(self):
        html = render_markdown("[click](javascript:alert(1))")
        assert "javascript:" not in html


class TestMentions:
    """``mention:`` / ``task:`` link tokens become chips."""

    def test_user_mention_becomes_chip(self):
        html = render_markdown("hey [@vox](mention:5) ping")
        assert '<span class="acta-mention" data-user-id="5">@vox</span>' in html

    def test_task_mention_becomes_internal_chip_link(self):
        html = render_markdown("see [ACTA-128](task:9)")
        assert 'class="acta-task-mention"' in html
        assert 'data-task-id="9"' in html
        assert 'href="/projects/ACTA/128/"' in html
        assert "_blank" not in html  # internal link stays in the same tab

    def test_task_mention_label_with_title(self):
        """Label carries "SLUG Title"; URL is derived from the leading slug."""
        html = render_markdown("see [ACTA-128 Fix the thing](task:9)")
        assert 'href="/projects/ACTA/128/"' in html
        assert "Fix the thing" in html

    def test_mention_span_cannot_carry_dangerous_attrs(self):
        """A forged chip is cosmetic-only (notifications derive from the
        membership-validated token parse), but it must never smuggle an
        event handler or non-numeric id through bleach."""
        html = render_markdown('<span class="acta-mention" data-user-id="1" onclick="x()">z</span>')
        assert "onclick" not in html
        html2 = render_markdown('<span class="acta-mention" data-user-id="1;alert(1)">z</span>')
        assert "alert(1)" not in html2

    def test_external_link_still_opens_new_tab(self):
        html = render_markdown("[g](https://g.com)")
        assert 'target="_blank"' in html


class TestEdgeCases:
    """Trivial inputs."""

    def test_none_returns_empty_string(self):
        assert render_markdown(None) == ""

    def test_empty_string_returns_empty_string(self):
        assert render_markdown("") == ""
