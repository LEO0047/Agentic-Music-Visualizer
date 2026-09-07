"""A very small fake of the TouchDesigner Python surface.

TouchDesigner is not installed on this machine and cannot be installed as a
library, so the parts of the reflex layer that are *logic* — OSC routing, the
manual-mode freeze, the drop executor, the heartbeat watchdog — are written
against a duck-typed ``comp`` object and exercised here.

What is faked, and only what is used:

* :class:`Par` — ``.val`` / ``.eval()`` / ``.menuNames`` / ``.menuLabels`` /
  ``.menuIndex`` / ``.default`` / ``.readOnly`` / ``.min`` / ``.max``.
* :class:`ParCollection` — ``comp.par.Scene`` reads, ``comp.par.Scene = x``
  writes, ``hasattr`` says whether a par exists.
* :class:`Page` — ``appendMenu`` / ``appendFloat`` / ``appendInt`` /
  ``appendStr`` / ``appendToggle``, each returning a ParGroup you index with
  ``[0]`` exactly like TD does.
* :class:`Comp` — ``.par``, ``.store`` / ``.fetch`` / ``.unstore``, ``.op()``,
  ``.parent()``, ``.appendCustomPage()``, ``.create()``, ``.destroy()``.
* :class:`TextDAT` — ``.text``, plus ``clear()`` / ``appendRow()`` / ``rows``
  so the Ramp TOP key tables ``build_network`` writes can be inspected.

This is a test double, not a TD emulator: it does not cook anything, does not
evaluate expressions and does not enforce TD's own par validation beyond menu
membership.
"""

from __future__ import annotations

from typing import Any, Iterator, Sequence

__all__ = [
    "Par",
    "ParGroup",
    "ParCollection",
    "Page",
    "Comp",
    "TextDAT",
    "OpRegistry",
    "build_pars",
    "director_comp",
]


class Par:
    """One custom parameter."""

    __hash__ = object.__hash__

    def __init__(
        self,
        name: str,
        style: str = "Float",
        default: Any = None,
        menuNames: Sequence[str] = (),
        menuLabels: Sequence[str] = (),
        min: float | None = None,
        max: float | None = None,
        clampMin: bool = False,
        clampMax: bool = False,
        readOnly: bool = False,
        label: str = "",
        owner: "Comp | None" = None,
    ) -> None:
        self.name = name
        self.style = style
        self.label = label or name
        self.menuNames = list(menuNames)
        self.menuLabels = list(menuLabels) or list(menuNames)
        self.min = min
        self.max = max
        self.clampMin = clampMin
        self.clampMax = clampMax
        self.readOnly = readOnly
        self.owner = owner
        self.expr: str | None = None
        self.default = default
        self._val: Any = default if default is not None else self._zero()
        self.writes: list[Any] = []

    # -- value ------------------------------------------------------------

    def _zero(self) -> Any:
        if self.style == "Menu":
            return self.menuNames[0] if self.menuNames else ""
        if self.style == "Str":
            return ""
        if self.style in ("Int", "Toggle"):
            return 0
        return 0.0

    @property
    def val(self) -> Any:
        return self._val

    @val.setter
    def val(self, value: Any) -> None:
        if self.style == "Menu":
            text = str(value)
            if text not in self.menuNames:
                raise ValueError(f"{self.name}: {text!r} is not in menu {self.menuNames}")
            self._val = text
        elif self.style == "Str":
            self._val = str(value)
        elif self.style in ("Int", "Toggle"):
            self._val = int(value)
        else:
            self._val = float(value)
        self.writes.append(self._val)

    def eval(self) -> Any:
        return self._val

    # -- menus ------------------------------------------------------------

    @property
    def menuIndex(self) -> int:
        if not self.menuNames:
            return int(self._val)
        try:
            return self.menuNames.index(self._val)
        except ValueError:  # pragma: no cover - val is validated on write
            return 0

    @menuIndex.setter
    def menuIndex(self, index: int) -> None:
        self.val = self.menuNames[int(index)]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Par):
            return other is self
        return self._val == other

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Par {self.name} {self.style}={self._val!r}>"


class ParGroup(list):
    """TD's ``appendXxx`` returns a ParGroup; code indexes it with ``[0]``."""


class ParCollection:
    """``comp.par`` — attribute access with TD's write-through assignment."""

    def __init__(self) -> None:
        object.__setattr__(self, "_pars", {})

    def _add(self, par: Par) -> Par:
        self._pars[par.name] = par
        return par

    def __getattr__(self, name: str) -> Par:
        pars = object.__getattribute__(self, "_pars")
        if name in pars:
            return pars[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        pars = object.__getattribute__(self, "_pars")
        if name in pars:
            pars[name].val = value
            return
        raise AttributeError(f"no custom par named {name!r}")

    def __contains__(self, name: object) -> bool:
        return name in self._pars

    def __iter__(self) -> Iterator[Par]:
        return iter(self._pars.values())

    def __len__(self) -> int:
        return len(self._pars)

    def names(self) -> list[str]:
        return list(self._pars)


class Page:
    """A custom parameter page."""

    def __init__(self, name: str, comp: "Comp") -> None:
        self.name = name
        self.comp = comp
        self.pars: list[Par] = []

    def _append(self, style: str, name: str, **kwargs: Any) -> ParGroup:
        par = Par(name, style=style, owner=self.comp, **kwargs)
        self.pars.append(par)
        self.comp.par._add(par)
        return ParGroup([par])

    def appendMenu(self, name: str, **kwargs: Any) -> ParGroup:
        return self._append("Menu", name, **kwargs)

    def appendFloat(self, name: str, **kwargs: Any) -> ParGroup:
        return self._append("Float", name, **kwargs)

    def appendInt(self, name: str, **kwargs: Any) -> ParGroup:
        return self._append("Int", name, **kwargs)

    def appendStr(self, name: str, **kwargs: Any) -> ParGroup:
        return self._append("Str", name, **kwargs)

    def appendToggle(self, name: str, **kwargs: Any) -> ParGroup:
        return self._append("Toggle", name, **kwargs)


class _Op:
    """Common base: name, path, parent, storage."""

    def __init__(self, name: str, parent: "Comp | None" = None) -> None:
        self.name = name
        self._parent = parent
        self._storage: dict[str, Any] = {}
        self.valid = True
        if parent is not None:
            parent.children[name] = self

    @property
    def path(self) -> str:
        if self._parent is None:
            return f"/{self.name}"
        return f"{self._parent.path}/{self.name}"

    def parent(self, level: int = 1) -> "Comp | None":
        node: Any = self
        for _ in range(level):
            if node is None:
                return None
            node = node._parent
        return node

    # -- storage ----------------------------------------------------------

    def store(self, key: str, value: Any) -> Any:
        self._storage[key] = value
        return value

    def fetch(self, key: str, default: Any = None, search: bool = True) -> Any:
        if key in self._storage:
            return self._storage[key]
        return default

    def unstore(self, key: str) -> None:
        self._storage.pop(key, None)

    @property
    def storage(self) -> dict[str, Any]:
        return self._storage

    def destroy(self) -> None:
        if self._parent is not None:
            self._parent.children.pop(self.name, None)
        self.valid = False


class Comp(_Op):
    """A COMP: custom pars, children, storage."""

    def __init__(self, name: str = "director", parent: "Comp | None" = None) -> None:
        super().__init__(name, parent)
        self.par = ParCollection()
        self.children: dict[str, _Op] = {}
        self.pages: dict[str, Page] = {}
        self.opType = "baseCOMP"

    def appendCustomPage(self, name: str) -> Page:
        page = self.pages.get(name)
        if page is None:
            page = Page(name, self)
            self.pages[name] = page
        return page

    @property
    def customPages(self) -> list[Page]:
        return list(self.pages.values())

    def create(self, optype: Any, name: str) -> "_Op":
        kind = getattr(optype, "__name__", str(optype))
        node = TextDAT(name, self) if "DAT" in kind else Comp(name, self)
        node.opType = kind
        return node

    def op(self, path: str) -> "_Op | None":
        """Resolve a relative path: ``x``, ``./x``, ``../x``, ``a/b``."""
        node: Any = self
        parts = [p for p in str(path).split("/") if p not in ("", ".")]
        if str(path).startswith("/"):
            while node.parent() is not None:
                node = node.parent()
            # A leading-slash path in the stub is resolved from the stub root.
            if parts and parts[0] == node.name:
                parts = parts[1:]
        for part in parts:
            if part == "..":
                node = node.parent()
            else:
                node = getattr(node, "children", {}).get(part)
            if node is None:
                return None
        return node


class TextDAT(_Op):
    """A Text DAT, plus the sliver of the Table DAT surface the build uses.

    ``build_network`` fills the Ramp TOP key tables with ``clear()`` +
    ``appendRow([...])``, so those two live here as well; ``rows`` is the
    resulting table and ``text`` stays the tab-separated rendering of it.
    """

    def __init__(self, name: str = "on_drop_dat", parent: "Comp | None" = None, text: str = "") -> None:
        super().__init__(name, parent)
        self.text = text
        self.rows: list[list[Any]] = []
        self.opType = "textDAT"

    def clear(self) -> None:
        self.text = ""
        self.rows = []

    def write(self, text: str) -> None:
        self.text += text

    def appendRow(self, cells: Sequence[Any]) -> list[Any]:  # noqa: N802 - TD API
        row = list(cells)
        self.rows.append(row)
        self.text += "\t".join(str(cell) for cell in row) + "\n"
        return row

    @property
    def numRows(self) -> int:  # noqa: N802 - TD API
        return len(self.rows)


class OpRegistry:
    """A callable ``op()`` over a flat ``{path: operator}`` registry."""

    def __init__(self, root: Comp | None = None) -> None:
        self.root = root
        self.entries: dict[str, _Op] = {}
        if root is not None:
            self.add(root)

    def add(self, node: _Op) -> _Op:
        self.entries[node.path] = node
        self.entries[node.name] = node
        for child in getattr(node, "children", {}).values():
            self.add(child)
        return node

    def __call__(self, path: str) -> _Op | None:
        return self.entries.get(str(path))


# --------------------------------------------------------------------------
# factories used by the tests
# --------------------------------------------------------------------------


def build_pars(comp: Comp, specs) -> Comp:
    """Create every :class:`~parspec.ParSpec` as a custom par on *comp*.

    Mirrors what ``build_network.build_director_pars`` does inside TD, so the
    stub director carries exactly the parameter surface the real one does.
    """
    for spec in specs:
        page = comp.appendCustomPage(spec.page)
        kwargs: dict[str, Any] = {"label": spec.label}
        if spec.style == "Menu":
            kwargs["menuNames"] = spec.menu_names
            kwargs["menuLabels"] = spec.menu_labels
        if spec.min is not None:
            kwargs["min"] = spec.min
            kwargs["clampMin"] = spec.clamp
        if spec.max is not None:
            kwargs["max"] = spec.max
            kwargs["clampMax"] = spec.clamp
        if spec.readonly:
            kwargs["readOnly"] = True
        kwargs["default"] = spec.default
        getattr(page, spec.append_method)(spec.name, **kwargs)
    return comp


def director_comp(specs=None, on_drop_text: str = "") -> Comp:
    """A stub ``/project1/amv/director`` with its pars and child DATs."""
    if specs is None:
        import parspec  # local import: keeps td_stub importable on its own

        specs = parspec.pars_from_schema(parspec.load_default_schema())
    amv = Comp("amv")
    director = Comp("director", amv)
    build_pars(director, specs)
    TextDAT("on_drop_dat", director, on_drop_text)
    TextDAT("osc_in", director)
    return director
