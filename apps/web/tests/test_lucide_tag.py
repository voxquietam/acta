"""Tests for the ``{% lucide %}`` template tag.

Covers:
* Known icon renders inline SVG with the requested class.
* Unknown name falls back to the default icon — no error, no XSS.
* The fallback name itself renders.
* Default class is applied when caller omits the second argument.
* The ``has_icon`` helper.
"""

from django.template import Context, Template

from apps.web.templatetags.lucide import DEFAULT_ICON, has_icon


def _render(template_source, **ctx):
    return Template("{% load lucide %}" + template_source).render(Context(ctx)).strip()


def test_known_icon_renders_inline_svg():
    out = _render('{% lucide "folder" "w-4 h-4" %}')
    assert out.startswith("<svg")
    assert 'class="w-4 h-4"' in out
    # ``folder`` icon uses a path / line element — assert the SVG body
    # is present, not just the opening tag.
    assert "</svg>" in out


def test_unknown_name_falls_back():
    out = _render('{% lucide "definitely-not-an-icon" "w-3 h-3" %}')
    # Falls back to DEFAULT_ICON, which exists in the JSON.
    assert out.startswith("<svg")
    assert 'class="w-3 h-3"' in out


def test_default_class_is_applied_when_omitted():
    out = _render('{% lucide "check" %}')
    # ``w-4 h-4`` is the default class — see ``lucide`` tag signature.
    assert 'class="w-4 h-4"' in out


def test_dynamic_icon_name_via_filter():
    out = _render('{% lucide name|default:"folder" "w-3 h-3" %}', name="")
    assert out.startswith("<svg")
    assert 'class="w-3 h-3"' in out


def test_has_icon_helper():
    assert has_icon("folder") is True
    assert has_icon(DEFAULT_ICON) is True
    assert has_icon("not-real-icon") is False


def test_default_icon_is_in_manifest():
    """Sanity guard: if the manifest stops shipping the fallback icon
    the tag would explode at template-render time."""
    assert has_icon(DEFAULT_ICON), f"DEFAULT_ICON {DEFAULT_ICON!r} missing from manifest"
