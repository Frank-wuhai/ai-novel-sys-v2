"""Regression: local_patch is now the default revise path (phase2/2).

Verifies the gate change in ``chapter_revision._try_local_patch_revision``:

1. Default brief (no revision_mode marker, no rewrite marker) --> local_patch
   is attempted (returns non-None result *or* attempts LLM patch — the key
   contract we check is that the gate does NOT short-circuit-return None).
2. rewrite_mode=True (fresh_rewrite or _revision_requires_rewrite) -->
   short-circuits with None so the caller falls through to the full rewrite.
3. Explicit ``修订模式:local_patch`` still routes through local_patch even
   when rewrite_mode=False (i.e. legacy behavior preserved).

We stub the DB-heavy dependencies with lightweight fakes so this stays a
pure unit test — no session, no LLM.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch as mock_patch


def _brief(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        goal=text,
        required_beats="",
        constraints="",
    )


def _version(content: str = "原始内容\n第二段\n第三段") -> SimpleNamespace:
    return SimpleNamespace(id=1, content=content, version_number=1)


def _chapter() -> SimpleNamespace:
    return SimpleNamespace(id=1, book_id=1, chapter_number=1)


def main() -> int:
    failures: list[str] = []
    from app.services.chapter_revision import _try_local_patch_revision

    # ---------------- case 1: default brief -> gate does NOT short-circuit
    #
    # We stub evaluate_generation_bias to return a "no hits" result which
    # causes _try_local_patch_revision to hand off to _try_llm_local_patch_revision.
    # We stub that too and record whether it was called — that's our
    # signal that the gate let us through.
    called_llm_fallback: list[bool] = []

    def _fake_bias(*args, **kwargs):
        return SimpleNamespace(model_bias_hits=[])

    def _fake_llm(*args, **kwargs):
        called_llm_fallback.append(True)
        return None  # LLM path also returns None, letting caller fall back

    with mock_patch("app.services.chapter_revision.evaluate_generation_bias", side_effect=_fake_bias), \
         mock_patch("app.services.chapter_revision._try_llm_local_patch_revision", side_effect=_fake_llm):
        result = _try_local_patch_revision(
            session=None,
            book_id=1,
            chapter=_chapter(),
            source_version=_version(),
            revision_brief=_brief("修复错别字"),  # no revision_mode marker
            canon_context="",
            dry_run=True,
            rewrite_mode=False,
        )
    if not called_llm_fallback:
        failures.append("case1: default brief did NOT reach local_patch path (gate wrongly short-circuited)")
    if result is not None:
        failures.append(f"case1: expected None result when LLM stub returns None, got {result}")

    # ---------------- case 2: rewrite_mode=True short-circuits to None
    called_llm_fallback.clear()
    with mock_patch("app.services.chapter_revision.evaluate_generation_bias", side_effect=_fake_bias), \
         mock_patch("app.services.chapter_revision._try_llm_local_patch_revision", side_effect=_fake_llm):
        result = _try_local_patch_revision(
            session=None,
            book_id=1,
            chapter=_chapter(),
            source_version=_version(),
            revision_brief=_brief("修订模式:fresh\n完整重写"),
            canon_context="",
            dry_run=True,
            rewrite_mode=True,  # caller flags rewrite
        )
    if called_llm_fallback:
        failures.append("case2: rewrite_mode=True must NOT invoke local_patch LLM fallback")
    if result is not None:
        failures.append(f"case2: rewrite_mode=True must return None, got {result}")

    # ---------------- case 3: explicit local_patch mode still works
    called_llm_fallback.clear()
    with mock_patch("app.services.chapter_revision.evaluate_generation_bias", side_effect=_fake_bias), \
         mock_patch("app.services.chapter_revision._try_llm_local_patch_revision", side_effect=_fake_llm):
        result = _try_local_patch_revision(
            session=None,
            book_id=1,
            chapter=_chapter(),
            source_version=_version(),
            revision_brief=_brief("修订模式:local_patch\n仅调整错别字"),
            canon_context="",
            dry_run=True,
            rewrite_mode=False,
        )
    if not called_llm_fallback:
        failures.append("case3: explicit 修订模式:local_patch did not reach local_patch path")

    # ---------------- case 4: bias hits -> deterministic patcher runs
    def _fake_bias_with_hits(*args, **kwargs):
        ns = SimpleNamespace(model_bias_hits=[SimpleNamespace(marker="badword", replacement="ok")])
        ns.to_dict = lambda: {"model_bias_hits": ["badword"]}
        return ns

    def _fake_apply(content, hits):
        return (content.replace("原始", "改良"), [("原始", "改良")])

    def _fake_store(*args, **kwargs):
        return SimpleNamespace(id=999, version_number=2)

    with mock_patch("app.services.chapter_revision.evaluate_generation_bias", side_effect=_fake_bias_with_hits), \
         mock_patch("app.services.chapter_revision.apply_model_drift_local_patch", side_effect=_fake_apply), \
         mock_patch("app.services.chapter_revision._store_local_patch_version", side_effect=_fake_store):
        result = _try_local_patch_revision(
            session=None,
            book_id=1,
            chapter=_chapter(),
            source_version=_version(),
            revision_brief=_brief("修复错别字"),
            canon_context="",
            dry_run=True,
            rewrite_mode=False,
        )
    if result is None or getattr(result, "id", None) != 999:
        failures.append(f"case4: deterministic patcher path must return stored version, got {result}")

    if failures:
        print("local_patch_default_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("local_patch_default_regression=PASS")
    print("cases_evaluated=4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
