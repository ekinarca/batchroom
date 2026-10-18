"""Batchroom CLI.

    batchroom serve                      # start the server + web UI
    batchroom run RECIPE INPUT... -o DIR # headless one-shot batch (CI-friendly)
    batchroom recipes list|validate
    batchroom models list|pull|verify|rm
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from . import APP_NAME, __version__
from .config import Settings
from .core.errors import BatchroomError


def _positive_int(raw: str) -> int:
    """argparse type: an integer >= 1 (BATCHROOM_WORKERS/PORT enforce the same)."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, got {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _settings_from(args) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "data_dir", None):
        settings.data_dir = Path(args.data_dir).expanduser()
    if getattr(args, "workers", None):
        settings.workers = args.workers
    settings.ensure_dirs()
    return settings


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def cmd_serve(args) -> int:
    import uvicorn

    settings = _settings_from(args)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    from .server import create_app

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level)
    return 0


# ---------------------------------------------------------------------------
# run (headless one-shot)
# ---------------------------------------------------------------------------


def cmd_run(args) -> int:
    from .core.image_io import READ_EXTENSIONS
    from .core.inputs import is_hidden_file_name, walk_files
    from .core.manifest import build_manifest, utc_now_iso, write_manifest
    from .core.pipeline import JobNames, process_item
    from .core.recipe import discover_recipes, load_recipe_file, recipe_dirs
    from .models.manager import ModelManager
    from .ops.base import PipelineContext

    settings = _settings_from(args)
    recipe_ref = args.recipe
    if recipe_ref.endswith((".yaml", ".yml")) and Path(recipe_ref).is_file():
        recipe = load_recipe_file(recipe_ref)
    else:
        recipes, problems = discover_recipes(recipe_dirs(settings))
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
        if recipe_ref not in recipes:
            print(f"error: recipe '{recipe_ref}' not found", file=sys.stderr)
            print(f"available: {', '.join(sorted(recipes))}", file=sys.stderr)
            return 1
        recipe = recipes[recipe_ref]

    out_dir = Path(args.output).expanduser()
    files: list[Path] = []
    skipped = 0
    for raw in args.inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            # -o inside an input folder: a re-run must not eat its own results
            candidates = list(walk_files(path, exclude_dirs=(out_dir,)))
        elif path.is_file():
            candidates = [path]
        else:
            print(f"error: input not found: {path}", file=sys.stderr)
            return 1
        for candidate in candidates:
            ext = candidate.suffix.lower()
            if is_hidden_file_name(candidate.name) or ext not in READ_EXTENSIONS:
                skipped += 1
                continue
            if not recipe.accepts_extension(ext):
                skipped += 1
                continue
            files.append(candidate)
    if not files:
        print("error: no processable input files", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    models = ModelManager(settings.models_dir, onnx_threads=settings.onnx_threads)

    # Pre-pull required models so the batch doesn't fail halfway through.
    for model_id in recipe.models_used():
        if not models.is_available(model_id):
            print(f"model '{model_id}' not present — downloading once...")
            try:
                models.ensure(model_id, progress=_download_progress(model_id))
                print()
            except BatchroomError as exc:
                print(f"\nerror: {exc}" + (f"\n  hint: {exc.hint}" if exc.hint else ""),
                      file=sys.stderr)
                return 1

    started = utc_now_iso()
    names = JobNames()
    ctx = PipelineContext(
        recipe_name=recipe.name,
        job_id="cli",
        date=datetime.now(UTC).strftime("%Y%m%d"),
        settings=settings,
        models=models,
    )
    total = len(files)
    print(f"{APP_NAME} {__version__} — recipe '{recipe.name}', {total} file(s), "
          f"{settings.workers} worker(s)")
    if skipped:
        print(f"  (skipped {skipped} non-matching file(s))")

    results = [None] * total

    def work(pair):
        index, source = pair
        return index, process_item(source, recipe, out_dir, ctx, index=index, names=names)

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        for index, result in pool.map(work, enumerate(files)):
            results[index] = result
            mark = "ok" if result.status == "done" else "FAILED"
            line = f"[{index + 1}/{total}] {result.source}: {mark} ({result.duration_ms} ms"
            line += f", {len(result.outputs)} output(s))" if result.status == "done" else ")"
            print(line)
            if result.status == "done":
                ok += 1
            else:
                failed += 1
                print(f"    {result.error}", file=sys.stderr)

    if recipe.output.manifest:
        manifest = build_manifest(
            job_id="cli-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S"),
            recipe_name=recipe.name,
            recipe_version=recipe.version,
            output_structure=recipe.output.structure,
            started_at=started,
            finished_at=None,
            results=[r for r in results if r is not None],
        )
        write_manifest(out_dir, manifest)
    print(f"done: {ok} ok, {failed} failed -> {out_dir}")
    return 0 if failed == 0 else 2


def _download_progress(model_id: str):
    state = {"last": -1}

    def cb(done: int, total: int | None) -> None:
        if total:
            pct = done * 100 // total
            if pct != state["last"]:
                state["last"] = pct
                print(f"\r  {model_id}: {pct}% of {total // (1 << 20)} MiB", end="", flush=True)

    return cb


# ---------------------------------------------------------------------------
# recipes
# ---------------------------------------------------------------------------


def cmd_recipes_list(args) -> int:
    from .core.recipe import discover_recipes, recipe_dirs

    settings = _settings_from(args)
    recipes, problems = discover_recipes(recipe_dirs(settings))
    for recipe in recipes.values():
        origin = "builtin" if recipe.builtin else "user"
        models = f" models: {', '.join(recipe.models_used())}" if recipe.models_used() else ""
        print(f"{recipe.name:24s} [{origin}] {' -> '.join(s.op.name for s in recipe.steps)}{models}")
        if recipe.description:
            print(f"{'':24s} {recipe.description}")
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    return 0


def cmd_recipes_validate(args) -> int:
    from .core.recipe import load_recipe_file

    failures = 0
    for raw in args.files:
        try:
            recipe = load_recipe_file(raw)
            print(f"{raw}: OK ({recipe.name}, {len(recipe.steps)} steps)")
        except BatchroomError as exc:
            failures += 1
            print(f"{raw}: INVALID — {exc}", file=sys.stderr)
            if exc.hint:
                print(f"  hint: {exc.hint}", file=sys.stderr)
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def cmd_models_list(args) -> int:
    from .models.manager import ModelManager

    settings = _settings_from(args)
    manager = ModelManager(settings.models_dir)
    print(f"{'ID':24s} {'TASK':20s} {'LICENSE':14s} {'SIZE':>9s}  STATUS")
    for status in manager.list_status():
        spec = status.spec
        size = f"{spec.size_bytes // (1 << 20)} MiB" if spec.size_bytes else "?"
        state = "downloaded" if status.downloaded else ("available" if status.pinned else "unpinned")
        default = " (default)" if spec.default else ""
        print(f"{spec.id:24s} {spec.task:20s} {spec.license:14s} {size:>9s}  {state}{default}")
    return 0


def cmd_models_pull(args) -> int:
    from .models.manager import ModelManager

    settings = _settings_from(args)
    manager = ModelManager(settings.models_dir)
    try:
        path = manager.ensure(
            args.model, progress=_download_progress(args.model),
            allow_unpinned=args.allow_unpinned,
        )
        print(f"\n{args.model}: ready at {path}")
        return 0
    except BatchroomError as exc:
        print(f"\nerror: {exc}" + (f"\n  hint: {exc.hint}" if exc.hint else ""), file=sys.stderr)
        return 1


def cmd_models_verify(args) -> int:
    from .models.manager import ModelManager

    settings = _settings_from(args)
    manager = ModelManager(settings.models_dir)
    try:
        ok, detail = manager.verify(args.model)
    except BatchroomError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.model}: {detail}")
    return 0 if ok else 1


def cmd_models_rm(args) -> int:
    from .models.manager import ModelManager

    settings = _settings_from(args)
    manager = ModelManager(settings.models_dir)
    removed = manager.delete(args.model)
    print(f"{args.model}: {'removed' if removed else 'was not downloaded'}")
    return 0


# ---------------------------------------------------------------------------
# license (Pro)
# ---------------------------------------------------------------------------


def cmd_license_status(args) -> int:
    import json

    from .pro import load_license

    settings = _settings_from(args)
    info = load_license(settings)
    print(json.dumps(info.to_public_dict(), indent=2))
    return 0 if (info.valid or info.error is None) else 1


def cmd_license_install(args) -> int:
    from .pro import parse_license, save_license

    settings = _settings_from(args)
    text = args.key
    key_path = Path(text)
    try:
        if len(text) < 512 and key_path.is_file():
            text = key_path.read_text(encoding="utf-8")
    except OSError:
        pass
    info = parse_license(text)
    if not info.valid:
        print(f"error: invalid license — {info.error}", file=sys.stderr)
        return 1
    if info.expired:
        print(f"error: license expired on {info.expires}", file=sys.stderr)
        return 1
    save_license(settings, text)
    print(f"license installed: {info.tier} · {info.licensee} · expires {info.expires or 'never'}")
    print("restart the server (or POST /api/license) to activate features")
    return 0


# ---------------------------------------------------------------------------
# users (Pro)
# ---------------------------------------------------------------------------


def cmd_users_create_admin(args) -> int:
    from .pro import load_license
    from .pro.users import UserError, count_users, create_user
    from .queue.db import Database

    settings = _settings_from(args)
    info = load_license(settings)
    if "multi_user" not in info.features():
        print(
            "error: multi-user accounts require an active Pro license\n"
            "  install one first: batchroom license install <key>",
            file=sys.stderr,
        )
        return 1
    password = args.password
    if not password:
        import getpass

        password = getpass.getpass("admin password (min 8 chars): ")
        if getpass.getpass("repeat: ") != password:
            print("error: passwords do not match", file=sys.stderr)
            return 1
    db = Database(settings.db_path)
    try:
        user = create_user(
            db, email=args.email, name=args.name or "", password=password,
            role="admin", seat_limit=info.seats,
        )
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"admin created: {user['email']} (id {user['id']}, {count_users(db)} user(s) total)")
    print("the web UI now requires login")
    return 0


def cmd_users_list(args) -> int:
    from .pro.users import list_users
    from .queue.db import Database

    settings = _settings_from(args)
    for user in list_users(Database(settings.db_path)):
        state = "disabled" if user["disabled"] else "active"
        print(f"{user['id']:>4}  {user['email']:32s} {user['role']:7s} {state}")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batchroom",
        description="Self-hosted AI media processing server — no meters, no quotas, no cloud.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--data-dir", help="state directory (default: ./data or $BATCHROOM_DATA_DIR)")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="start the server and web UI")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=_positive_int, default=None)
    p_serve.add_argument("--workers", type=_positive_int, default=None)
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="run a recipe over files, headless")
    p_run.add_argument("recipe", help="recipe name or path to a recipe .yaml")
    p_run.add_argument("inputs", nargs="+", help="input files or directories")
    p_run.add_argument("-o", "--output", required=True, help="output directory")
    p_run.add_argument("--workers", type=_positive_int, default=None)
    p_run.set_defaults(func=cmd_run)

    p_recipes = sub.add_parser("recipes", help="list or validate recipes")
    recipes_sub = p_recipes.add_subparsers(dest="recipes_command", required=True)
    recipes_sub.add_parser("list", help="list available recipes").set_defaults(
        func=cmd_recipes_list
    )
    p_validate = recipes_sub.add_parser("validate", help="validate recipe files")
    p_validate.add_argument("files", nargs="+")
    p_validate.set_defaults(func=cmd_recipes_validate)

    p_models = sub.add_parser("models", help="manage AI models")
    models_sub = p_models.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser("list", help="list catalog + status").set_defaults(func=cmd_models_list)
    p_pull = models_sub.add_parser("pull", help="download + verify a model")
    p_pull.add_argument("model")
    p_pull.add_argument("--allow-unpinned", action="store_true",
                        help="allow downloading a model without a pinned checksum")
    p_pull.set_defaults(func=cmd_models_pull)
    p_verify = models_sub.add_parser("verify", help="re-hash a downloaded model")
    p_verify.add_argument("model")
    p_verify.set_defaults(func=cmd_models_verify)
    p_rm = models_sub.add_parser("rm", help="delete a downloaded model file")
    p_rm.add_argument("model")
    p_rm.set_defaults(func=cmd_models_rm)

    p_license = sub.add_parser("license", help="manage the Pro license")
    license_sub = p_license.add_subparsers(dest="license_command", required=True)
    license_sub.add_parser("status", help="show license state").set_defaults(
        func=cmd_license_status
    )
    p_install = license_sub.add_parser("install", help="install a license key")
    p_install.add_argument("key", help="key string or path to a key file")
    p_install.set_defaults(func=cmd_license_install)

    p_users = sub.add_parser("users", help="manage users (Pro)")
    users_sub = p_users.add_subparsers(dest="users_command", required=True)
    p_admin = users_sub.add_parser("create-admin", help="bootstrap the first admin account")
    p_admin.add_argument("--email", required=True)
    p_admin.add_argument("--name", default="")
    p_admin.add_argument("--password", help="omit to be prompted securely")
    p_admin.set_defaults(func=cmd_users_create_admin)
    users_sub.add_parser("list", help="list accounts").set_defaults(func=cmd_users_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except BatchroomError as exc:  # e.g. a malformed BATCHROOM_* variable
        print(f"error: {exc}" + (f"\n  hint: {exc.hint}" if exc.hint else ""), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
