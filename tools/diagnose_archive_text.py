#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Summarize an existing DB archive's text formats without exposing content.

Python 2.6+ standard library only. Reads files; never modifies the archive.
Reports aggregate counts only, not IDs, titles, snippets or file paths.
"""
from __future__ import print_function

import json
import os
import re
import sys
from optparse import OptionParser

MAX_FILE_BYTES = 64 * 1024 * 1024


def inspect_archive(folder):
    documents = os.path.join(folder, "documents")
    if not os.path.isdir(documents):
        raise ValueError("Expected an extracted DB archive containing documents/.")
    keys = ("document_html_files", "html_valid_utf8", "html_invalid_utf8",
            "html_has_utf8_meta", "html_has_utf8_bom", "html_known_korean_headers",
            "database_json_files", "json_valid_utf8", "json_parse_errors", "contexts",
            "context_has_hangul", "context_has_replacement_character", "context_has_html_tags",
            "context_has_escaped_html", "context_has_percent_escapes",
            "context_has_literal_unicode_escapes", "context_has_php_serialized_prefix",
            "context_has_base64_only_characters", "context_has_latin1_mojibake_patterns",
            "context_empty_or_null", "files_unreadable_or_too_large")
    counts = dict((key, 0) for key in keys)
    for directory, dirs, files in os.walk(documents):
        for filename in files:
            if filename not in ("document.html", "database.json"):
                continue
            is_html = filename == "document.html"
            counts["document_html_files" if is_html else "database_json_files"] += 1
            try:
                with open(os.path.join(directory, filename), "rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise IOError("too large")
            except (IOError, OSError):
                counts["files_unreadable_or_too_large"] += 1
                continue
            try:
                value = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                counts["html_invalid_utf8" if is_html else "json_parse_errors"] += 1
                continue
            if is_html:
                counts["html_valid_utf8"] += 1
                counts["html_has_utf8_bom"] += int(data.startswith(b"\xef\xbb\xbf"))
                counts["html_has_utf8_meta"] += int(bool(re.search(r"charset\s*=\s*[\"']?utf-8", value[:4096], re.I)))
                counts["html_known_korean_headers"] += int(u"저장된 결재선 기록" in value and u"브라우저 인쇄" in value)
                continue
            counts["json_valid_utf8"] += 1
            try:
                record = json.loads(value)
                context = record["document"]["context"]
            except (ValueError, KeyError, TypeError):
                counts["json_parse_errors"] += 1
                continue
            counts["contexts"] += 1
            if not context:
                counts["context_empty_or_null"] += 1
                continue
            if not isinstance(context, type(u"")):
                counts["json_parse_errors"] += 1
                continue
            patterns = (
                ("context_has_hangul", u"[\uac00-\ud7a3]"),
                ("context_has_replacement_character", u"\ufffd"),
                ("context_has_html_tags", r"<(?:p|div|table|span|html|body|br|font)(?:\s|/?>)"),
                ("context_has_escaped_html", r"&(?:amp;)?lt;(?:p|div|table|span|html|body|br|font)(?:\s|&|/|>)"),
                ("context_has_percent_escapes", r"%(?:3c|3e|[8-f][0-9a-f])"),
                ("context_has_literal_unicode_escapes", r"\\u[0-9a-f]{4}"),
                ("context_has_php_serialized_prefix", r'^\s*(?:a:\d+:\{|s:\d+:\")'),
                ("context_has_base64_only_characters", r"^[A-Za-z0-9+/]{40,}={0,2}$"),
                ("context_has_latin1_mojibake_patterns", u"[\u00c2\u00c3\u00eb\u00ec][\u0080-\u00bf]"),
            )
            for key, pattern in patterns:
                counts[key] += int(bool(re.search(pattern, context, re.I)))
    return {"format": "SEGIO_TEXT_DIAGNOSTIC_V1", "counts": counts,
            "note": "Pattern counts are clues, not a confirmed cause. No content or file names included."}


def main():
    parser = OptionParser(usage="%prog /path/to/extracted/archive")
    options, args = parser.parse_args()
    if len(args) != 1:
        parser.error("Specify the extracted archive directory, not the ZIP file.")
    try:
        print(json.dumps(inspect_archive(args[0]), sort_keys=True, indent=2))
        return 0
    except (ValueError, IOError, OSError):
        print("Could not inspect the archive. Verify its directory path; contents omitted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
