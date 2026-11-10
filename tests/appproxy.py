"""Test helper: one object that reaches every module of the app.

Tests write `monkeypatch.setattr(m, "TELEGRAM_BOT_TOKEN", "x")` or patch a function such as
`m.request_json` without caring which module defines it. Reads go to the defining module; a patch
is applied to every app module that holds the very same object (the definition and any
`from x import name` copies), so callers anywhere see it, and monkeypatch's undo restores them all.
"""
import sys
import types


class AppProxy:
    def _modules(self):
        return [mod for name, mod in sorted(sys.modules.items())
                if (name == "app" or name.startswith("app.")) and isinstance(mod, types.ModuleType)]

    def _owner(self, name):
        holders = [mod for mod in self._modules() if name in vars(mod)]
        if not holders:
            raise AttributeError(name)
        for mod in holders:   # functions and classes: the module that defines them
            if getattr(vars(mod)[name], "__module__", None) == mod.__name__:
                return mod
        for preferred in ("app.main", "app.config"):   # settings: wherever the code keeps them
            mod = sys.modules.get(preferred)
            if mod in holders:
                return mod
        return holders[0]

    def __getattr__(self, name):
        return getattr(self._owner(name), name)

    def __setattr__(self, name, value):
        try:
            current = vars(self._owner(name))[name]
        except AttributeError:
            object.__setattr__(self, name, value)   # scratch attributes a test keeps on `m`
            return
        for mod in self._modules():
            if name in vars(mod) and vars(mod)[name] is current:
                setattr(mod, name, value)
