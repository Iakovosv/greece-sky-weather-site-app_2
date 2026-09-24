"""How the astro card classifies a failed `import ephem`.

Deliberately separate from test_astro.py, whose module-level
`skipif(not _HAVE_EPHEM)` would skip every test here in precisely the install
this file exists to diagnose.

The bug being pinned: the card told a Windows user with no ephem installed that
"η βιβλιοθήκη ephem υπάρχει αλλά απέτυχε να φορτώσει" while printing
`ModuleNotFoundError: No module named 'ephem'` directly underneath. The two
sentences contradict each other, and the reader goes looking for a broken wheel
that is simply absent.

These tests drive the real `_try_import` through the real import machinery by
intercepting `builtins.__import__`, rather than poking the module flags. Poking
the flags would pass even if the classification logic were wrong.
"""
from __future__ import annotations

import builtins
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import astro  # noqa: E402


class _ImportRaiser:
    """Fails `import ephem` with a chosen exception, passes everything else through.

    `name` mirrors what the interpreter sets on a real ModuleNotFoundError: the
    module that could not be found, which may be a dependency rather than ephem.
    """

    def __init__(self, exc: Exception, match: str = "ephem"):
        self.exc = exc
        self.match = match

    def __call__(self, name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name == self.match:
            raise self.exc
        return self._real(name, globals, locals, fromlist, level)


@pytest.fixture
def restore_astro_state():
    """Snapshot and restore the module-level flags, so a test that fails mid-way
    cannot leave the process believing ephem is broken."""
    state = (astro.ephem, astro._HAVE_EPHEM, astro._IMPORT_ERROR, astro._MISSING)
    yield
    astro.ephem, astro._HAVE_EPHEM, astro._IMPORT_ERROR, astro._MISSING = state


def _try_import_with(monkeypatch, exc, match="ephem"):
    real = builtins.__import__
    raiser = _ImportRaiser(exc, match)
    raiser._real = real
    monkeypatch.setattr(builtins, "__import__", raiser)
    return astro._try_import()


def test_a_missing_ephem_is_classified_as_missing(monkeypatch, restore_astro_state):
    """`import ephem` with ephem truly absent: ModuleNotFoundError naming ephem."""
    err = ModuleNotFoundError("No module named 'ephem'", name="ephem")
    assert _try_import_with(monkeypatch, err) is False
    assert astro.is_missing() is True
    assert astro.import_error() == "ModuleNotFoundError: No module named 'ephem'"


def test_a_broken_c_extension_is_not_classified_as_missing(monkeypatch,
                                                          restore_astro_state):
    """Wrong ABI / missing .so: an ImportError that is not a ModuleNotFoundError.
    Reinstalling ephem will not fix it, so it must not be offered as the fix."""
    err = ImportError("libgfortran.so.5: cannot open shared object file")
    assert _try_import_with(monkeypatch, err) is False
    assert astro.is_missing() is False
    assert "libgfortran" in astro.import_error()


def test_a_missing_dependency_of_ephem_is_not_classified_as_missing(monkeypatch,
                                                                   restore_astro_state):
    """`import ephem` can raise naming a *different* module when one of ephem's
    own dependencies is absent. ephem itself is installed, so `pip install ephem`
    is the wrong advice."""
    err = ModuleNotFoundError("No module named 'some_dep'", name="some_dep")
    assert _try_import_with(monkeypatch, err) is False
    assert astro.is_missing() is False
    assert astro.import_error() == "ModuleNotFoundError: No module named 'some_dep'"


def test_the_card_does_not_contradict_itself_when_ephem_is_absent(monkeypatch,
                                                                 restore_astro_state):
    """The user-visible defect. The reason must not claim the library is present
    while the error beside it says it was not found."""
    err = ModuleNotFoundError("No module named 'ephem'", name="ephem")
    assert _try_import_with(monkeypatch, err) is False
    payload = astro.sky_now(37.9838, 23.7275)
    assert payload["available"] is False
    assert "δεν είναι εγκατεστημένη" in payload["reason"]
    assert "υπάρχει αλλά απέτυχε" not in payload["reason"]
    assert payload["missing"] is True
    assert "ModuleNotFoundError" in payload["import_error"]
    assert "-m pip install" in payload["install_hint"]


def test_the_card_says_present_but_broken_only_for_a_real_load_failure(
        monkeypatch, restore_astro_state):
    monkeypatch.setattr(astro, "reload_ephem", lambda: False)
    err = ImportError("libgfortran.so.5: cannot open shared object file")
    assert _try_import_with(monkeypatch, err) is False
    payload = astro.sky_now(37.9838, 23.7275)
    assert "υπάρχει αλλά απέτυχε" in payload["reason"]
    assert "libgfortran" in payload["reason"]
    assert payload["missing"] is False


def test_health_offers_the_install_command_only_when_it_would_help(
        monkeypatch, restore_astro_state):
    """`astro_fix` is a promise that running it fixes the card. Offering it for a
    broken wheel sends the operator round the reinstall loop again."""
    from fastapi.testclient import TestClient

    import app

    client = TestClient(app.app)

    err = ModuleNotFoundError("No module named 'ephem'", name="ephem")
    assert _try_import_with(monkeypatch, err) is False
    opt = client.get("/api/health").json()["optional"]
    assert opt["astro_ephem"] is False
    assert opt["astro_missing"] is True
    assert "astro_fix" in opt

    err = ImportError("libgfortran.so.5: cannot open shared object file")
    assert _try_import_with(monkeypatch, err) is False
    opt = client.get("/api/health").json()["optional"]
    assert opt["astro_missing"] is False
    assert "astro_fix" not in opt
    assert "astro_import_error" in opt
