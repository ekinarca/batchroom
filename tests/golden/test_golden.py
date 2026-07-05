"""Golden-image regression suite: every case must match the committed
expected outputs within pixel tolerance. Regenerate deliberately with
``python tests/golden/gen.py --update`` and review the diff."""

from pathlib import Path

import pytest

from .gen import CASES, CORPUS_DIR, EXPECTED_DIR, compare_images, run_case


@pytest.mark.parametrize(("case_name", "recipe_yaml", "corpus_file"), CASES,
                         ids=[c[0] for c in CASES])
def test_golden_case(case_name, recipe_yaml, corpus_file, tmp_path, settings):
    corpus_path = CORPUS_DIR / corpus_file
    expected_dir = EXPECTED_DIR / case_name
    assert corpus_path.is_file(), "corpus missing — run tests/golden/gen.py --update"
    assert expected_dir.is_dir(), "expected outputs missing — run tests/golden/gen.py --update"

    out_dir = tmp_path / "actual"
    result = run_case(case_name, recipe_yaml, corpus_file, out_dir, settings.models_dir)

    expected_files = sorted(
        p.relative_to(expected_dir) for p in expected_dir.rglob("*") if p.is_file()
    )
    actual_files = sorted(Path(o.path) for o in result.outputs)
    assert actual_files == expected_files, (
        f"output set changed: {actual_files} != {expected_files}"
    )
    for rel in expected_files:
        problem = compare_images(
            expected_dir / rel, out_dir / rel, jpg=rel.suffix == ".jpg"
        )
        assert problem is None, f"{case_name}/{rel}: {problem}"
