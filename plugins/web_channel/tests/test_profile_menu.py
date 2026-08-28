"""Header Profile menu + /profile + /projects (render smoke). The app shell's account
surface: Admin console is reachable ONLY through the Profile menu (never the primary
nav), and only for a principal with a kernel-derived admin cap. Driven through the app
with the Core client faked (no live kernel)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from web_channel import auth, core_client, localusers
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def _principal(is_admin: bool, **kw) -> auth.Principal:
    return auth.Principal(
        viewer="alice",
        scopes=["public", "src:1"],
        groups=["staff"],
        is_admin=is_admin,
        display="Alice",
        sub="alice",
        provider="local",
        sid="sess-1",
        **kw,
    )


def _as(monkeypatch, principal: auth.Principal | None) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(web, "_current_principal", lambda request: principal)


def _p0(monkeypatch) -> None:
    """P0 no-auth mode: no OIDC, no local users, no session — the fixed operator."""
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    monkeypatch.setattr(localusers, "has_any", lambda: False)
    monkeypatch.setattr(web, "_current_principal", lambda request: None)


def _sidebar(html: str) -> str:
    return html.split("<aside", 1)[1].split("</aside>", 1)[0]


def _menu(html: str) -> str:
    """The popover panel: from its id to the end of its `role=menu` list."""
    m = re.search(r'id="profile-menu".*?role="menu".*?</div>', html, re.S)
    assert m, "profile popover missing"
    return m.group(0)


@pytest.mark.parametrize("path", ["/", "/dashboard", "/profile", "/projects"])
@pytest.mark.parametrize("is_admin", [False, True])
def test_shell_pages_render_profile_menu(monkeypatch, path: str, is_admin: bool) -> None:
    _as(monkeypatch, _principal(is_admin))
    r = client.get(path)
    assert r.status_code == 200, (path, r.text[:200])
    html = r.text
    # the trigger is a real, tabbable <button> that names itself and its popover
    m = re.search(r"<button class=\"profile-menu-button\"[^>]*>", html)
    assert m, "profile trigger missing"
    trigger = m.group(0)
    for attr in (
        'type="button"',
        'aria-label="Profile"',
        'aria-haspopup="menu"',
        'aria-controls="profile-menu"',
        'aria-expanded="false"',  # closed before Alpine boots
    ):
        assert attr in trigger, attr
    assert "tabindex" not in trigger  # natural tab order
    menu = _menu(html)
    assert 'aria-hidden="true"' in menu  # closed before Alpine boots (no flash)
    # the identity block is NOT inside role=menu (a menu owns only items/separators)
    head, items = menu.split('role="menu"', 1)
    assert "Alice" in head and "staff" in head and "2 scopes" in head
    assert 'aria-labelledby="profile-menu-trigger"' in items
    # every action is a real link
    assert '<a role="menuitem" href="/profile">Profile</a>' in items
    assert '<a role="menuitem" href="/projects">Projects</a>' in items
    assert '<a role="menuitem" href="/logout">Logout</a>' in items
    assert ("Admin console" in items) is is_admin
    # the primary nav never carries Admin console (moved to Profile), for anyone
    assert "Admin console" not in _sidebar(html)
    assert "/admin" not in _sidebar(html)


def test_admin_console_shell_still_renders_for_admin(monkeypatch) -> None:
    _as(monkeypatch, _principal(True))
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Admin console" in r.text


def test_admin_console_forbidden_for_member(monkeypatch) -> None:
    _as(monkeypatch, _principal(False))
    assert client.get("/admin").status_code == 403


def test_menu_badges_follow_principal_flags(monkeypatch) -> None:
    _as(monkeypatch, _principal(True, is_elevated=True, external=True))
    menu = _menu(client.get("/").text)
    assert ">elevated<" in menu and ">guest<" in menu
    _as(monkeypatch, _principal(False))
    menu = _menu(client.get("/").text)
    assert ">elevated<" not in menu and ">guest<" not in menu


def test_profile_page_shows_identity_and_logout(monkeypatch) -> None:
    _as(monkeypatch, _principal(False))
    r = client.get("/profile")
    assert r.status_code == 200
    main = r.text.split("<main", 1)[1]
    assert '<h1 class="page-title">Profile</h1>' in main
    assert 'href="/logout"' in main
    assert "staff" in main and "src:1" in main
    assert re.search(r"<dd>\s*member", main)  # a non-admin's access row
    assert 'href="/admin"' not in main
    _as(
        monkeypatch, _principal(True, is_elevated=True, elevation_expires_at="2026-08-28T10:00:00Z")
    )
    main = client.get("/profile").text.split("<main", 1)[1]
    assert 'href="/admin"' in main
    assert '<time datetime="2026-08-28T10:00:00Z">10:00:00</time>' in main


def test_profile_and_projects_redirect_when_auth_is_on_and_no_session(monkeypatch) -> None:
    _as(monkeypatch, None)
    for path in ("/profile", "/projects"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/login", path


def test_p0_no_auth_mode_has_no_menu_and_honest_pages(monkeypatch) -> None:
    _p0(monkeypatch)
    home = client.get("/")
    assert home.status_code == 200 and "profile-menu-button" not in home.text
    r = client.get("/profile")
    assert r.status_code == 200 and "No signed-in identity" in r.text
    r = client.get("/projects")
    assert r.status_code == 200 and "Not available yet" in r.text
    assert 'href="/admin' not in r.text


def test_projects_lists_only_what_the_kernel_says_is_visible(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as(monkeypatch, _principal(False))
    seen: dict = {}

    async def fake(assertion: str, mine_only: bool = False):
        seen["assertion"], seen["mine_only"] = assertion, mine_only
        return core_pb2.ListProjectsResponse(
            status=core_pb2.CALL_OK,
            projects=[
                core_pb2.ProjectView(
                    id="p1", name="Docs", visibility="public", source_count=2, member_count=3
                ),
                core_pb2.ProjectView(
                    id="p2", name="Team X", visibility="shared", description="ours"
                ),
            ],
        )

    monkeypatch.setattr(core_client, "list_projects", fake)
    r = client.get("/projects")
    assert r.status_code == 200
    assert seen["assertion"] and seen["mine_only"] is True  # never the admin all-view
    assert "Docs" in r.text and "Team X" in r.text and "ours" in r.text
    assert 'href="/admin/projects' not in r.text  # a member gets no admin deep links
    assert ">public<" in r.text and ">shared<" in r.text


def test_projects_admin_gets_all_projects_link_and_detail_links(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as(monkeypatch, _principal(True))

    async def fake(assertion: str, mine_only: bool = False):
        assert mine_only is True  # an admin's own view too — /admin/projects is the all-view
        return core_pb2.ListProjectsResponse(
            status=core_pb2.CALL_OK,
            projects=[core_pb2.ProjectView(id="p1", name="Docs", visibility="public")],
        )

    monkeypatch.setattr(core_client, "list_projects", fake)
    html = client.get("/projects").text
    assert 'href="/admin/projects"' in html
    assert 'href="/admin/projects/p1"' in html


def test_projects_honest_states(monkeypatch) -> None:
    # unsigned session: no kernel call, an honest note
    monkeypatch.delenv("SWARM_ACTOR_SECRET", raising=False)
    _as(monkeypatch, _principal(False))
    called = {"n": 0}

    async def fake(assertion: str, mine_only: bool = False):
        called["n"] += 1
        raise AssertionError("must not be called without an assertion")

    monkeypatch.setattr(core_client, "list_projects", fake)
    r = client.get("/projects")
    assert r.status_code == 200 and "Not available yet" in r.text and called["n"] == 0

    # kernel down: unavailable, not a 500
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)

    async def boom(assertion: str, mine_only: bool = False):
        raise RuntimeError("down")

    monkeypatch.setattr(core_client, "list_projects", boom)
    r = client.get("/projects")
    assert r.status_code == 200 and "Kernel unavailable" in r.text

    # empty: legitimate empty state
    async def none(assertion: str, mine_only: bool = False):
        return core_pb2.ListProjectsResponse(status=core_pb2.CALL_OK, projects=[])

    monkeypatch.setattr(core_client, "list_projects", none)
    r = client.get("/projects")
    assert r.status_code == 200 and "No projects yet" in r.text
