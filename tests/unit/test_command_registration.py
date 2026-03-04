"""Unit tests for top-level command registration."""

from types import SimpleNamespace

from src.handlers import register_commands
from src.handlers.actions import register_actions


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}
        self.actions: dict[str, object] = {}
        self.views: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator

    def action(self, name):
        def decorator(func):
            self.actions[str(name)] = func
            return func

        return decorator

    def view(self, name):
        def decorator(func):
            self.views[str(name)] = func
            return func

        return decorator


def test_register_commands_excludes_codex_slash_commands():
    app = _FakeApp()
    db = SimpleNamespace()
    claude_executor = SimpleNamespace()
    codex_executor = SimpleNamespace()

    register_commands(app, db, claude_executor, codex_executor=codex_executor)

    assert "/usage" in app.handlers
    assert "/clear" in app.handlers
    assert "/git" in app.handlers
    assert "/codex-status" not in app.handlers
    assert "/codex-clear" not in app.handlers
    assert "/codex-sessions" not in app.handlers
    assert "/codex-cleanup" not in app.handlers
    assert "/codex-thread" not in app.handlers
    assert "/codex-config" not in app.handlers
    assert "/codex-metrics" not in app.handlers


def test_register_actions_includes_worktree_buttons():
    app = _FakeApp()
    db = SimpleNamespace()
    claude_executor = SimpleNamespace()
    codex_executor = SimpleNamespace()

    deps = register_commands(app, db, claude_executor, codex_executor=codex_executor)
    register_actions(app, deps)

    assert "worktree_switch" in app.actions
    assert "worktree_merge_current" in app.actions
    assert "worktree_remove" in app.actions
