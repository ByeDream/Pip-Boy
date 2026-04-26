"""Tests for :mod:`pip_agent.plugins`.

The module is a thin async wrapper around the bundled Claude Code CLI's
``plugin`` / ``plugin marketplace`` subcommands. We test it by mocking
``plugins._run`` (the subprocess seam) and asserting:

* the right ``argv`` is built for each operation, including ``--scope``
  and ``--json`` flags,
* JSON output paths parse correctly (including the empty-stdout case),
* non-zero exit codes raise :class:`PluginsCLIError` with the original
  argv / stderr preserved,
* :class:`PluginsCLINotFound` fires when neither bundled nor PATH CLI
  is available,
* ``plugin_search`` filters ``plugin_list(available=True)`` locally.

There is one belt-and-braces test that the bundled-CLI lookup
*returns* a path (not None) on the developer machine, but it skips
gracefully when the SDK install lacks ``_bundled/claude``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from pip_agent import plugins as plug


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _bundled_cli
# ---------------------------------------------------------------------------


class TestBundledCli:

    def test_returns_path_when_bundled_present(self):
        try:
            cli = plug._bundled_cli()
        except plug.PluginsCLINotFound:
            pytest.skip("Claude Code CLI not bundled in this SDK install")
        assert cli.is_file()
        assert cli.name in ("claude", "claude.exe")

    def test_raises_when_neither_bundled_nor_on_path(self, monkeypatch, tmp_path):
        # Force the bundled lookup to point at a non-existent file
        # AND mask shutil.which so the PATH fallback can't rescue us.
        import claude_agent_sdk

        fake_root = tmp_path / "fake_sdk"
        (fake_root / "_bundled").mkdir(parents=True)
        # Pretend the SDK lives at fake_root by stubbing __file__.
        monkeypatch.setattr(claude_agent_sdk, "__file__", str(fake_root / "__init__.py"))
        monkeypatch.setattr(plug.shutil, "which", lambda _name: None)
        with pytest.raises(plug.PluginsCLINotFound):
            plug._bundled_cli()


# ---------------------------------------------------------------------------
# Subprocess seam: stub _run and assert argv shapes
# ---------------------------------------------------------------------------


class _FakeRun:
    """Stand-in for ``plugins._run`` that records every call.

    Each call returns ``(stdout, stderr, returncode)`` pulled from
    ``next_result`` so individual tests can simulate JSON output,
    plain text, or error exits without monkeypatching attribute by
    attribute.
    """

    def __init__(self, results: list[tuple[str, str, int]] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._results = list(results or [])

    def push(self, stdout: str = "", stderr: str = "", rc: int = 0) -> None:
        self._results.append((stdout, stderr, rc))

    async def __call__(self, *argv: str, cwd=None, timeout=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        if not self._results:
            return ("", "", 0)
        return self._results.pop(0)


@pytest.fixture
def fake_run(monkeypatch):
    fr = _FakeRun()
    monkeypatch.setattr(plug, "_run", fr)
    return fr


class TestMarketplaceAdd:

    def test_argv_includes_source_and_scope(self, fake_run):
        fake_run.push("ok\n")
        out, err, rc = _run(plug.marketplace_add("anthropics/claude-code"))
        assert rc == 0
        assert out == "ok\n"
        assert fake_run.calls[0]["argv"] == [
            "plugin", "marketplace", "add",
            "anthropics/claude-code", "--scope", "user",
        ]

    def test_uses_network_timeout(self, fake_run, monkeypatch):
        # Network-bound ops must use the configurable network timeout,
        # not the 30 s read-side cap. Real-world data point: the bundled
        # ``exa`` plugin install timed out at ~28 s on the dev box,
        # which is why the network policy exists at all.
        monkeypatch.setattr(
            plug, "_network_timeout", lambda: 555.0,
        )
        fake_run.push("ok\n")
        _run(plug.marketplace_add("acme/foo"))
        assert fake_run.calls[0]["timeout"] == 555.0

    def test_project_scope_is_propagated(self, fake_run, tmp_path):
        fake_run.push()
        _run(plug.marketplace_add("foo/bar", scope="project", cwd=tmp_path))
        call = fake_run.calls[0]
        assert call["argv"][-2:] == ["--scope", "project"]
        assert call["cwd"] == tmp_path

    def test_non_zero_exit_raises_cli_error(self, fake_run):
        fake_run.push("", "boom\n", 2)
        with pytest.raises(plug.PluginsCLIError) as exc_info:
            _run(plug.marketplace_add("x/y"))
        assert exc_info.value.returncode == 2
        assert "boom" in exc_info.value.stderr


class TestMarketplaceList:

    def test_parses_json_array(self, fake_run):
        fake_run.push(json.dumps([{"name": "official"}, {"name": "third"}]))
        items = _run(plug.marketplace_list())
        assert [it["name"] for it in items] == ["official", "third"]
        assert fake_run.calls[0]["argv"] == [
            "plugin", "marketplace", "list", "--json",
        ]

    def test_empty_stdout_yields_empty_list(self, fake_run):
        # Some CLI versions emit a blank line when the result set is
        # empty rather than ``[]`` — _parse_json normalises that so
        # callers can iterate without a None-check.
        fake_run.push("\n")
        assert _run(plug.marketplace_list()) == []

    def test_dict_response_is_wrapped(self, fake_run):
        fake_run.push(json.dumps({"name": "single"}))
        items = _run(plug.marketplace_list())
        assert items == [{"name": "single"}]


class TestPluginInstall:

    def test_default_scope_is_user(self, fake_run):
        fake_run.push("installed\n")
        _run(plug.plugin_install("web-search"))
        assert fake_run.calls[0]["argv"] == [
            "plugin", "install", "web-search", "-s", "user",
        ]

    def test_uses_network_timeout(self, fake_run, monkeypatch):
        monkeypatch.setattr(plug, "_network_timeout", lambda: 555.0)
        fake_run.push("ok\n")
        _run(plug.plugin_install("exa"))
        assert fake_run.calls[0]["timeout"] == 555.0

    def test_scope_local_is_propagated_with_cwd(self, fake_run, tmp_path):
        fake_run.push("ok\n")
        _run(plug.plugin_install(
            "web-search@anthropic", scope="local", cwd=tmp_path,
        ))
        call = fake_run.calls[0]
        assert call["argv"][-2:] == ["-s", "local"]
        assert call["argv"][2] == "web-search@anthropic"
        assert call["cwd"] == tmp_path


class TestPluginUninstall:

    def test_no_scope_no_flag(self, fake_run):
        fake_run.push("uninstalled\n")
        _run(plug.plugin_uninstall("web-search"))
        argv = fake_run.calls[0]["argv"]
        assert argv == ["plugin", "uninstall", "web-search"]
        assert "-s" not in argv

    def test_explicit_scope_is_propagated(self, fake_run):
        fake_run.push()
        _run(plug.plugin_uninstall("web-search", scope="project"))
        argv = fake_run.calls[0]["argv"]
        assert argv[-2:] == ["-s", "project"]


class TestPluginList:

    def test_default_lists_installed(self, fake_run):
        fake_run.push("[]")
        _run(plug.plugin_list())
        argv = fake_run.calls[0]["argv"]
        assert argv == ["plugin", "list", "--json"]
        assert "--available" not in argv

    def test_available_flag_is_passed(self, fake_run):
        fake_run.push("[]")
        _run(plug.plugin_list(available=True))
        argv = fake_run.calls[0]["argv"]
        assert "--available" in argv
        assert "--json" in argv

    def test_returns_parsed_items(self, fake_run):
        fake_run.push(json.dumps([
            {"name": "a", "scope": "user"},
            {"name": "b", "scope": "project"},
        ]))
        items = _run(plug.plugin_list())
        assert {it["name"] for it in items} == {"a", "b"}

    def test_available_unwraps_envelope_dict(self, fake_run):
        # ``plugin list --available --json`` returns
        # ``{"installed": [...], "available": [...]}`` rather than a
        # flat array. We must surface ``available[*]`` (the catalogue),
        # not wrap the whole envelope into a single bogus item — that
        # was the regression that rendered "Available plugins (1): ?"
        # in the host CLI.
        fake_run.push(json.dumps({
            "installed": [],
            "available": [
                {"name": "exa", "marketplaceName": "claude-plugins-official"},
                {"name": "firecrawl", "marketplaceName": "claude-plugins-official"},
            ],
        }))
        items = _run(plug.plugin_list(available=True))
        assert [it["name"] for it in items] == ["exa", "firecrawl"]

    def test_installed_unwraps_envelope_dict(self, fake_run):
        # Symmetrical to the available case: if the CLI ever switches
        # the plain ``plugin list --json`` to the envelope shape too,
        # ``installed`` should be picked here.
        fake_run.push(json.dumps({
            "installed": [{"name": "a", "scope": "user"}],
            "available": [{"name": "b"}],
        }))
        items = _run(plug.plugin_list(available=False))
        assert [it["name"] for it in items] == ["a"]

    def test_envelope_with_missing_field_returns_empty(self, fake_run):
        fake_run.push(json.dumps({"installed": []}))
        assert _run(plug.plugin_list(available=True)) == []

    def test_unexpected_scalar_returns_empty(self, fake_run):
        fake_run.push("42")
        assert _run(plug.plugin_list()) == []


class TestPluginEnableDisable:

    def test_enable_with_scope(self, fake_run):
        fake_run.push()
        _run(plug.plugin_enable("foo", scope="user"))
        assert fake_run.calls[0]["argv"] == [
            "plugin", "enable", "foo", "-s", "user",
        ]

    def test_disable_without_scope(self, fake_run):
        fake_run.push()
        _run(plug.plugin_disable("foo"))
        assert fake_run.calls[0]["argv"] == ["plugin", "disable", "foo"]


class TestPluginSearch:
    """``plugin_search`` filters ``plugin_list(available=True)`` locally
    rather than calling a non-existent CLI subcommand. We verify the
    matching rules directly so a future CLI bump that adds a real
    ``plugin search`` doesn't silently regress to a different shape.
    """

    def test_substring_matches_name_description_tags(self, fake_run):
        fake_run.push(json.dumps([
            {"name": "pdf-tools", "description": "Read PDFs", "tags": []},
            {"name": "image", "description": "Process images", "tags": ["graphics"]},
            {"name": "browser", "description": "Web fetching", "tags": ["web", "search"]},
        ]))
        results = _run(plug.plugin_search("PDF"))
        assert {it["name"] for it in results} == {"pdf-tools"}

    def test_tag_substring_matches(self, fake_run):
        fake_run.push(json.dumps([
            {"name": "a", "description": "", "tags": ["graphics"]},
            {"name": "b", "description": "", "tags": ["web", "search"]},
        ]))
        results = _run(plug.plugin_search("graph"))
        assert [it["name"] for it in results] == ["a"]

    def test_empty_query_returns_all(self, fake_run):
        fake_run.push(json.dumps([{"name": "x"}, {"name": "y"}]))
        results = _run(plug.plugin_search(""))
        assert len(results) == 2

    def test_no_match_returns_empty_list(self, fake_run):
        fake_run.push(json.dumps([{"name": "a", "description": "x"}]))
        assert _run(plug.plugin_search("nope")) == []


# ---------------------------------------------------------------------------
# run_sync bridge
# ---------------------------------------------------------------------------


class TestRunSync:
    """``plugins.run_sync`` exists so synchronous host-command handlers
    can drive async wrappers from inside an already-running event loop.
    """

    def test_returns_coroutine_result(self):
        async def coro() -> int:
            return 42

        assert plug.run_sync(coro()) == 42

    def test_propagates_exception(self):
        async def coro():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            plug.run_sync(coro())


# ---------------------------------------------------------------------------
# Sanity: every async wrapper is wired into a profile span name. Catches
# the "forgot to wrap with _profile.span" regression cheaply.
# ---------------------------------------------------------------------------


class TestProfileSpans:

    @pytest.mark.parametrize(
        "fn, span",
        [
            (lambda: plug.marketplace_add("x"),         "plugins.marketplace_add"),
            (lambda: plug.marketplace_list(),           "plugins.marketplace_list"),
            (lambda: plug.marketplace_remove("x"),      "plugins.marketplace_remove"),
            (lambda: plug.marketplace_update(),         "plugins.marketplace_update"),
            (lambda: plug.plugin_install("x"),          "plugins.install"),
            (lambda: plug.plugin_uninstall("x"),        "plugins.uninstall"),
            (lambda: plug.plugin_list(),                "plugins.list"),
            (lambda: plug.plugin_enable("x"),           "plugins.enable"),
            (lambda: plug.plugin_disable("x"),          "plugins.disable"),
            (lambda: plug.plugin_search("x"),           "plugins.search"),
        ],
    )
    def test_each_wrapper_opens_named_span(self, fake_run, monkeypatch, fn, span):
        # Push enough successful results for any chain (search → list → return).
        for _ in range(3):
            fake_run.push("[]")

        opened: list[str] = []

        from pip_agent import _profile
        real_span = _profile.span

        def spy_span(name, **meta):
            opened.append(name)
            return real_span(name, **meta)

        monkeypatch.setattr(_profile, "span", spy_span)
        _run(fn())
        assert span in opened


# ---------------------------------------------------------------------------
# ensure_marketplaces — host cold-start bootstrap
# ---------------------------------------------------------------------------


class TestEnsureMarketplaces:
    """Idempotent bootstrap helper used by ``run_host``.

    Behaviour we lock in:

    * skips specs whose ``owner/repo`` already shows up in the
      ``marketplace_list`` ``repo`` field — no extra subprocess,
    * still calls ``add`` for missing specs (and reports them in the
      return value),
    * never raises: list / add failures degrade to a WARNING log so
      the host cold-start is unblocked,
    * returns ``[]`` for an empty / whitespace-only input without
      touching the CLI at all.
    """

    def test_skips_already_present_owner_repo(self, fake_run):
        fake_run.push(json.dumps([
            {"name": "claude-plugins-official",
             "repo": "anthropics/claude-plugins-official"},
        ]))
        added = _run(plug.ensure_marketplaces([
            "anthropics/claude-plugins-official",
        ]))
        assert added == []
        # Only the list call should have fired.
        assert len(fake_run.calls) == 1
        assert fake_run.calls[0]["argv"][:3] == ["plugin", "marketplace", "list"]

    def test_adds_missing_owner_repo(self, fake_run):
        fake_run.push("[]")
        fake_run.push("Successfully added marketplace: foo\n")
        added = _run(plug.ensure_marketplaces(["acme/foo"]))
        assert added == ["acme/foo"]
        assert len(fake_run.calls) == 2
        add_argv = fake_run.calls[1]["argv"]
        assert add_argv[:4] == ["plugin", "marketplace", "add", "acme/foo"]
        assert add_argv[-2:] == ["--scope", "user"]

    def test_mixed_present_and_missing(self, fake_run):
        fake_run.push(json.dumps([
            {"name": "old", "repo": "foo/bar"},
        ]))
        fake_run.push("ok\n")
        added = _run(plug.ensure_marketplaces(["foo/bar", "baz/qux"]))
        assert added == ["baz/qux"]
        # list + one add only.
        assert len(fake_run.calls) == 2
        assert fake_run.calls[1]["argv"][3] == "baz/qux"

    def test_blank_specs_are_skipped(self, fake_run):
        added = _run(plug.ensure_marketplaces(["", "   ", None]))  # type: ignore[list-item]
        assert added == []
        assert fake_run.calls == []

    def test_strips_whitespace_around_specs(self, fake_run):
        fake_run.push("[]")
        fake_run.push("ok\n")
        added = _run(plug.ensure_marketplaces(["  acme/foo  "]))
        assert added == ["acme/foo"]
        assert fake_run.calls[1]["argv"][3] == "acme/foo"

    def test_list_failure_swallowed_and_returns_empty(
        self, monkeypatch, caplog,
    ):
        async def boom(*_a, **_k):
            return ("", "list broke", 5)

        monkeypatch.setattr(plug, "_run", boom)
        with caplog.at_level("WARNING", logger=plug.__name__):
            added = _run(plug.ensure_marketplaces(["acme/foo"]))
        assert added == []
        assert any("marketplace bootstrap" in rec.message for rec in caplog.records)

    def test_add_failure_logs_but_continues(self, monkeypatch, caplog):
        # First call (list) returns []; subsequent two adds: first fails,
        # second succeeds. We expect the second to still go through.
        results: list[tuple[str, str, int]] = [
            ("[]", "", 0),
            ("", "git: connection refused", 5),
            ("ok\n", "", 0),
        ]
        calls: list[list[str]] = []

        async def fake_run(*argv, cwd=None, timeout=None):
            calls.append(list(argv))
            return results.pop(0)

        monkeypatch.setattr(plug, "_run", fake_run)
        with caplog.at_level("WARNING", logger=plug.__name__):
            added = _run(plug.ensure_marketplaces(["broken/one", "good/two"]))
        assert added == ["good/two"]
        assert len(calls) == 3  # list + two adds
        assert any(
            "broken/one" in rec.message and "failed" in rec.message
            for rec in caplog.records
        )

    def test_non_owner_repo_spec_is_always_attempted(self, fake_run):
        # URL / local-path specs aren't matched against the ``repo`` field
        # (the CLI itself is idempotent in that case — duplicate add
        # exits 0). We still want them to fire so the bootstrap reaches
        # them when the corresponding marketplace JSON record uses a
        # different shape.
        fake_run.push(json.dumps([
            {"name": "stuff", "repo": "anthropics/claude-plugins-official"},
        ]))
        fake_run.push("ok\n")
        added = _run(plug.ensure_marketplaces([
            "https://github.com/some/repo.git",
        ]))
        assert added == ["https://github.com/some/repo.git"]
        assert len(fake_run.calls) == 2


# ---------------------------------------------------------------------------
# ensure_bootstrap_once — lazy gate at first public coroutine use
# ---------------------------------------------------------------------------


class TestLazyBootstrap:
    """First call into any gated coroutine runs the configured bootstrap;
    later calls short-circuit. Previously the bootstrap ran at host boot
    and paid a ~3 s subprocess hit per launch; this gate shifts it to
    first plugin use so sessions that never touch plugins avoid it.
    """

    @pytest.fixture(autouse=True)
    def _reset_gate(self):
        # The suite-wide autouse fixture in ``conftest.py`` closes the
        # gate for ordinary tests; these tests need it open. Reset
        # before and after so state doesn't leak either way.
        plug.reset_bootstrap_for_test()
        yield
        plug.reset_bootstrap_for_test()

    def test_first_gated_call_fires_bootstrap(self, fake_run, monkeypatch):
        from pip_agent.config import settings
        monkeypatch.setattr(
            settings, "bootstrap_marketplaces", "acme/foo", raising=False,
        )
        # marketplace_list (inside ensure_marketplaces) sees empty catalogue
        # → marketplace_add fires → then the user's own marketplace_list
        # call runs and returns the real payload.
        fake_run.push("[]")            # bootstrap's own list
        fake_run.push("ok\n")          # bootstrap's add
        fake_run.push("[]")            # user's list
        _run(plug.marketplace_list())
        # 3 subprocess spawns: bootstrap list + bootstrap add + user list.
        argvs = [c["argv"][:3] for c in fake_run.calls]
        assert argvs == [
            ["plugin", "marketplace", "list"],
            ["plugin", "marketplace", "add"],
            ["plugin", "marketplace", "list"],
        ]

    def test_second_call_does_not_rerun_bootstrap(self, fake_run, monkeypatch):
        from pip_agent.config import settings
        monkeypatch.setattr(
            settings, "bootstrap_marketplaces", "acme/foo", raising=False,
        )
        fake_run.push("[]")            # bootstrap list
        fake_run.push("ok\n")          # bootstrap add
        fake_run.push("[]")            # first user list
        fake_run.push("[]")            # second user list
        _run(plug.marketplace_list())
        _run(plug.marketplace_list())
        # Bootstrap fires exactly once — 2 bootstrap calls + 2 user calls.
        assert len(fake_run.calls) == 4

    def test_empty_setting_skips_bootstrap_entirely(self, fake_run, monkeypatch):
        from pip_agent.config import settings
        monkeypatch.setattr(
            settings, "bootstrap_marketplaces", "", raising=False,
        )
        fake_run.push("[]")
        _run(plug.marketplace_list())
        # Only the user's own call; no bootstrap subprocess.
        assert len(fake_run.calls) == 1

    def test_bootstrap_failure_is_swallowed_and_flag_flipped(
        self, fake_run, monkeypatch, caplog,
    ):
        from pip_agent.config import settings
        monkeypatch.setattr(
            settings, "bootstrap_marketplaces", "acme/foo", raising=False,
        )
        # Explode inside ensure_marketplaces — the gate must catch it,
        # log a WARNING, and still flip _bootstrap_done so retries
        # don't re-spawn the subprocess on every subsequent call.
        async def boom(_specs, **_kw):
            raise RuntimeError("subprocess vanished")
        monkeypatch.setattr(plug, "ensure_marketplaces", boom)
        fake_run.push("[]")            # user's list after the failed gate
        import logging
        with caplog.at_level(logging.WARNING, logger=plug.__name__):
            _run(plug.marketplace_list())
        assert plug._bootstrap_done is True
        assert any(
            "marketplace bootstrap aborted" in rec.message
            for rec in caplog.records
        )

    def test_install_also_triggers_gate(self, fake_run, monkeypatch):
        # Every public coroutine is gated, not just the marketplace ones.
        from pip_agent.config import settings
        monkeypatch.setattr(
            settings, "bootstrap_marketplaces", "acme/foo", raising=False,
        )
        fake_run.push("[]")            # bootstrap list
        fake_run.push("ok\n")          # bootstrap add
        fake_run.push("ok\n")          # user's install
        _run(plug.plugin_install("something"))
        # First call: marketplace list (bootstrap). Last call: install.
        assert fake_run.calls[0]["argv"][:3] == ["plugin", "marketplace", "list"]
        assert fake_run.calls[-1]["argv"][:2] == ["plugin", "install"]
