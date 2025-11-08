import argparse
import json
import shutil
import sys
import warnings
import webbrowser
from pathlib import Path
from typing import cast

import pdoc
import pdoc.doc
import pdoc.extract
import pdoc.render
import pdoc.search
from tqdm import tqdm

DEFAULT_IGNORE = {"!idlelib", "!idlelib.", "!turtledemo.", "!lib2to3."}


class Cache[K, V]:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def key(self, key: K) -> Path:
        return Path(str(key))

    def save(self, path: Path, value: V) -> None: ...
    def load(self, path: Path) -> V: ...
    def compute(self, key: K) -> V: ...

    def get(self, key: K) -> V:
        try:
            return self[key]
        except KeyError:
            value = self.compute(key)
            self[key] = value
            return value

    def __getitem__(self, key: K) -> V:
        path = self.path / self.key(key)
        if not path.exists():
            raise KeyError(key)
        return self.load(path)

    def __setitem__(self, key: K, value: V) -> None:
        path = self.path / self.key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.save(path, value)

    def __delitem__(self, key: K) -> None:
        path = self.path / self.key(key)
        if path.exists():
            path.unlink()

    def __contains__(self, key: K) -> bool:
        path = self.path / self.key(key)
        return path.exists()


class CacheHTML(Cache[pdoc.doc.Module, str]):
    def __init__(self, path: Path, modules: dict[str, pdoc.doc.Module]) -> None:
        super().__init__(path)
        self.modules = modules

    def key(self, key: pdoc.doc.Module) -> Path:
        return Path(key.fullname.replace(".", "/") + ".html")

    def save(self, path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")

    def load(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def compute(self, key: pdoc.doc.Module) -> str:
        return pdoc.render.html_module(key, self.modules)


class CacheIndex(Cache[tuple[str, pdoc.doc.Module], list[dict]]):
    def __init__(self, path: Path, modules: dict[str, pdoc.doc.Module]) -> None:
        super().__init__(path)

        module_template: pdoc.render.jinja2.Template = pdoc.render.env.get_template("module.html.jinja2")
        self.ctx: pdoc.render.jinja2.runtime.Context = module_template.new_context(
            {"module": pdoc.doc.Module(pdoc.render.types.ModuleType("")), "all_modules": modules}
        )
        for _ in module_template.root_render_func(self.ctx):  # type: ignore
            pass

    def key(self, key: tuple[str, pdoc.doc.Module]) -> Path:
        return Path(key[0].replace(".", "/") + ".json")

    def save(self, path: Path, value: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as f:
            json.dump(value, f)

    def load(self, path: Path) -> list[dict]:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def compute(self, key: tuple[str, pdoc.doc.Module]) -> list[dict]:
        return pdoc.search.make_index(
            dict([key]),
            self._is_public,
            cast(str, pdoc.render.env.globals["docformat"]),
        )

    def _is_public(self, x: pdoc.doc.Doc) -> bool:
        return bool(self.ctx["is_public"](x).strip())


def search_index(modules: dict[str, pdoc.doc.Module], cache_path: Path) -> str:
    """Renders the Elasticlunr.js search index."""
    cache = CacheIndex(cache_path, modules)
    if not pdoc.render.env.globals["search"]:
        return ""

    index = [
        idx for name, mod in tqdm(modules.items(), "Indexing modules", unit="modules") for idx in cache.get((name, mod))
    ]

    print("Compiling Search Index...")
    compile_js = Path(pdoc.render.env.get_template("build-search-index.js").filename)  # type: ignore
    return pdoc.render.env.get_template("search.js.jinja2").render(
        search_index=pdoc.search.precompile_index(index, compile_js)
    )


def render_modules(modules: dict[str, pdoc.doc.Module], output_directory: Path) -> None:
    cache = CacheHTML(output_directory, modules)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        for name, module in (pbar := tqdm(modules.items(), "Rendering modules", unit="modules")):
            pbar.set_postfix({"module": name})
            if module not in cache:
                cache[module] = cache.compute(module)

    index = pdoc.render.html_index(modules)
    if index:
        (output_directory / "index.html").write_bytes(index.encode())

    search = search_index(modules, output_directory / ".cache" / "search")
    if search:
        (output_directory / "search.js").write_bytes(search.encode())


def load_modules(modules: list[str]) -> dict[str, pdoc.doc.Module]:
    loaded = {}
    invalid = set()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        for name in (pbar := tqdm(pdoc.extract.walk_specs(modules), "Loading modules", unit="modules")):
            pbar.set_postfix({"module": name})
            try:
                if name in invalid:
                    continue
                loaded[name] = pdoc.doc.Module.from_name(name)
            except RuntimeError as exc:
                if "Error importing" in str(exc):
                    parts = name.split(".")
                    invalid |= {".".join(name[:i]) for i in range(1, len(parts))}
    return {
        name: mod
        for name, mod in sorted(loaded.items(), key=lambda x: (int(bool(x[0].count("."))), x[0]))
        if name not in invalid
    }


def build_modules(modules: list[str], output: Path) -> set[str]:
    """Build the docs for the given modules."""
    output.mkdir(parents=True, exist_ok=True)
    pdoc.render.configure(math=True, mermaid=True)
    targets = load_modules(modules)
    render_modules(
        targets,
        output,
    )

    return set(targets.keys())


def get_stdlib() -> set[str]:
    builtin = {mod for mod in sys.builtin_module_names if not mod.startswith("_")}
    lib = {mod for mod in sys.stdlib_module_names if not mod.startswith("_")}
    return builtin | lib


def cli() -> argparse.Namespace:
    def as_path(path: str) -> Path:
        return Path(path).resolve()

    parser = argparse.ArgumentParser(description="Generate HTML documentation for Python modules.")
    parser.add_argument(
        "modules",
        nargs="*",
        default=[],
        help="List of modules to document.",
    )
    parser.add_argument(
        "--output",
        default="docs",
        type=as_path,
        help="Output directory for the generated documentation.",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=list(DEFAULT_IGNORE),
        help="List of modules to ignore.",
    )
    parser.add_argument(
        "--no-stdlib",
        dest="stdlib",
        action="store_false",
        default=True,
        help="Include standard library modules.",
    )
    # TODO : Don't cache the input modules that are subject to change
    parser.add_argument(
        "--no-cache",
        dest="cache",
        action="store_false",
        default=True,
        help="Rebuild the cache.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        default=False,
        help="Open the generated documentation in the default web browser.",
    )

    return parser.parse_args()


def main() -> None:
    args = cli()
    module_names = []
    if args.stdlib:
        module_names.extend(get_stdlib())
    module_names.extend(args.ignore)
    module_names.extend(args.modules)

    path_cache: Path = args.output / ".cache"
    if not args.cache and path_cache.exists():
        shutil.rmtree(path_cache)

    modules = build_modules(
        module_names,
        output=args.output,
    )
    print(f"Documented {len(modules)} modules")
    if args.open:
        webbrowser.open(args.output / "index.html")


if __name__ == "__main__":
    main()
