#!/usr/bin/env python3
from pathlib import Path

path = Path(__file__).resolve().with_name("apply_post_modularization_fixes.py")
text = path.read_text(encoding="utf-8")

helper_anchor = '''def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)
'''
helper_replacement = helper_anchor + '''\n\ndef replace_in_function(text, function_name, old, new, label):
    marker = f"def {function_name}("
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"{label}: function {function_name!r} not found")
    next_function = text.find("\ndef ", start + len(marker))
    end = len(text) if next_function < 0 else next_function + 1
    function_text = text[start:end]
    count = function_text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{label}: expected one match in {function_name}, got {count}"
        )
    return text[:start] + function_text.replace(old, new, 1) + text[end:]
'''
if "def replace_in_function(" not in text:
    if text.count(helper_anchor) != 1:
        raise RuntimeError("replace helper anchor is not unique")
    text = text.replace(helper_anchor, helper_replacement, 1)

old_duck_call = '''text = replace_once(text, old_duck_body, '            combined = normalize_mail_body(detail)\\n', "DuckMail body normalization")'''
new_duck_call = '''text = replace_in_function(
    text,
    "duckmail_get_oai_code",
    old_duck_body,
    '            combined = normalize_mail_body(detail)\\n',
    "DuckMail body normalization",
)'''
if old_duck_call in text:
    text = text.replace(old_duck_call, new_duck_call, 1)
elif new_duck_call not in text:
    raise RuntimeError("DuckMail replacement call anchor not found")

old_yyds_call = '''text = replace_once(text, old_yyds_body, '            combined = normalize_mail_body(detail)\\n', "YYDS body normalization")'''
new_yyds_call = '''text = replace_in_function(
    text,
    "yyds_get_oai_code",
    old_yyds_body,
    '            combined = normalize_mail_body(detail)\\n',
    "YYDS body normalization",
)'''
if old_yyds_call in text:
    text = text.replace(old_yyds_call, new_yyds_call, 1)
elif new_yyds_call not in text:
    raise RuntimeError("YYDS replacement call anchor not found")

# Guard against reintroducing ambiguous full-file replacements for provider bodies.
for forbidden in (
    "replace_once(text, old_duck_body",
    "replace_once(text, old_yyds_body",
):
    if forbidden in text:
        raise RuntimeError(f"ambiguous provider replacement remains: {forbidden}")

path.write_text(text, encoding="utf-8")
print("post-modularization patcher repaired with function-scoped anchors")
