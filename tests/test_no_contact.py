"""Definition of done: nothing in the radar codebase can post, comment, like or DM.

Static guard over src/lead_radar: the only HTTP writes allowed are to the model providers (OpenRouter,
TypeSafe) and outbound report delivery (Slack, Sheets). No social-platform write endpoints, no Browser Box
actions that type or click into a platform.
"""
import ast
import re
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "lead_radar"
FORBIDDEN = re.compile(
    r"/(?:comment|reply|like|react|follow|message|messages|dm|send|post)/(?:create|send|new)|"
    r"send_message|send_dm|post_comment|create_comment|like_post|\bdirect_message\b|/v1/.*/(?:send|publish)",
    re.I)


def _py_files():
    return [p for p in SRC.rglob("*.py")]


def test_no_social_write_endpoints():
    for path in _py_files():
        text = path.read_text()
        assert not FORBIDDEN.search(text), f"{path}: contact/write endpoint"


def test_http_writes_only_to_allowed_hosts():
    allowed = ("openrouter.ai", "api.typesafe.ai", "slack.com", "hooks.slack.com", "sheets.googleapis.com",
               "RADAR_APP_URL",  # the radar's own hosted dashboard (daily cron trigger)
               "DATABOX_URL",    # your own Data Box (job_board, enrich): a data tool, never a platform
               "BROWSERBOX_URL")  # your own Browser Box, `read` only (program lane): see the test below
    for path in _py_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("post", "put", "patch", "delete"):
                src = ast.get_source_segment(path.read_text(), node) or ""
                owner = ast.get_source_segment(path.read_text(), node.func.value) or ""
                if owner in ("app", "router"):  # HTTP route decorators in api.py, not outbound calls
                    continue
                module_text = path.read_text()
                assert any(h in module_text for h in allowed), f"{path}: outbound {node.func.attr} to unknown host: {src[:80]}"


def test_scrapecreators_client_is_get_only():
    text = (SRC / "scrapecreators.py").read_text()
    assert "self._http.get(" in text
    assert not re.search(r"self\._http\.(post|put|patch|delete)\(", text)


def test_job_boards_are_get_only():
    """Job boards are read, never written to: no applying, bidding or messaging employers."""
    for path in [SRC / "listings" / "http.py", *(SRC / "adapters").glob("*.py")]:
        text = path.read_text()
        assert not re.search(r"\.(post|put|patch|delete)\(", text), f"{path}: job-board write"


def test_repo_ships_only_lead_radar():
    """Definition of done covers the whole codebase: no other package, no contact workflow files."""
    root = SRC.parents[1]
    packages = [p.name for p in (root / "src").iterdir() if p.is_dir() and (p / "__init__.py").exists()]
    assert packages == ["lead_radar"]
    offenders = [p for p in root.rglob("*") if p.is_file() and ".venv" not in p.parts and ".git" not in p.parts
                 and re.search(r"(message|like|friend|comment|dm|post)_?(send|workflow)|bdr_facebook", p.name, re.I)]
    assert not offenders, offenders


def test_browser_box_is_read_only():
    """The program lane may only call Browser Box `read`: never act, task, computer or session."""
    text = (SRC / "programs" / "browserbox.py").read_text()
    assert 'TOOL = "read"' in text
    assert "/api/v1/{TOOL}" in text
    assert not re.search(r"/api/v1/(?:act|task|computer|session)\b|[\"'](?:act|task|computer|session)[\"']", text)
